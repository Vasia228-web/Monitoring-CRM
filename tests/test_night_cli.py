"""Нічний диригент назовні (E9, D53): `cli.py night --dry-run`, `cli.py liveness report`,
конфіг config/night.toml, вікна, юніти systemd, тривоги сторожа.

На коді до E9 команди `night` немає (argparse — код 2), `liveness report` друкує лише
JSON зведення /status, юніт нічних робіт — realty-identity (лише дозбір ознак),
сторож про ніч мовчить.
"""
from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))

from night_kit import clean_night, local_epoch  # noqa: E402,F401

from realty import configfiles, ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "deploy" / "fedora" / "systemd"
pytestmark = pytest.mark.usefixtures("clean_night")


# --- CLI (публічний шлях) ---------------------------------------------------------------


def _temp_dbs(tmp_path):
    from realty.models import Base, Listing

    db_path, ops_path = tmp_path / "cli.db", tmp_path / "cli_ops.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        for i in range(30):
            url = f"https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-{34100000 + i}.html"
            s.add(Listing(source="domria", external_id=str(34100000 + i), original_url=url,
                          price_usd=50_000.0, quality_status="ok", is_active=True,
                          last_seen=datetime(2026, 10, 1)))
        for i in range(12):
            url = f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{9100000 + i}/"
            s.add(Listing(source="lun", external_id=f"l{i}", original_url=url,
                          price_usd=50_000.0, quality_status="ok", is_active=False,
                          delisted_at=datetime(2026, 9, 1), last_seen=datetime(2026, 9, 25)))
        s.commit()
    engine.dispose()
    env = {**os.environ, "DB_URL": f"sqlite:///{db_path}", "OPS_DB_URL": f"sqlite:///{ops_path}"}
    return env, ops_path


