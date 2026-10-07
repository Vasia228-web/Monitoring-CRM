"""Охоронець планів: запити сайту не проходять усю таблицю listings (Блок 2, E4–E5, D50).

Етап 0: сім повних проходів 94-МБ таблиці на кожен перегляд «/» (1,85 с на
Fedora). Тепер список id береться з покривного індексу ix_listings_visible, а
решта запитів теплого перегляду — за індексами. План залежить від версії SQLite
й від нових фільтрів (Блоки 3/4), а вивід при зміні плану лишається тим самим —
тому його перевіряє машина. Те саме на робочій базі — `cli.py db plans`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import ops  # noqa: E402
from realty.analytics import cache, objects, segments  # noqa: E402
from realty.models import Base, Condition, Listing, MarketType, Property  # noqa: E402
from realty.web.app import app  # noqa: E402

NOW = datetime(2026, 10, 7, 8, 0, 0)


def _mod(name):
    import importlib
    return importlib.import_module(name)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'plans.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    sources = ["domria", "olx", "lun", "rieltor", "flombu"]
    with Session() as s:
        for pid in range(1, 151):
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=1 + pid % 4, area_total=50.0))
        s.flush()
        for i in range(1, 401):
            s.add(Listing(
                id=i, source=sources[i % 5], external_id=str(i),
                original_url=f"https://example.test/{i}", price_usd=30_000.0 + 137 * i,
                price_per_sqm=600.0 + i, rooms=1 + i % 4, area_total=40.0 + i % 30,
                market_type=[MarketType.PRIMARY, MarketType.SECONDARY][i % 2],
                condition=[Condition.RENOVATED, Condition.NEEDS_REPAIR][i % 2],
                published_at=NOW - timedelta(days=i % 90), first_seen=NOW - timedelta(days=30),
                last_seen=NOW, quality_status="ok" if i % 7 else "review",
                is_active=bool(i % 11), manual_active=False if i % 37 == 0 else None,
                in_progress=i % 13 == 0, property_id=1 + i % 150 if i % 9 else None,
                last_checked=NOW - timedelta(hours=i % 48)))
        s.commit()
    return engine, Session


def test_list_queries_plan_guard(db):
    """8 сортувань × 9 фільтрів: лише покривні індекси; нуль проходів таблиці."""
    plans = _mod("realty.web.plans")
    engine, Session = db
    with Session() as s:
        report = plans.report(s, NOW)
    bad = [(r["label"], r["bad"]) for r in report if r["bad"]]
    assert not bad, bad
    lists = [r for r in report if r["kind"] == "список"]
    assert len(lists) == 8 * len(plans.LIST_FILTERS)
    for r in lists:
        text_ = " | ".join(r["plan"])
        assert "COVERING INDEX ix_listings_visible" in text_ \
            or "COVERING INDEX ix_listings_in_progress" in text_, (r["label"], text_)


def test_the_guard_catches_a_table_scan(db):
    """Перевірка самого охоронця: без індексів міграції ті самі запити — «погані»."""
    from sqlalchemy import text

    plans = _mod("realty.web.plans")
    engine, Session = db
    with engine.begin() as conn:
        for name in ("ix_listings_visible", "ix_listings_keeper", "ix_listings_in_progress",
                     "ix_listings_last_checked", "ix_listings_last_seen"):
            conn.execute(text(f'DROP INDEX "{name}"'))
    with Session() as s:
        report = plans.report(s, NOW)
    assert sum(1 for r in report if r["bad"]) > 20


def test_no_full_listings_scan_per_request(db, tmp_path, monkeypatch):
    """Теплі GET «/», «/?page=2», «В обробці», «Аналітика», квартира: жодного проходу
    таблиці listings і жодної перевірки «версії» знімка повним проходом."""
    plans = _mod("realty.web.plans")
    engine, Session = db
    import realty.web.analytics_routes as routes
    import realty.web.app as appmod
    for mod in (routes, appmod):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(ops_engine)
    for var in ("AUTH_USER", "AUTH_PASSWORD", "FRIEND_USER", "FRIEND_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    for mod in (segments, objects):
        monkeypatch.setattr(mod, "_now", lambda: NOW)
    cache.invalidate()
    seen: list[tuple[str, list[str]]] = []

    def before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            rows = cursor.connection.execute("EXPLAIN QUERY PLAN " + statement,
                                             params or ()).fetchall()
            seen.append((" ".join(statement.split()), [r[3] for r in rows]))

    client = TestClient(app)
    urls = ["/", "/?page=2", "/processing", "/analytics", "/property/5?verify=0"]
    for url in urls:
        assert client.get(url).status_code == 200, url       # прогрів
    event.listen(engine, "before_cursor_execute", before)
    try:
        for url in urls:
            assert client.get(url).status_code == 200, url
    finally:
        event.remove(engine, "before_cursor_execute", before)
    cache.invalidate()
    scans = [(sql[:120], line) for sql, plan in seen for line in plan if plans.is_bad(line)]
    assert not scans, scans
    version_checks = [sql for sql, _ in seen
                      if "count(listings.id)" in sql and "max(listings.last_seen)" in sql]
    assert not version_checks, version_checks
