"""Нові індекси Блоку 2 не змінюють того, що бачить людина (D48).

Прототип Блоку 2 показав: щойно в базі з'являється покривний індекс, SQLite
віддає ті самі рядки в ІНШОМУ порядку — і в порівнянні джерел позначка
«найдешевше» перескочила між двома джерелами з рівною медіаною. Порядок
рядків без ORDER BY — не гарантія, а збіг плану. Тому скрізь, де від порядку
залежить вивід, він тепер заданий явно (за id — так, як було досі).

Тест — до самих індексів (крок E4): на синтетичній базі з планом «як на
робочій» (без шести застарілих індексів моделі) рахуємо вивід, додаємо
індекси, заплановані Блоком 2, і рахуємо знову. Має збігтися все: порівняння
джерел, склад джерел, знімок аналітики, сторінка квартири (порядок рядків),
історія ціни, поля квартири після «розділити» і «злити».

Дві фази. Перша — лише нові індекси: SQLite і далі бере ix_listings_property_id
для WHERE property_id = ?, а той віддає рядки за id — тож ця фаза ловить лише
запити по всій таблиці (порівняння й склад джерел). Друга — ще й без
ix_listings_property_id: тепер WHERE property_id = ? обслуговує індекс
keeper (property_id, якість, …), і рядки квартири приходять за якістю, а не за
id. Саме вона ловить сторінку квартири, «розділити»/«злити» й нічиї в історії
ціни (у квартири 1 оголошення різної якості й події з однаковим часом).
Окремо перевірено, що в самих запитах стоїть ORDER BY за id: інакше збіг
виводу міг би бути випадковим.

Індекси (крок E4, D50) — з моделі через саму міграцію `db.migrate()`: база
фікстури — «як робоча до E4» (без шести застарілих і без п'яти нових), а тест
застосовує ту саму міграцію, що й `cli.py db migrate`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event  # noqa: E402

from realty import db as dbmod, dedup, ops  # noqa: E402
from realty.analytics import cache, objects, segments, sources  # noqa: E402
from realty.analytics.settings import Settings  # noqa: E402
from realty.models import Base, Condition, Listing, MarketType, PriceEvent, Property  # noqa: E402
from realty.web.app import app  # noqa: E402

NOW = datetime(2026, 10, 7, 8, 0, 0)
CFG = Settings()

# План Блоку 2 (D48): 5 нових індексів listings — їх створює db.migrate() з моделі.
PLANNED = ["ix_listings_in_progress", "ix_listings_keeper", "ix_listings_last_checked",
           "ix_listings_last_seen", "ix_listings_visible"]


def _listing(i: int, pid: int, **kw) -> Listing:
    base = dict(source="domria", external_id=str(i), original_url=f"https://example.test/{i}",
                price=50_000, currency="USD", price_usd=50_000.0, rooms=2, area_total=50.0,
                price_per_sqm=1000.0, floor=5, floors_total=9, location="вул. Стуса, 30",
                market_type=MarketType.SECONDARY, condition=Condition.RENOVATED,
                published_at=NOW - timedelta(days=40), first_seen=NOW - timedelta(days=30),
                last_seen=NOW, quality_status="ok", property_id=pid)
    base.update(kw)
    return Listing(id=i, **base)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'order.db'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:                       # план «як на робочій базі» до E4
        for name in PLANNED + list(dbmod.OBSOLETE_INDEXES):
            conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        pids = [1] + list(range(100, 130)) + list(range(200, 230))
        for pid in pids:
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=50.0,
                           price_usd_min=50_000.0, price_per_sqm=1000.0,
                           first_seen=NOW - timedelta(days=30), last_seen=NOW))
        s.flush()
        # Квартира 1: рівні ціни; ознаки, де «найчастіше» — нічия (поле бере
        # перше в порядку учасників); якість різна, тож індекс keeper упорядкував
        # би їх інакше, ніж id.
        s.add_all([
            _listing(1, 1, floor=3, quality_status="review", price_per_sqm=None),
            _listing(2, 1, floor=7, source="olx", price_per_sqm=None),
            _listing(3, 1, floor=5, source="lun", price_per_sqm=None),
        ])
        # Історія ціни квартири 1 з НІЧИЯМИ за часом між оголошеннями різної
        # якості: від порядку рівних залежать перша й остання точки й «зміна, %».
        t0, t1, t2 = NOW - timedelta(days=20), NOW - timedelta(days=10), NOW - timedelta(days=2)
        s.add_all([
            PriceEvent(id=1, listing_id=1, source="domria", price=50_000, price_usd=50_000.0,
                       observed_at=t0),
            PriceEvent(id=2, listing_id=2, source="olx", price=48_000, price_usd=48_000.0,
                       observed_at=t0),
            PriceEvent(id=3, listing_id=1, source="domria", price=47_000, price_usd=47_000.0,
                       observed_at=t1),
            PriceEvent(id=4, listing_id=1, source="domria", price=46_000, price_usd=46_000.0,
                       observed_at=t2),
            PriceEvent(id=5, listing_id=3, source="lun", price=45_000, price_usd=45_000.0,
                       observed_at=t2),
        ])
        # Порівняння джерел: OLX і DIM.RIA з ОДНАКОВОЮ медіаною в одному сегменті.
        # Рядки OLX мають менші id, але більші id квартир — покривний індекс
        # (…, property_id, …) поставив би DIM.RIA першим.
        s.add_all([_listing(10 + k, 200 + k, source="olx") for k in range(30)])
        s.add_all([_listing(50 + k, 100 + k, source="domria") for k in range(30)])
        s.commit()

    import realty.web.analytics_routes as routes
    monkeypatch.setattr(routes, "SessionLocal", Session)
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(ops_engine)
    for mod in (segments, objects):
        monkeypatch.setattr(mod, "_now", lambda: NOW)
    for var in ("AUTH_USER", "AUTH_PASSWORD", "FRIEND_USER", "FRIEND_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    cache.invalidate()
    yield engine, Session
    cache.invalidate()


def _flat(prop: Property) -> dict:
    return {c: getattr(prop, c) for c in ("rooms", "area_total", "floor", "floors_total",
                                          "street", "house", "district", "location",
                                          "price_usd_min", "price_usd_max", "price_per_sqm",
                                          "market_type", "condition", "sources_count")}


def _outputs(Session) -> dict:
    cache.invalidate()
    out = {}
    with Session() as s:
        out["composition"] = sources.composition(s)
        out["matched"] = sources.matched(s, CFG)
        out["universe"] = [vars(i) for i in segments.build_universe(s).items]
        out["price_history"] = objects.price_history(s, 1)
        out["cross_source"] = objects.cross_source(s, 1)
    with TestClient(app) as client:
        r = client.get("/property/1?verify=0")
        assert r.status_code == 200, r.text[:300]
        out["property_page"] = r.text
    with Session() as s:                    # «розділити» — і відкотити
        new_pid = dedup.split_off(s, 1, [3])
        out["split"] = (_flat(s.get(Property, 1)), _flat(s.get(Property, new_pid)))
        s.rollback()
    with Session() as s:                    # «злити» — і відкотити
        kept = dedup.merge_into(s, 1, 100)
        out["merge"] = _flat(s.get(Property, kept))
        s.rollback()
    cache.invalidate()
    return out


def _plan(conn, sql: str) -> str:
    return " ".join(r[3] for r in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql))


def test_planned_indexes_change_plans_but_not_outputs(db):
    engine, Session = db
    before = _outputs(Session)
    rep = dbmod.migrate(bind=engine)
    assert sorted(rep.indexes_created) == PLANNED, rep
    with engine.begin() as conn:
        # Перевірка самого тесту: індекс справді змінює план запиту джерел —
        # інакше тест нічого б не доводив.
        plan = _plan(conn, "SELECT source, rooms, condition, market_type, price_per_sqm "
                           "FROM listings WHERE quality_status = 'ok' AND price_per_sqm "
                           "IS NOT NULL AND rooms IS NOT NULL")
    assert "ix_listings_visible" in plan, plan
    after = _outputs(Session)
    for key in before:
        assert after[key] == before[key], f"«{key}» змінився після індексів Блоку 2"

    # Фаза 2: без індексу за property_id рядки квартири дає keeper — за якістю.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_listings_property_id"))
        plan = _plan(conn, "SELECT id FROM listings WHERE property_id = 1")
    assert "ix_listings_keeper" in plan, plan
    after_keeper = _outputs(Session)
    for key in before:
        assert after_keeper[key] == before[key], \
            f"«{key}» змінився, коли рядки квартири віддає індекс keeper"

    # І сам вивід такий, як досі (порядок id): найдешевшим за рівної медіани
    # лишається джерело, що трапилось першим у порядку id.
    assert before["matched"]["table"][0]["cheapest"] == "olx"
    assert [r["source"] for r in before["composition"]] == ["olx", "domria"]
    assert before["split"][0]["floor"] == 3          # перший із решти за id
    # Нічиї в історії ціни — за оголошенням: перша точка — оголошення 1, остання — 3.
    history = before["price_history"]
    assert (history["first"]["price"], history["last"]["price"]) == (50_000.0, 45_000.0)
    assert [p["listing_id"] for p in history["points"]] == [1, 2, 1, 1, 3]


def test_row_order_is_explicit_in_the_queries(db):
    """ORDER BY за id стоїть у самих запитах — збіг виводу не випадковість плану."""
    engine, Session = db
    seen: list[str] = []

    def capture(conn, cursor, statement, params, context, executemany):
        seen.append(" ".join(statement.split()))

    def run(fn) -> list[str]:
        seen.clear()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            with Session() as s:
                fn(s)
                s.rollback()
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        return [q for q in seen if q.startswith("SELECT")]

    def listing_queries_ordered(queries, where):
        mine = [q for q in queries if "FROM listings" in q and where in q]
        assert mine, f"немає запиту з «{where}»"
        for q in mine:
            assert "ORDER BY listings.id" in q, q

    listing_queries_ordered(run(segments.build_universe), "listings.property_id IS NOT NULL")
    listing_queries_ordered(run(lambda s: objects.cross_source(s, 1)),
                            "listings.property_id = ?")
    listing_queries_ordered(run(lambda s: dedup.split_off(s, 1, [3])),
                            "listings.property_id = ?")
    listing_queries_ordered(run(lambda s: dedup.merge_into(s, 1, 100)),
                            "listings.property_id = ?")
    (history,) = [q for q in run(lambda s: objects.price_history(s, 1))
                  if "FROM price_events" in q]
    assert history.endswith("ORDER BY price_events.observed_at, price_events.listing_id, "
                            "price_events.id"), history
