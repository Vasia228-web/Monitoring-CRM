"""Тривога сторожа «підозрілих зведень різко більше» і нові види перевірки (Блок 4,
E10, D57): нові види complex / complex_phase не дають хибної тривоги, бо сплеск
рахується за suspicious_core («старі» види); стара ops.db отримує колонку з NULL.

Без імпорту нових модулів: тести йдуть через публічні watchdog.check_dedup і
ops.init_ops і на коді до E10 падають на поведінці.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_watchdog_new_audit_kind_does_not_raise_false_alarm(tmp_path, monkeypatch):
    """8 старих перевірок по ~300 підозрілих (suspicious_core NULL) і нова: 420, з них 120
    лише «різні ЖК», core = 300 → тривоги немає (стара формула дала б 420 > 410)."""
    from sqlalchemy import create_engine

    from realty import ops, watchdog

    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", eng)
    monkeypatch.setattr(ops, "OpsSession", sessionmaker(bind=eng, expire_on_commit=False,
                                                        future=True))
    ops.init_ops(force=True)
    now = datetime(2026, 10, 8, 12, 0)
    # Рядки — прямим SQL (і колонкою, якщо її ще немає): тест іде через публічний
    # check_dedup і на коді до E10 падає на тривозі, а не на моделі.
    with eng.begin() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("dedup_audits")}
        if "suspicious_core" not in cols:
            conn.execute(text("ALTER TABLE dedup_audits ADD COLUMN suspicious_core INTEGER"))
        rows = [(now - timedelta(hours=24 - k), 300 + k % 3, None, {"floor": 300})
                for k in range(8)]
        rows.append((now - timedelta(hours=1), 420, 300, {"floor": 300, "complex": 120}))
        for at, sus, core, kinds in rows:
            conn.execute(text(
                "INSERT INTO dedup_audits (at, properties, suspicious, suspicious_core, missed, "
                "by_kind) VALUES (:at, 5000, :s, :c, 10, :k)"),
                {"at": at.strftime("%Y-%m-%d %H:%M:%S.%f"), "s": sus, "c": core,
                 "k": json.dumps(kinds)})
    alerts = watchdog.check_dedup(now)
    assert not [a for a in alerts if a.key.startswith("dedup-")], alerts


def test_old_audit_rows_get_null_core_column(tmp_path, monkeypatch):
    """Нова колонка suspicious_core у наявній ops.db — NULL на старих рядках, а не 0."""
    import sqlite3

    from sqlalchemy import create_engine

    from realty import ops

    path = tmp_path / "old_ops.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE dedup_audits (id INTEGER PRIMARY KEY, at DATETIME, rules "
                "VARCHAR(200), properties INTEGER, suspicious INTEGER, missed INTEGER, "
                "by_kind TEXT, queue TEXT, missed_list TEXT)")
    con.execute("INSERT INTO dedup_audits (properties, suspicious, missed) VALUES (1, 5, 0)")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}", future=True)
    monkeypatch.setattr(ops, "engine", eng)
    ops.init_ops(force=True)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT suspicious_core FROM dedup_audits")).scalar() is None


def test_watchdog_places_alerts(tmp_path, monkeypatch):
    """Крок «райони й ЖК»: упав — тривога; would_change > 0 — тривога «чекає рішення»."""
    from sqlalchemy import create_engine

    from realty import ops, watchdog

    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", eng)
    monkeypatch.setattr(ops, "OpsSession", sessionmaker(bind=eng, expire_on_commit=False,
                                                        future=True))
    ops.init_ops(force=True)
    now = datetime(2026, 10, 8, 12, 0)
    assert watchdog.check_places(now) == []
    with ops.ops_session() as s:
        s.add(ops.PlacesRun(at=now, status="ok", kind="cycle", would_change=0))
    assert watchdog.check_places(now) == []
    with ops.ops_session() as s:
        s.add(ops.PlacesRun(at=now, status="ok", kind="cycle", would_change=3,
                            would_change_detail=json.dumps({"by_field": {"complex": 3}})))
    keys = [a.key for a in watchdog.check_places(now)]
    assert keys == ["places-would-change"]
    with ops.ops_session() as s:
        s.add(ops.PlacesRun(at=now, status="failed", kind="cycle", message="ConfigError: x"))
        s.add(ops.PlacesRun(at=now, status="dry_run", kind="manual", would_change=0))
    keys = [a.key for a in watchdog.check_places(now)]
    assert keys == ["places-failed"]
