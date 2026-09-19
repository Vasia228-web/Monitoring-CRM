"""Сигнал тиші: тривога від ВІДСУТНОСТІ успіху, без спаму, з «відновилось»."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from realty import backup, ops, watchdog  # noqa: F401  (backup реєструє свою таблицю)

NOW = datetime(2026, 9, 19, 12, 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(engine)
    monkeypatch.setattr(watchdog, "enabled_sources", lambda: ["domria", "olx"])
    monkeypatch.setattr(watchdog, "check_verify_blocks", lambda now: [])
    monkeypatch.setattr(watchdog, "PUBLIC_URL_PATH", tmp_path / "public_url")
    sent: list[str] = []
    state = tmp_path / "alerts.json"
    return {"sent": sent, "run": lambda now: watchdog.run(now, sent.append, state)}


def _cycle(status, finished, kept=0, message=None):
    with ops.ops_session() as s:
        s.add(ops.CycleRecord(status=status, started_at=finished - timedelta(minutes=40),
                              finished_at=finished, kept=kept, message=message))


def _run(source, started, kept, status="ok", ok=50, failed=0, blocked=0, written=None):
    written = kept if written is None else written
    with ops.ops_session() as s:
        s.add(ops.RunRecord(source=source, mode="fresh", status=status, started_at=started,
                            finished_at=started + timedelta(minutes=3), kept=kept,
                            updated=written,
                            requests_ok=ok, requests_failed=failed, requests_blocked=blocked))


def test_silence_is_detected_from_absence_not_from_errors(env):
    """Рівно випадок 10.09: помилок немає, процес живий, успіху — теж."""
    _cycle("ok", NOW - timedelta(hours=9), kept=120)
    rep = env["run"](NOW)
    assert rep["sent"] == ["silence"]
    assert "Тиша: 9 год" in env["sent"][0]


def test_alive_but_collected_nothing_is_an_outage(env):
    _cycle("failed", NOW - timedelta(hours=1), kept=0, message="за цикл не зібрано жодного")
    _cycle("failed", NOW - timedelta(hours=4), kept=0)
    _cycle("ok", NOW - timedelta(hours=8), kept=90)
    rep = env["run"](NOW)
    assert "silence" in rep["sent"]
    assert "не зібрано" in env["sent"][0]


def test_quiet_when_recent_success(env):
    _cycle("partial", NOW - timedelta(hours=2), kept=40)
    assert env["run"](NOW)["active"] == []
    assert env["sent"] == []


def test_one_message_per_event_then_repeat_then_recovery(env):
    _cycle("ok", NOW - timedelta(hours=10), kept=100)
    env["run"](NOW)
    env["run"](NOW + timedelta(minutes=30))
    env["run"](NOW + timedelta(hours=2))
    assert len(env["sent"]) == 1                          # не спамимо
    env["run"](NOW + timedelta(hours=6, minutes=5))
    assert len(env["sent"]) == 2 and "досі триває" in env["sent"][1]
    _cycle("ok", NOW + timedelta(hours=6, minutes=30), kept=80)
    rep = env["run"](NOW + timedelta(hours=7))
    assert rep["resolved"] == ["silence"]
    assert "Відновилось" in env["sent"][2]
    env["run"](NOW + timedelta(hours=8))
    assert len(env["sent"]) == 3                          # «відновилось» — один раз


def test_fresh_install_waits_before_shouting(env):
    assert env["run"](NOW)["active"] == []
    assert env["run"](NOW + timedelta(hours=8))["active"] == ["silence"]


def test_failed_send_is_retried_next_time(env, tmp_path):
    _cycle("ok", NOW - timedelta(hours=10), kept=100)
    def broken(_):
        raise RuntimeError("мережа")
    rep = watchdog.run(NOW, broken, tmp_path / "alerts.json")
    assert rep["errors"] and not rep["sent"]
    rep = env["run"](NOW + timedelta(minutes=30))
    assert rep["sent"] == ["silence"]


def test_source_that_quietly_collects_nothing(env):
    _cycle("ok", NOW - timedelta(hours=1), kept=200)
    for i in range(8):
        _run("domria", NOW - timedelta(hours=3 * (i + 3)), kept=160)
    _run("domria", NOW - timedelta(hours=6), kept=0)
    _run("domria", NOW - timedelta(hours=3), kept=52, written=0)   # зібрав, але карантин не пустив
    rep = env["run"](NOW)
    assert rep["sent"] == ["drop:domria"]
    assert "не дійшло жодного оголошення" in env["sent"][0]


def test_early_stop_is_not_a_drop(env):
    """Регресія: DIM.RIA зупиняється після першої сторінки, коли новинок немає
    (20 замість 160) — перша версія правила вважала це поломкою."""
    _cycle("ok", NOW - timedelta(hours=1), kept=200)
    for i in range(8):
        _run("domria", NOW - timedelta(hours=3 * (i + 3)), kept=160)
    _run("domria", NOW - timedelta(hours=6), kept=20)
    _run("domria", NOW - timedelta(hours=3), kept=20)
    assert env["run"](NOW)["active"] == []


def test_single_bad_run_is_not_yet_an_alarm(env):
    _cycle("ok", NOW - timedelta(hours=1), kept=200)
    for i in range(8):
        _run("domria", NOW - timedelta(hours=3 * (i + 2)), kept=160)
    _run("domria", NOW - timedelta(hours=3), kept=0, written=0)
    assert env["run"](NOW)["active"] == []


def test_block_rate_jump(env):
    _cycle("ok", NOW - timedelta(hours=1), kept=200)
    for i in range(6):
        _run("olx", NOW - timedelta(hours=3 * (i + 3)), kept=50, ok=50, blocked=0)
    _run("olx", NOW - timedelta(hours=6), kept=50, ok=50)
    _run("olx", NOW - timedelta(hours=3), kept=20, ok=30, failed=20, blocked=18)
    rep = env["run"](NOW)
    assert rep["sent"] == ["blocks:olx"]
    assert "36%" in env["sent"][0]


def test_failed_backup_is_reported(env):
    _cycle("ok", NOW - timedelta(hours=1), kept=200)
    with ops.ops_session() as s:
        s.add(backup.BackupRecord(status="failed", created_at=NOW - timedelta(hours=2),
                                  message="немає жодного місця поза машиною"))
    rep = env["run"](NOW)
    assert rep["sent"] == ["backup"]
    assert "поза машиною" in env["sent"][0]
