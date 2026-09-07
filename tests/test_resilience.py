"""Стійкість до мережевих збоїв і чесність статусу після падіння."""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty import ops
from realty.fetcher import FetchError
from realty.pipeline import Pipeline
from realty.sources.base import BaseSource


def test_browser_errors_become_fetch_errors(monkeypatch):
    """Регресія: помилка Playwright не є FetchError, тому пролітала повз усі
    перехоплювачі й валила весь прогін — так сталося на обриві мережі."""
    from realty.fetcher import BrowserFetcher

    b = BrowserFetcher(label="тест")
    monkeypatch.setattr(b, "_ensure", lambda: None)
    monkeypatch.setattr(b, "cache", type("C", (), {"get": lambda *a: None,
                                                   "set": lambda *a: None})())

    class _Page:
        def goto(self, *a, **kw):
            raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")
        def close(self): pass

    b._ctx = type("Ctx", (), {"new_page": lambda self: _Page()})()
    with pytest.raises(FetchError) as e:
        b.render("https://example.test/x")
    assert "ERR_INTERNET_DISCONNECTED" in str(e.value)


class _Boom(BaseSource):
    name = "domria"

    def iter_listings(self):
        yield {"source": "domria", "external_id": "1", "original_url": "u1",
               "price": 1.0, "currency": "USD", "rooms": 1, "area_total": 40.0}
        raise RuntimeError("мережа зникла")


def test_source_crash_keeps_what_was_collected(monkeypatch, tmp_path):
    """Збій джерела не має забирати з собою вже зібране й решту джерел."""
    import realty.db as db
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from realty.models import Base, Listing

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, future=True))
    monkeypatch.setattr(db, "init_db", lambda: None)

    import realty.pipeline as pl
    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setitem(pl.REGISTRY, "domria", _Boom)

    p = Pipeline(sources=["domria"], use_llm=False)
    p.run()                                   # не має кинути назовні

    with db.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Listing)) == 1
    assert p.report.per_source["domria"]["errors"] == 1

    last = ops.recent_runs(1)[0]
    assert last["source"] == "domria" and last["status"] == "failed"
    assert last["finished_at"], "запис прогону лишився відкритим"
    assert "мережа зникла" in (last["message"] or "")


def test_dead_run_is_reaped():
    """Прогін, чий процес уже помер, має закритись, а не висіти вічно."""
    run_id = ops.start_run("проба-зависання", mode="fresh", trigger="manual")
    with ops.ops_session() as s:
        s.get(ops.RunRecord, run_id).pid = 999999      # такого процесу немає

    assert ops.reap_stale_runs() >= 1
    with ops.ops_session() as s:
        run = s.get(ops.RunRecord, run_id)
        assert run.status == "failed" and run.finished_at is not None
        assert "не закривши прогін" in (run.message or "")
        s.delete(run)


def test_busy_flag_without_heartbeat_is_not_active():
    """Регресія: після падіння дашборд показував «Працює», хоча процесів нуль."""
    ops.beat("проба", busy=True)
    with ops.ops_session() as s:
        hb = s.get(ops.Heartbeat, 1)
        hb.beat_at = ops._now() - timedelta(minutes=ops.IDLE_AFTER_MIN + 20)

    health = ops.worker_health()
    assert health["state"] != "active", "хибний зелений сигнал після обриву"
    assert health["alert"], "мовчазний «зайнятий» воркер має давати тривогу"

    ops.beat("відновлено", busy=False)
