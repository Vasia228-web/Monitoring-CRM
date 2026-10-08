"""Спільне для тестів нічного диригента (E9, D53): віртуальний час і смуги в процесі тесту.

Справжня ніч — це ~95 хв і окремі процеси смуг. Тут той самий `Conductor` і та сама
`Lane` (lane.py), але:
  * годинник диригента — `FakeClock` (sleep лише пересуває час);
  * кожна смуга — `VirtualLane` зі СВОЇМ віртуальним годинником: вона робить кроки,
    поки її час не обжене час диригента, і її пауза (Pacer) пересуває лише її час —
    тож смуги «паралельні», а темп кожного хоста видно в часах запитів;
  * мережа — FakeNet з liveness_kit (за ключем «сайт:id»), мережі немає.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import FakeNet  # noqa: E402,F401

from realty import ops  # noqa: E402


def utc_of(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


def local_epoch(y, mo, d, h, mi, s=0) -> float:
    return datetime(y, mo, d, h, mi, s).timestamp()


class FakeClock:
    def __init__(self, t0: float) -> None:
        self.t = float(t0)
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += max(0.0, s)


class TimedNet:
    """FakeNet, що записує (віртуальний час смуги, адреса, метод) кожного запиту."""

    def __init__(self, net: FakeNet) -> None:
        self.net = net
        self.log: list[tuple[str, float, str, str]] = []     # (хост смуги, час, адреса, метод)
        self.clocks: dict[str, object] = {}

    def for_lane(self, host: str, clock):
        self.clocks[host] = clock
        outer = self

        class _Net:
            def check(self, url, method="HEAD", delay=None, max_bytes=0):
                outer.log.append((host, clock.time(), url, method))
                return outer.net.check(url, method=method, delay=delay, max_bytes=max_bytes)

            def close(self):
                pass

        return _Net()


class VirtualLane:
    def __init__(self, host, plan_path, out_path, *, master: FakeClock, net: TimedNet, cfg,
                 identity_fn=None, hang: bool = False, evidence_fn=None) -> None:
        from realty.liveness import capture
        from realty.night.lane import Lane

        spec = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        self.host = host
        self.master = master
        self.clock = FakeClock(master.time())
        self.out = open(out_path, "a", encoding="utf-8")
        self.lane = Lane(spec, self.out, fetcher=net.for_lane(host, self.clock), cfg=cfg,
                         hooks=capture.default_hooks(cfg), clock=self.clock,
                         now_fn=lambda: utc_of(self.clock.time()), identity_fn=identity_fn,
                         snapshots={},
                         # Рендери OLX (E11, D60): фабрика отримує годинник смуги.
                         evidence_fn=evidence_fn(self.clock) if evidence_fn else None)
        self.spec = spec
        self.hang = hang
        self.finished = False
        self.killed = False
        self.code = 0

    def alive(self) -> bool:
        if self.finished or self.killed:
            return False
        if self.hang:
            return True                                   # завислий процес: сам не виходить
        while self.clock.time() <= self.master.time():
            if not self.lane.step():
                self.lane.finish()
                self.finished = True
                self.out.close()
                return False
        return True

    def stop(self) -> None:
        self.killed = True
        if not self.out.closed:
            self.out.close()


class VirtualLauncher:
    def __init__(self, master: FakeClock, net: TimedNet, cfg, *, identity_fn=None,
                 hang=(), evidence_fn=None) -> None:
        self.master, self.net, self.cfg = master, net, cfg
        self.identity_fn = identity_fn
        self.evidence_fn = evidence_fn
        self.hang = set(hang)
        self.lanes: dict[str, VirtualLane] = {}

    def start(self, host, plan_path, out_path):
        lane = VirtualLane(host, plan_path, out_path, master=self.master, net=self.net,
                           cfg=self.cfg, identity_fn=self.identity_fn, hang=host in self.hang,
                           evidence_fn=self.evidence_fn)
        self.lanes[host] = lane
        return lane


@pytest.fixture
def clean_night():
    """Нічні записи й утримання хостів — у спільній ops.db тестів: кожен тест чистий."""
    def wipe():
        ops.init_ops(force=True)
        # night_state, olx_tab_seen — стан нічних робіт доказів (E11, D60).
        for table in ("night_runs", "night_holds", "liveness_fuse", "liveness_fuse_log",
                      "night_state", "olx_tab_seen"):
            try:
                with ops.engine.begin() as conn:
                    conn.execute(text(f"DELETE FROM {table}"))
            except Exception:                           # noqa: BLE001 — до E9 таблиць немає
                pass
    wipe()
    yield
    wipe()


def night_env(tmp_path, scope, master: FakeClock, launcher, *, backup=None, due=False,
              liquidity=None, backup_age_hours=None):
    """`due` — бекап потрібен за будь-яким правилом; `backup_age_hours` — вік останнього
    успішного бекапу (тоді «потрібен» = старший за поріг правила)."""
    from realty.night.conductor import Env

    calls = {"backup": 0, "due_asked": []}

    def _backup(timeout_s):
        calls["backup"] += 1
        return backup(timeout_s) if backup else {"status": "ok", "file": "test.tar.xz"}

    def _due(hours):
        calls["due_asked"].append(hours)
        if backup_age_hours is not None:
            return backup_age_hours > hours, f"{backup_age_hours} год тому"
        return due, None

    env = Env(clock=master, launcher=launcher, backup=_backup,
              backup_due=_due,
              liquidity=liquidity or (lambda s: {"all": {"median_days": 50, "events": 1,
                                                         "censored": 1}}),
              lock_path=tmp_path / "cycle.lock", disabled_flag=tmp_path / "COLLECTOR_OFF",
              work_dir=tmp_path / "night", scope=scope,
              utcnow=lambda: utc_of(master.time()))
    # Замок процесу перевірки при відкритті — свій для тесту (не data/lookup.lock).
    env.drain_lock = tmp_path / "lookup.lock"
    return env, calls


def scope_of(Session):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return scope