def _cli(env, *args):
    return subprocess.run([sys.executable, "cli.py", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


def test_dry_run_prints_requests_times_pace_per_host_without_writing(tmp_path):
    env, _ops = _temp_dbs(tmp_path)
    r = _cli(env, "night", "--dry-run", "--json")
    assert r.returncode == 0, r.stderr[-2000:]
    d = json.loads(r.stdout)
    ria, rlt = d["hosts"]["dom.ria.com"], d["hosts"]["rieltor.ua"]
    assert ria["tiers"]["onetime_blind"] == 30 and ria["requests"] == 30
    assert ria["seconds"] == pytest.approx(30 * ria["pace"])
    assert rlt["tiers"]["onetime_reseen"] == 12 and rlt["pace"] >= 3.0
    assert rlt["seconds"] == pytest.approx(12 * rlt["pace"])
    # Крок — max(темп, типовий час запиту): GET DOM.RIA ≈ 1,4 с (рецензія E9, D53), тож
    # у вікно 97 хв — 4 157 ключів, а не 5 820; перше вікно — ще й без бекапу (потрібен:
    # у тимчасовій ops.db бекапів немає).
    assert ria["rate"] == pytest.approx(1.4) and ria["rate_source"] == "типовий"
    assert ria["window_capacity"] == int((97 * 60) // 1.4)
    assert d["backup"]["due"] and d["backup"]["seconds"] == 15 * 60
    assert ria["first_window_capacity"] == int((82 * 60) // 1.4)
    assert rlt["rate"] == rlt["pace"] and ria["left_after_windows"] == [0, 0, 0, 0]
    assert d["order"][0] == "canary" and d["order"][-1] == "identity"
    text = _cli(env, "night", "--dry-run")
    assert text.returncode == 0
    assert "× крок =" in text.stdout and "rieltor.ua" in text.stdout
    assert "лишиться після вікон 1–4" in text.stdout
    # Нічого не записано: нічних вікон немає, перевірок теж.
    rep = _cli(env, "liveness", "report")
    assert "нічних вікон ще не було" in rep.stdout


def test_liveness_report_prints_the_night_per_host_with_before_after(tmp_path):
    env, ops_path = _temp_dbs(tmp_path)
    eng = create_engine(f"sqlite:///{ops_path}", future=True)
    ops.OpsBase.metadata.create_all(eng)
    S = sessionmaker(bind=eng, future=True)
    with S() as s:
        s.add(ops.NightRun(
            night_date="2026-10-09", window="01:10", status="ok",
            started_at=datetime(2026, 10, 8, 22, 10), finished_at=datetime(2026, 10, 8, 23, 50),
            stop_requests_at=datetime(2026, 10, 8, 23, 47),
            release_lock_at=datetime(2026, 10, 8, 23, 55),
            lock_acquired_at=datetime(2026, 10, 8, 22, 10),
            lock_released_at=datetime(2026, 10, 8, 23, 49),
            active_before=22869, active_after=23880,
            per_host=json.dumps({"olx.ua": {"keys": 1450, "rows": 1600, "delisted": 4,
                                            "restored": 1015, "repaired": 2, "unknown": 31,
                                            "not_found": 9, "held": 0},
                                 "rieltor.ua": {"keys": 1400, "rows": 1409, "delisted": 0,
                                                "restored": 0, "repaired": 0, "unknown": 12,
                                                "not_found": 0, "held": 0}}),
            lanes=json.dumps({"olx.ua": {"requests": 1452, "stopped": None, "not_reached": 0,
                                         "skipped_held": 0},
                              "rieltor.ua": {"requests": 1400, "stopped": "deadline",
                                             "not_reached": 9, "skipped_held": 0}}),
            liquidity_before=json.dumps({"all": {"median_days": 56, "events": 2577,
                                                 "censored": 12548}}),
            liquidity_after=json.dumps({"all": {"median_days": 61, "events": 2560,
                                                "censored": 13570}})))
        s.commit()
    eng.dispose()
    r = _cli(env, "liveness", "report")
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout
    assert "НІЧ №" in out and "olx.ua" in out and "rieltor.ua" in out
    olx_line = next(line for line in out.splitlines() if line.strip().startswith("olx.ua"))
    assert olx_line.split()[1:8] == ["1452", "1450", "4", "1015", "2", "31", "9"]
    assert "до 22869 → після 23880 (повернуто 1015, знято 4" in out
    assert "медіана 56 дн." in out and "медіана 61 дн." in out
    # Часи — місцеві (вікна конфігу), у дужках — UTC (рецензія E9, D53).
    from realty.night.report import local

    stop = datetime(2026, 10, 8, 23, 47)
    assert f"запити до {local(stop):%H:%M} (UTC 23:47)" in out
    status = _cli(env, "liveness", "report", "--status")
    assert status.returncode == 0 and json.loads(status.stdout)["computed_at"]


def test_unhold_from_the_cli(tmp_path):
    env, ops_path = _temp_dbs(tmp_path)
    eng = create_engine(f"sqlite:///{ops_path}", future=True)
    ops.OpsBase.metadata.create_all(eng)
    with sessionmaker(bind=eng, future=True)() as s:
        s.add(ops.NightHold(host="rieltor.ua", state="held", reason="блокування"))
        s.commit()
    eng.dispose()
    r = _cli(env, "night", "unhold", "--host", "rieltor.ua")
    assert r.returncode == 0 and "знову дозволено" in r.stdout


# --- Конфіг і вікна ---------------------------------------------------------------------


def _night_data() -> dict:
    return tomllib.loads((ROOT / "config" / "night.toml").read_text(encoding="utf-8"))


def _problems(data) -> list[str]:
    errors: list[str] = []
    configfiles._build(configfiles.NightConfig, data, "", errors)
    return errors


def test_night_config_is_strict_about_order_windows_and_hosts():
    assert _problems(_night_data()) == []
    d = _night_data()
    d["jobs"]["order"] = ["held", "canary"]
    assert any("canary — першою" in e for e in _problems(d))
    d = _night_data()
    d["jobs"]["order"] = ["canary", "identity", "rm_sample"]
    assert any("identity — останньою" in e for e in _problems(d))
    d = _night_data()
    d["windows"][1]["start"] = "02:50"
    assert any("перекриваються" in e for e in _problems(d))
    d = _night_data()
    d["windows"][0]["release_lock"] = "02:48"
    assert any("kill_grace_seconds" in e for e in _problems(d))
    d = _night_data()
    d["jobs"]["rm_sample_per_host"] = {"blagodeveloper.com": 3}
    assert any("blagodeveloper.com" in e for e in _problems(d))
    d = _night_data()
    d["jobs"]["order"] = ["canary", "sweep"]
    assert any("не з переліку" in e for e in _problems(d))
    d = _night_data()
    del d["lock"]["poll_seconds"]
    assert any("ключа немає" in e for e in _problems(d))


def test_windows_deadlines_and_budget():
    from realty.night import windows

    cfg = configfiles.load("night")
    at = local_epoch(2026, 10, 9, 1, 10, 30)
    w = windows.current(cfg, at)
    assert w.label == "01:10" and w.night_date == "2026-10-09"
    assert w.stop_requests == local_epoch(2026, 10, 9, 2, 47)
    assert w.release_lock == local_epoch(2026, 10, 9, 2, 55)
    w = windows.current(cfg, at, budget_min=30)
    assert w.stop_requests == at + 30 * 60 and w.release_lock == local_epoch(2026, 10, 9, 2, 55)
    assert windows.current(cfg, local_epoch(2026, 10, 9, 3, 30)) is None
    assert windows.current(cfg, local_epoch(2026, 10, 9, 4, 50)).label == "04:10"
    nxt = windows.upcoming(cfg, local_epoch(2026, 10, 9, 14, 0))
    assert nxt.label == "01:10" and nxt.night_date == "2026-10-10"


# --- Юніти й встановлення -----------------------------------------------------------------


def _section(name, section):
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(UNITS / name, encoding="utf-8")
    return dict(p[section])


def test_one_night_timer_replaces_the_identity_timer():
    assert not (UNITS / "realty-identity.timer").exists()
    assert not (UNITS / "realty-identity.service").exists()
    timer = _section("realty-night.timer", "Timer")
    assert timer["OnCalendar"] == "*-*-* 01,04:10:00" and timer["Persistent"] == "false"
    svc = _section("realty-night.service", "Service")
    assert svc["ExecStart"].endswith("cli.py night --budget-min 100")
    assert svc["Type"] == "oneshot" and svc["KillMode"] == "control-group"
    # Пріоритети нічного юніта — як вимагає інтеграція (конфлікт 13).
    want = {"Nice": "15", "CPUWeight": "20", "IOWeight": "20", "IOSchedulingClass":
            "best-effort", "IOSchedulingPriority": "7", "OOMScoreAdjust": "500",
            "MemoryHigh": "1800M", "MemoryMax": "2600M"}
    assert {k: svc[k] for k in want} == want
    # systemd уб'є все до циклів о 03:05 і 06:05, навіть якщо код зависне.
    span = svc["TimeoutStartSec"]
    assert span == "1h50min"
    cfg = configfiles.load("night")
    for w in cfg.windows:
        start = configfiles.hhmm_minutes(w.start)
        assert start + 110 < configfiles.hhmm_minutes(w.release_lock) + 10
    install = (ROOT / "deploy" / "fedora" / "install.sh").read_text(encoding="utf-8")
    assert "disable --now realty-identity.timer" in install
    assert "realty-night.timer" in install and "realty-identity.timer realty-dedup" not in install


def test_install_switches_timers_safely_when_rerun():
    """Обрив install.sh між вимиканням старого таймера й увімкненням нового не лишає машину
    без нічного таймера (рецензія E9, D53): рішення — зі стану, що переживає обрив (старий
    identity, сам нічний або цикл увімкнені), і новий таймер вмикається ДО того, як старий
    прибирається; служба старого, що саме йде, зупиняється окремо."""
    install = (ROOT / "deploy" / "fedora" / "install.sh").read_text(encoding="utf-8")
    loop = install[install.index("want_night=0"):install.index("cp deploy/fedora/systemd")]
    for unit in ("realty-identity.timer", "realty-night.timer", "realty-cycle.timer"):
        assert unit in loop
    enable = install.index('systemctl --user enable --now realty-night.timer\n  echo "   нічний')
    assert enable < install.index("disable --now realty-identity.timer")
    assert enable < install.index('rm -f "$UNITS/realty-identity.service"')
    assert "stop realty-identity.service" in install
    svc = (UNITS / "realty-night.service").read_text(encoding="utf-8")
    assert "IOWeight СЬОГОДНІ НЕ ДІЄ" in svc and "root-setup.sh немає" in svc
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "Відкат нічного диригента" in deploy and "journalctl --user -u realty-night" in deploy


# --- Сторож -----------------------------------------------------------------------------------


def test_watchdog_alerts_on_backup_failure_blocks_holds_and_late_release():
    from realty import watchdog

    now = datetime(2026, 10, 9, 0, 0)
    ops.init_ops()
    with ops.ops_session() as s:
        s.add(ops.NightRun(night_date="2026-10-09", window="01:10", status="backup_failed",
                           started_at=now - timedelta(hours=1), message="диск"))
        s.add(ops.NightRun(night_date="2026-10-09", window="04:10", status="partial",
                           started_at=now - timedelta(minutes=30),
                           lanes=json.dumps({"rieltor.ua": {"stopped": "blocks", "blocked": 5,
                                                            "requests": 5}}),
                           release_lock_at=now - timedelta(minutes=5),
                           lock_released_at=now - timedelta(minutes=1)))
        s.add(ops.NightHold(host="olx.ua", state="held", since=now, reason="блокування"))
    keys = {a.key for a in watchdog.check_night(now)}
    assert {"night-backup", "night-blocked:rieltor.ua", "night-hold:olx.ua",
            "night-late"} <= keys
    assert "night-failed" not in keys
    with ops.ops_session() as s:                      # процес убито — запис «триває» вічно
        s.add(ops.NightRun(night_date="2026-10-08", window="04:10", status="running",
                           started_at=now - timedelta(hours=20)))
    alerts = {a.key: a for a in watchdog.check_night(now)}
    assert "night-failed" in alerts
    # Бекап не вдався — «у цьому вікні нічого не писали» (наступне вікно пробує знову).
    assert "у цьому вікні нічого не писали" in alerts["night-backup"].text
    # Час — місцевий, як вікна в конфігу, у дужках — UTC.
    late = alerts["night-late"].text
    released = now - timedelta(minutes=1)
    from realty.night.report import local

    assert f"{local(released):%H:%M} (UTC {released:%H:%M})" in late


def test_watchdog_alerts_when_no_night_runs_or_windows_keep_being_skipped(tmp_path, monkeypatch):
    """Ніч не відбулась зовсім (таймер вимкнено, диригент падає до свого запису), а цикли
    йдуть — night-missing; два вікна поспіль «цикл не звільнив замок» — night-skipped."""
    from realty import watchdog

    monkeypatch.setattr(watchdog, "COLLECTOR_OFF", tmp_path / "COLLECTOR_OFF", raising=False)
    monkeypatch.setattr(watchdog, "NIGHT_TIMER_UNIT", tmp_path / "realty-night.timer",
                        raising=False)
    now = datetime(2026, 10, 20, 12, 0)
    ops.init_ops()
    with ops.ops_session() as s:
        cycle = ops.CycleRecord(started_at=now - timedelta(hours=3), status="ok")
        s.add(cycle)
        s.flush()
        cycle_id = cycle.id
    try:
        _missing_and_skipped(watchdog, tmp_path, now)
    finally:
        with ops.ops_session() as s:                   # спільна ops.db тестів — прибрати за собою
            s.delete(s.get(ops.CycleRecord, cycle_id))


def _missing_and_skipped(watchdog, tmp_path, now):
    keys = {a.key for a in watchdog.check_night(now)}
    assert "night-missing" not in keys                 # нічний диригент ще не розгорнуто
    (tmp_path / "realty-night.timer").write_text("[Timer]")
    assert "night-missing" in {a.key for a in watchdog.check_night(now)}
    (tmp_path / "COLLECTOR_OFF").write_text("")
    assert "night-missing" not in {a.key for a in watchdog.check_night(now)}
    (tmp_path / "COLLECTOR_OFF").unlink()
    with ops.ops_session() as s:
        for h in (30, 27):
            s.add(ops.NightRun(night_date="2026-10-19", window="01:10", status="lock_timeout",
                               started_at=now - timedelta(hours=h)))
    keys = {a.key for a in watchdog.check_night(now)}
    assert "night-missing" in keys and "night-skipped" in keys
    with ops.ops_session() as s:
        s.add(ops.NightRun(night_date="2026-10-20", window="04:10", status="ok",
                           started_at=now - timedelta(hours=8)))
    keys = {a.key for a in watchdog.check_night(now)}
    assert "night-missing" not in keys and "night-skipped" not in keys


def test_conductor_that_cannot_start_still_leaves_a_failed_record(monkeypatch):
    """Збій до власного запису диригента (конфіг, база) — рядок «failed» однаково (сторож
    бачить), а не лише «failed» у systemd."""
    import argparse
    import signal

    import cli
    from realty.night import conductor

    class Broken:
        def __init__(self, **kw):
            raise ValueError("night.toml: ключа немає")

    monkeypatch.setattr(conductor, "Conductor", Broken)
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    with pytest.raises(ValueError):
        cli.cmd_night(argparse.Namespace(action="run", plan=None, out=None, host=None,
                                         dry_run=False, json=False, budget_min=100))
    from sqlalchemy import select

    with ops.ops_session() as s:
        row = s.scalars(select(ops.NightRun).order_by(ops.NightRun.id.desc())).first()
        assert row.status == "failed" and "диригент не стартував" in row.message
