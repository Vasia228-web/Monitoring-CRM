"""Нічний дозбір ознак: не паралельно зі збором, у межах бюджету, з місця зупинки."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import realty.identity_backfill as bf
from realty.fetcher import FetchError
from realty.models import Base, Listing
from realty.runner import CycleLock, StepResult

SEEN = datetime(2026, 9, 1, 12, 0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    clock, calls = Clock(), []

    class FakeFetcher:
        def __init__(self, *a, **kw):
            pass

        def get_json(self, url, params=None):
            ext = url.rsplit("/", 1)[1]
            calls.append(ext)
            clock.t += 10                               # кожна картка — 10 «секунд»
            if ext == "404":
                raise FetchError("HTTP 404")
            return {"flat_entity_id": int(ext) * 7, "building_entity_id": 5,
                    "latitude": 48.9, "longitude": 24.7, "user_id": 1}

        def close(self):
            pass

    monkeypatch.setattr(bf, "SessionLocal", Session)
    monkeypatch.setattr(bf, "init_db", lambda: None)
    monkeypatch.setattr(bf, "Fetcher", FakeFetcher)
    monkeypatch.setattr(bf.time, "monotonic", clock)
    monkeypatch.setattr(bf.ops, "beat", lambda *a, **kw: None)
    with Session() as s:
        for ext, ident in [("1", None), ("2", None), ("3", {"flat": "ria:21"}),
                           ("404", None), ("5", None)]:
            s.add(Listing(source="domria", external_id=ext, original_url=f"https://d/{ext}",
                          identity=ident, is_active=ext != "5",
                          first_seen=SEEN, last_seen=SEEN))
        s.commit()
    kw = dict(lock_path=tmp_path / "cycle.lock", disabled_flag=tmp_path / "OFF")
    return Session, calls, kw


def _idents(Session):
    with Session() as s:
        return {l.external_id: l.identity for l in s.scalars(select(Listing))}


def test_skips_the_window_while_a_cycle_holds_the_lock(env):
    Session, calls, kw = env
    lock = CycleLock(kw["lock_path"])
    assert lock.acquire()
    try:
        rep = bf.run(["domria"], budget_s=3600, **kw)
    finally:
        lock.release()
    assert rep["status"].startswith("skipped") and calls == []


def test_disabled_collector_disables_backfill_too(env):
    Session, calls, kw = env
    kw["disabled_flag"].write_text("")
    assert bf.run(["domria"], budget_s=3600, **kw)["status"] == "disabled"
    assert calls == []


def test_fetches_only_listings_without_identity_and_keeps_last_seen(env):
    Session, calls, kw = env
    rep = bf.run(["domria"], budget_s=3600, **kw)
    assert sorted(calls) == ["1", "2", "404", "5"]            # «3» уже мав ознаки
    got = _idents(Session)
    assert got["1"]["flat"] == "ria:7" and got["3"] == {"flat": "ria:21"}
    assert got["404"]["flat"] == "ria:?" and "unavailable" in got["404"]
    assert rep["errors"] == 1 and rep["left"]["domria"] == 0
    with Session() as s:                                     # дозбір — не «бачили в стрічці»
        assert {l.last_seen for l in s.scalars(select(Listing))} == {SEEN}
    # Друге вікно: нема чого брати — і недоступну картку не смикаємо щоночі.
    calls.clear()
    bf.run(["domria"], budget_s=3600, **kw)
    assert calls == []


def test_budget_stops_the_window_and_the_next_one_resumes(env):
    Session, calls, kw = env
    rep = bf.run(["domria"], budget_s=25, **kw)              # влізає 3 картки по 10 с
    assert calls == ["404", "2", "1"] and rep["left"]["domria"] == 1   # спершу активні
    calls.clear()
    bf.run(["domria"], budget_s=3600, **kw)
    assert calls == ["5"]                                    # з місця зупинки
    assert all(v.get("flat") for v in _idents(Session).values())


def test_full_pass_runs_first_and_is_capped_by_the_remaining_budget(env, monkeypatch):
    Session, calls, kw = env
    with Session() as s:
        for i in range(60):
            s.add(Listing(source="lun", external_id=f"l{i}", original_url=f"https://l/{i}",
                          first_seen=SEEN, last_seen=SEEN))
        s.commit()
    steps = []

    def fake_step(step, budget):
        steps.append((step.argv[-8:], round(step.timeout), round(budget)))
        return StepResult(step.name, "ok", 1.0, 0), None

    monkeypatch.setattr(bf, "run_step", fake_step)
    bf.run(["domria", "lun"], budget_s=600, **kw)
    assert steps and "lun" in steps[0][0] and "--no-llm" in steps[0][0]
    assert steps[0][1] <= 600 and steps[0][2] <= 600       # не довше за бюджет вікна
    assert calls                                           # DIM.RIA — після, на решту
