"""id квартир стабільні між перебудовами; злиття — переадресація старого id.

До 22.09.2026 перебудова видаляла всі квартири й нумерувала заново з 1: за
півтори доби 89% оголошень опинились в іншій квартирі за номером, збережені
посилання показували чужі квартири, скарги вказували не туди.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from realty.dedup import assign_ids, rebuild, resolve_property_id
from realty.models import (
    Base, Condition, DataReport, Listing, MarketType, Property, PropertyRedirect,
)


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _on(conn, _r):
        conn.execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _l(i, source, **kw):
    now = datetime(2026, 9, 22)
    base = dict(source=source, external_id=str(i), original_url=f"https://{source}/{i}",
                price=65000, currency="USD", price_usd=65000.0, rooms=2, area_total=55.0,
                floor=5, floors_total=9, location="вул. Незалежності, 146",
                market_type=MarketType.SECONDARY, condition=Condition.RENOVATED,
                first_seen=now, last_seen=now, quality_status="ok")
    base.update(kw)
    return Listing(**base)


FLATS = [  # (source, кімнат, площа, поверх, адреса, ціна) — три різні квартири
    ("domria", 2, 55.0, 5, "вул. Незалежності, 146", 65000),
    ("lun",    2, 55.0, 5, "вул. Незалежності, 146", 65000),     # = перша
    ("olx",    1, 38.0, 2, "вул. Стуса, 30", 41000),
    ("domria", 3, 82.0, 8, "вул. Мазепи, 164", 99000),
    ("flombu", 3, 82.0, 8, "вул. Мазепи, 164", 99000),            # = четверта
]


def _seed(Session):
    with Session() as s:
        for i, (src, rooms, area, floor, loc, price) in enumerate(FLATS, 1):
            s.add(_l(i, src, rooms=rooms, area_total=area, floor=floor, location=loc,
                     price=price, price_usd=float(price)))
        s.commit()
        rebuild(s)
        s.commit()


def _pids(s):
    return {l.id: l.property_id for l in s.scalars(select(Listing))}


def test_two_rebuilds_without_new_data_keep_every_id(Session):
    _seed(Session)
    with Session() as s:
        before = _pids(s)
        props_before = sorted(s.scalars(select(Property.id)))
    for _ in range(2):
        with Session() as s:
            rebuild(s)
            s.commit()
    with Session() as s:
        assert _pids(s) == before
        assert sorted(s.scalars(select(Property.id))) == props_before
    assert before[1] == before[2] and before[4] == before[5]        # склеєні як треба


def test_new_listings_do_not_shift_anyone_elses_id(Session):
    _seed(Session)
    with Session() as s:
        before = _pids(s)
        top = max(s.scalars(select(Property.id)))
        # Нове оголошення вже відомої квартири й нова квартира «посередині».
        joins = _l(10, "olx", rooms=2, area_total=55.0, floor=5,
                   location="вул. Незалежності, 146")
        fresh = _l(11, "olx", rooms=1, area_total=30.0, floor=1,
                   location="вул. Грушевського, 1", price=30000, price_usd=30000.0)
        s.add_all([joins, fresh])
        s.commit()
        j, f = joins.id, fresh.id
        rebuild(s)
        s.commit()
        after = _pids(s)
    assert {k: after[k] for k in before} == before                  # старі — на місці
    assert after[j] == before[1]                                    # приєдналось до своєї
    assert after[f] > top                                           # нова — новий номер


def test_processing_mark_and_report_survive_rebuild(Session):
    _seed(Session)
    with Session() as s:
        lid = 3
        pid = s.get(Listing, lid).property_id
        s.get(Listing, lid).in_progress = True
        s.add(DataReport(listing_id=lid, property_id=pid, field="price",
                         snapshot={"price_usd": 41000}))
        s.commit()
    for _ in range(2):
        with Session() as s:
            rebuild(s)
            s.commit()
    with Session() as s:
        row = s.get(Listing, lid)
        report = s.scalars(select(DataReport)).one()
        assert row.in_progress is True                              # позначка на місці
        assert row.property_id == pid                               # квартира та сама
        assert report.property_id == row.property_id                # скарга — на ту ж квартиру


def test_merged_flat_old_link_leads_to_the_one_that_remains(Session, monkeypatch):
    _seed(Session)
    with Session() as s:
        a, b = s.get(Listing, 3).property_id, s.get(Listing, 4).property_id
        s.add(DataReport(listing_id=4, property_id=b, field="gone", snapshot={}))
        # З'ясувалось, що оголошення 4 і 5 — та сама квартира, що й 3.
        for i in (4, 5):
            row = s.get(Listing, i)
            row.rooms, row.area_total, row.floor = 1, 38.0, 2
            row.location, row.price, row.price_usd = "вул. Стуса, 30", 41000, 41000.0
        s.commit()
        rebuild(s)
        s.commit()
        survivor = s.get(Listing, 3).property_id
        assert {s.get(Listing, i).property_id for i in (3, 4, 5)} == {survivor}
        gone = b if survivor == a else a
        assert s.get(Property, gone) is None
        assert s.get(PropertyRedirect, gone).new_id == survivor
        assert resolve_property_id(s, gone) == survivor
        report_pid = s.scalars(select(DataReport)).one().property_id

    # Стара сторінка (і посилання зі скарги) веде на квартиру, що лишилась.
    import realty.web.analytics_routes as routes
    monkeypatch.setattr(routes, "SessionLocal", Session)
    from realty.web.app import app
    r = TestClient(app, follow_redirects=False).get(f"/property/{report_pid}?verify=0")
    if report_pid != survivor:
        assert r.status_code == 302 and r.headers["location"] == f"/property/{survivor}?verify=0"


def test_retired_id_is_never_given_to_another_flat(Session):
    _seed(Session)
    with Session() as s:
        for i in (4, 5):                                            # злиття: один id зникає
            row = s.get(Listing, i)
            row.rooms, row.area_total, row.floor = 1, 38.0, 2
            row.location, row.price, row.price_usd = "вул. Стуса, 30", 41000, 41000.0
        s.commit()
        rebuild(s)
        s.commit()
        retired = s.scalars(select(PropertyRedirect.old_id)).one()
        extra = _l(20, "olx", rooms=4, area_total=120.0, floor=10, location="вул. Нова, 99",
                   price=150000, price_usd=150000.0)
        s.add(extra)
        s.commit()
        rebuild(s)
        s.commit()
        new = s.get(Listing, extra.id).property_id
    assert new != retired and new > retired


def test_split_keeps_the_id_for_the_larger_part():
    # Стара квартира 7 розпалась на групу з 3 оголошень і групу з 1.
    groups = [[1, 2, 3], [4]]
    ids = assign_ids(groups, {1: 7, 2: 7, 3: 7, 4: 7})
    assert ids == [7, None]


def test_rebuild_does_not_pretend_listings_were_seen(Session):
    """Перебудова не має міняти «коли бачили оголошення»: у моделі last_seen
    оновлюється при будь-якому UPDATE рядка, і стара перебудова так «освіжала»
    кожне оголошення, якому перенумерувала квартиру."""
    _seed(Session)
    with Session() as s:
        before = {l.id: l.last_seen for l in s.scalars(select(Listing))}
        for i in (4, 5):                                   # злиття → номер квартири зміниться
            row = s.get(Listing, i)
            row.rooms, row.area_total, row.floor = 1, 38.0, 2
            row.location, row.price, row.price_usd = "вул. Стуса, 30", 41000, 41000.0
        s.commit()
        touched = {l.id: l.last_seen for l in s.scalars(select(Listing))}
        rebuild(s)
        s.commit()
        after = {l.id: l.last_seen for l in s.scalars(select(Listing))}
    assert after == touched                                # перебудова — нуль змін last_seen


def test_report_points_to_the_flat_of_its_listing(monkeypatch, Session):
    """Скарга, записана до стабільних id, зберігає застарілий номер квартири —
    показуємо квартиру за оголошенням, а старий номер лишаємо як історію."""
    _seed(Session)
    with Session() as s:
        current = s.get(Listing, 3).property_id
        s.add(DataReport(listing_id=3, property_id=999, field="price", snapshot={}))
        s.commit()
    import realty.web.status as status
    monkeypatch.setattr(status, "SessionLocal", Session)
    from realty.web.app import app
    item = TestClient(app).get("/api/status/reports").json()["items"][0]
    assert item["property_id"] == current and item["property_id_at_report"] == 999
