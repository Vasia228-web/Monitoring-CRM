"""Нічна смуга хоста (`cli.py night lane`, E9, D53): темп, дедлайн, запобіжник, процес.

На коді до E9 смуг-процесів немає (realty/night не існує, у cli.py немає `night`).
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import FakeNet, add, clean_fuse, db, olx_url, ria_url  # noqa: E402,F401
from night_kit import (  # noqa: E402,F401
    FakeClock, TimedNet, clean_night, local_epoch, night_env, scope_of, utc_of,
)

from realty.liveness import policy, queue  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.usefixtures("clean_fuse", "clean_night")
T0 = local_epoch(2026, 10, 9, 1, 10)


def _row(lid, url, source="olx", **kw):
    base = dict(id=lid, site_key=None, source=source, external_id=str(lid), original_url=url,
                probe_url=None, is_active=True, manual_active=None, delisted_at=None,
                last_seen=datetime(2026, 10, 8), last_attempt=None, last_checked=None,
                check_failures=0, absent_since=None, viewed_at=None, property_id=None)
    base.update(kw)
    return queue.Row(**base)


def _spec(host, items, *, pace, stop_at, held_path=None, family=None, identity=None):
    from realty.night import codec

    return {"host": host, "family": family or policy.load().hosts[host].family, "pace": pace,
            "stop_at": stop_at, "held_path": str(held_path) if held_path else None,
            "identity": identity, "max_consecutive_blocks": 5, "block_share": 0.10,
            "block_min_requests": 20, "items": [codec.item_to_json(i) for i in items]}


def _olx_items(n, *, tier="onetime_blind", www_every=2):
    out = []
    for i in range(n):
        tok = f"10Ln{i:03d}"
        url = olx_url(tok) if i % www_every else olx_url(tok).replace("www.olx.ua", "olx.ua")
        out.append(queue.WorkItem(key=f"olx:{tok}", host="olx.ua", url=url, tier=tier,
                                  rows=(_row(i + 1, url),)))
    return out


def _lane(spec, net, clock, out=None):
    from realty.night.lane import Lane

    cfg = policy.load()
    return Lane(spec, out or io.StringIO(), fetcher=net.for_lane(spec["host"], clock), cfg=cfg,
                clock=clock, now_fn=lambda: utc_of(clock.time()), snapshots={})


def test_pace_is_start_to_start_across_every_address_of_the_host():
    """www.olx.ua і olx.ua — той самий сайт: пауза смуги між будь-якими двома запитами
    (обмежувач фетчера рахує за адресою — йому довіряти тут не можна)."""
    clock = FakeClock(T0)
    net = TimedNet(FakeNet(default=200))
    lane = _lane(_spec("olx.ua", _olx_items(10), pace=2.8, stop_at=T0 + 3600), net, clock)
    lane.run()
    times = [t for _h, t, _u, _m in net.log]
    assert len(times) == 10 and {u.split("/")[2] for _h, _t, u, _m in net.log} == {
        "www.olx.ua", "olx.ua"}
    assert min(b - a for a, b in zip(times, times[1:])) >= 2.8 - 1e-6


def test_no_request_is_issued_at_or_after_the_deadline():
    clock = FakeClock(T0)
    net = TimedNet(FakeNet(default=200))
    out = io.StringIO()
    lane = _lane(_spec("olx.ua", _olx_items(10), pace=2.8, stop_at=T0 + 10), net, clock, out)
    summary = lane.run()
    assert [t - T0 for _h, t, *_ in net.log] == pytest.approx([0, 2.8, 5.6, 8.4])
    assert summary["stopped"] == "deadline" and summary["not_reached"] == 6
    # Час від старту до останньої перевірки — замір кроку для `night --dry-run` (рецензія E9).
    assert summary["check_seconds"] == pytest.approx(8.4)
    assert json.loads(out.getvalue().splitlines()[-1])["t"] == "done"


def test_held_file_written_mid_run_stops_requests_for_that_site(tmp_path):
    held = tmp_path / "held.json"
    clock = FakeClock(T0)
    net = TimedNet(FakeNet(default=200))
    out = io.StringIO()
    lane = _lane(_spec("olx.ua", _olx_items(10), pace=2.8, stop_at=T0 + 3600, held_path=held),
                 net, clock, out)
    for _ in range(3):
        assert lane.step()
    held.write_text(json.dumps({"held": ["olx"]}))           # диригент: запобіжник спрацював
    summary = lane.run()
    assert len(net.log) == 3 and summary["skipped_held"] == 7
    kinds = [json.loads(line)["t"] for line in out.getvalue().splitlines()]
    assert kinds.count("item") == 3 and kinds.count("skip") == 7


def test_block_share_stops_the_lane_for_the_night():
    """Не поспіль, але понад 10% відмов після 20 запитів — смуга стоїть (інтеграція)."""
    calls = {"n": 0}

    def answer(url, method):
        calls["n"] += 1
        return 403 if calls["n"] % 4 == 0 else 200          # 25% відмов, поспіль — ніколи

    clock = FakeClock(T0)
    net = TimedNet(FakeNet(default=answer))
    summary = _lane(_spec("olx.ua", _olx_items(60), pace=2.8, stop_at=T0 + 3600), net,
                    clock).run()
    assert summary["stopped"] == "block_share"
    assert summary["requests"] == 20


def test_existence_check_after_the_deadline_is_refused_not_guessed():
    """Серія 404 дозріла, але слот перевірки існування — після stop_at: запиту немає,
    відповідь «не визначено» → «не знайдено» (не знято)."""
    from realty.night.lane import Lane

    cfg = policy.load()
    url = ria_url(34990001)
    streak = (datetime(2026, 10, 6, 1), datetime(2026, 10, 7, 2))
    item = queue.WorkItem(key="domria:34990001", host="dom.ria.com", url=url, tier="repeat404",
                          rows=(_row(1, url, source="domria"),), streak404=streak)
    clock = FakeClock(T0)
    net = TimedNet(FakeNet({"domria:34990001": 404}))
    out = io.StringIO()
    lane = Lane(_spec("dom.ria.com", [item], pace=1.0, stop_at=T0 + 0.5), out,
                fetcher=net.for_lane("dom.ria.com", clock), cfg=cfg, clock=clock,
                now_fn=lambda: utc_of(clock.time() + 86400), snapshots={})
    lane.run()
    assert len(net.log) == 1                                  # лише сам 404, без картки API
    rec = json.loads(out.getvalue().splitlines()[0])
    assert rec["verdict"]["kind"] == "not_found"


def _lane_proc(tmp_path, *, env_flag: bool, lock_path):
    import os

    from realty.night.lane import LANE_ENV

    plan = tmp_path / "plan.json"
    out = tmp_path / "out.jsonl"
    spec = _spec("olx.ua", [], pace=2.8, stop_at=T0 + 3600)
    spec["lock_path"] = str(lock_path)
    plan.write_text(json.dumps(spec))
    env = {k: v for k, v in os.environ.items() if k != LANE_ENV}
    if env_flag:
        env[LANE_ENV] = "olx.ua"
    r = subprocess.run([sys.executable, "cli.py", "night", "lane", "--plan", str(plan),
                        "--out", str(out)], cwd=ROOT, capture_output=True, text=True,
                       timeout=120, env=env)
    return r, out


def test_lane_process_runs_from_the_cli_and_reports_done(tmp_path):
    """Справжній процес смуги (`cli.py night lane`) — з порожнім планом: жодного запиту,
    рядок «done» і код 0 (мережу в дочірніх процесах тестів заборонено, D45). Батьківський
    процес (тут — тест, як диригент) тримає замок циклу з плану."""
    from realty.runner import CycleLock

    lock = CycleLock(tmp_path / "cycle.lock")
    assert lock.acquire()
    try:
        r, out = _lane_proc(tmp_path, env_flag=True, lock_path=tmp_path / "cycle.lock")
    finally:
        lock.release()
    assert r.returncode == 0, r.stderr[-2000:]
    done = json.loads(out.read_text().splitlines()[-1])
    assert done["t"] == "done" and done["requests"] == 0


def test_lane_process_refuses_without_the_conductor(tmp_path):
    """Ручний чи залишений `cli.py night lane` не питає сайтів: без змінної диригента —
    код 2; зі змінною, але замок циклу не в руках батьківського процесу — теж 2
    (рецензія E9, D53: інакше смуга з мережею й без замка — паралельно з циклом)."""
    from realty.runner import CycleLock

    lock_path = tmp_path / "cycle.lock"
    r, out = _lane_proc(tmp_path, env_flag=False, lock_path=lock_path)
    assert r.returncode == 2 and "лише нічний диригент" in r.stderr
    assert not out.exists()
    r, out = _lane_proc(tmp_path, env_flag=True, lock_path=lock_path)    # замка не тримає ніхто
    assert r.returncode == 2 and "замок циклу" in r.stderr and not out.exists()


def test_conductor_kills_a_real_lane_process_that_outlives_the_deadline(db, tmp_path):
    """Смуга — окремий процес у своїй групі: якщо після stop_requests + kill_grace вона
    ще жива, диригент зупиняє її примусово (як крок циклу) і звільняє замок."""
    from realty.night.conductor import Conductor, SubprocessLauncher
    from realty import configfiles

    add(db, olx_url("10Kill1"), source="olx", external_id="k")
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
    launcher = SubprocessLauncher(argv=lambda plan, out: sleeper)
    started = []
    real_start = launcher.start

    def start(host, plan, out):
        lane = real_start(host, plan, out)
        started.append(lane)
        return lane

    launcher.start = start
    master = FakeClock(T0)
    env, _ = night_env(tmp_path, scope_of(db), master, launcher)
    res = Conductor(ncfg=configfiles.load("night"), lcfg=policy.load(), env=env).run()
    assert res["status"] == "partial" and started
    assert all(lane.proc.poll() is not None for lane in started)      # процес мертвий
    assert all(info.get("killed") for info in res["lanes"].values())
