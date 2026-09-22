"""Правила-вето зведення (D41): кожне окремо, рішення власника, «не впевнений — не зливай».

Сценарії — з ручної вибірки 20 квартир із 8+ оголошеннями (22.09), де у 12
було злито кілька різних квартир.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from realty.dedup import RULES, Manual, Shape, active_rules, cluster, concurrent_gap, rebuild
from realty.models import (
    Base, Condition, DedupDecision, Listing, MarketType, Property,
)

NOW = datetime(2026, 9, 22)


def _sh(i, source="domria", rooms=1, area=43.5, floor=8, street="манхеттен", house=("34",),
        price=85500.0, url=None, **kw):
    kw.setdefault("start", NOW - timedelta(days=10))
    kw.setdefault("end", NOW)
    return Shape(i, source, url or f"https://{source}/{i}", rooms, area, floor, street,
                 frozenset(house or ()), None, price, **kw)


def _together(groups, *ids):
    return any(set(ids) <= set(g) for g in groups)


def _apart(groups, a, b):
    return not _together(groups, a, b)


# --- корпус ------------------------------------------------------------------------

def test_korpus_keeps_buildings_of_one_complex_apart_even_through_a_bridge():
    """/property/24: «34 корпус 9», «34/7» і «34» зводились в одне через спільне «34»."""
    k9 = _sh(1, house=("34", "9"), korpus="34к9")
    k7 = _sh(2, house=("34", "7"), korpus="34к7")
    bare = _sh(3, house=("34",))                       # корпус невідомий — не протиріччя
    assert _together(cluster([k9, k7, bare]), 1, 2, 3)            # як було до D41
    groups = cluster([k9, k7, bare], {"korpus"})
    assert _apart(groups, 1, 2)                                   # вето на всю групу
    assert _together(groups, 3, 1) or _together(groups, 3, 2)     # «34» — до однієї з них


def test_korpus_unknown_on_one_side_is_not_a_contradiction():
    assert _together(cluster([_sh(1, korpus="34к9"), _sh(2)], {"korpus"}), 1, 2)


# --- id квартири DIM.RIA ------------------------------------------------------------

def test_same_ria_flat_id_merges_despite_noisy_area_and_different_ids_split():
    """/property/3007: 12 агентів, один id квартири, площа від 40,45 до 41,3 м²."""
    a = _sh(1, area=40.45, flat="ria:bc5b9e")
    b = _sh(2, area=41.3, flat="ria:bc5b9e")
    assert _apart(cluster([a, b]), 1, 2)                          # 0,85 м² — старе правило ні
    assert _together(cluster([a, b], {"ria_flat"}), 1, 2)
    twin = _sh(3, area=40.45, flat="ria:e5b471")                  # /property/3238, оголошення 5300
    assert _apart(cluster([a, twin], {"ria_flat"}), 1, 3)


# --- ціна одночасних ----------------------------------------------------------------

def test_price_gap_counts_only_for_listings_that_hung_at_the_same_time():
    early = _sh(1, price=70000.0, start=NOW - timedelta(days=60), end=NOW - timedelta(days=30))
    later_cheaper = _sh(2, price=62000.0, start=NOW - timedelta(days=20))
    concurrent = _sh(3, price=62000.0, start=NOW - timedelta(days=50))
    assert concurrent_gap(early, later_cheaper) is None           # зняли → з'явилось дешевше
    assert concurrent_gap(early, concurrent) > 0.10
    assert _together(cluster([early, later_cheaper], {"price"}), 1, 2)
    assert _apart(cluster([early, concurrent], {"price"}), 1, 3)


def test_price_is_compared_at_the_moment_both_were_up_not_today():
    """Ціну міряємо на момент, коли обидва висіли, — за історією змін."""
    a = _sh(1, price=60000.0, prices=((NOW - timedelta(days=10), 70000.0),
                                      (NOW - timedelta(days=1), 60000.0)),
            end=NOW - timedelta(days=5))                          # знято до зниження ціни
    b = _sh(2, price=69000.0)
    assert concurrent_gap(a, b) < 0.02


# --- стан, координати, будинок ------------------------------------------------------

def test_condition_renovated_vs_needs_repair_is_a_veto_unknown_is_not():
    a = _sh(1, condition=Condition.RENOVATED)
    assert _apart(cluster([a, _sh(2, condition=Condition.NEEDS_REPAIR)], {"condition"}), 1, 2)
    assert _together(cluster([a, _sh(3, condition=Condition.UNKNOWN)], {"condition"}), 1, 3)


def test_far_precise_coordinates_veto_but_approximate_never_do():
    a = _sh(1, lat=48.9210, lon=24.6883, geo="point")
    far = _sh(2, lat=48.9300, lon=24.6883, geo="point")           # ~1 км
    near = _sh(3, lat=48.9211, lon=24.6884, geo="point")
    olx = _sh(4, source="olx", lat=48.9500, lon=24.7, geo="approx")
    groups = cluster([a, far, near, olx], {"geo"})
    assert _apart(groups, 1, 2) and _together(groups, 1, 3) and _together(groups, 1, 4)


def test_building_ids_compare_only_within_one_source():
    a = _sh(1, building="ria:6981afa0")
    b = _sh(2, building="ria:623b0ce3")
    lun = _sh(3, source="lun", building="lun:10886945")
    assert _apart(cluster([a, b], {"building"}), 1, 2)
    assert _together(cluster([a, lun], {"building"}), 1, 3)


# --- без ланцюжків ------------------------------------------------------------------

def test_no_chain_stops_two_floors_meeting_through_a_listing_without_floor():
    """A (5 поверх) ≈ B (поверх не вказано) ≈ C (6 поверх), але A і C — різні квартири."""
    a, bridge, c = _sh(1, floor=5), _sh(2, floor=None), _sh(3, floor=6)
    assert _together(cluster([a, bridge, c]), 1, 2, 3)            # ланцюжок до D41
    groups = cluster([a, bridge, c], {"no_chain"})
    assert _apart(groups, 1, 3)


def test_no_chain_leaves_a_tied_listing_without_address_alone():
    """Безадресне оголошення OLX однаково пасує до двох квартир — вибір був би навмання."""
    a = _sh(1, street="мазепи", house=("168",))
    b = _sh(2, street="вовчинецька", house=("223",))
    olx = _sh(3, source="olx", street=None, house=())
    legacy = cluster([a, b, olx])
    assert _together(legacy, 3, 1) or _together(legacy, 3, 2)
    groups = cluster([a, b, olx], {"no_chain"})
    assert [3] in groups


# --- новобудова ---------------------------------------------------------------------

def test_newbuild_listing_without_id_that_fits_two_flats_stays_alone():
    """Однакові квартири на тому самому поверсі в різних секціях."""
    f1 = _sh(1, flat="ria:aaa", primary=True)
    f1b = _sh(2, flat="ria:aaa", primary=True)
    f2 = _sh(3, flat="ria:bbb", primary=True)
    olx = _sh(4, source="olx", street=None, house=(), primary=True)
    groups = cluster([f1, f1b, f2, olx], {"ria_flat", "newbuild"})
    assert _together(groups, 1, 2) and _apart(groups, 1, 3) and [4] in groups


def test_newbuild_listing_that_fits_exactly_one_flat_joins_it():
    """/property/232: 11 агентів DIM.RIA з одним id + OLX і LUN тієї ж квартири."""
    ria = [_sh(i, flat="ria:4749af", primary=True, area=98.0, floor=6, price=195000.0)
           for i in range(1, 4)]
    olx = _sh(9, source="olx", street=None, house=(), primary=True, area=98.0, floor=6,
              price=194918.43)
    assert _together(cluster([*ria, olx], set(RULES)), 1, 2, 3, 9)


# --- рішення власника ---------------------------------------------------------------

def test_owner_says_different_beats_even_the_same_link():
    a, b = _sh(1, url="https://x/1"), _sh(2, source="lun", url="https://x/1")
    assert _together(cluster([a, b]), 1, 2)
    manual = Manual([("different", [1], [2])])
    assert _apart(cluster([a, b], (), manual), 1, 2)


def test_owner_says_same_beats_every_rule():
    a = _sh(1, korpus="34к9", condition=Condition.RENOVATED)
    b = _sh(2, korpus="34к7", condition=Condition.NEEDS_REPAIR, floor=3)
    manual = Manual([("same", [1], [2])])
    assert _together(cluster([a, b], set(RULES), manual), 1, 2)


def test_production_rules_are_off_until_the_owner_decides(monkeypatch):
    monkeypatch.delenv("DEDUP_RULES", raising=False)
    assert active_rules() == frozenset()
    monkeypatch.setenv("DEDUP_RULES", "korpus, ria_flat,nonsense")
    assert active_rules() == {"korpus", "ria_flat"}
    monkeypatch.setenv("DEDUP_RULES", "all")
    assert active_rules() == set(RULES)


# --- перебудова з рішенням власника -------------------------------------------------

@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _l(i, **kw):
    base = dict(source="domria", external_id=str(i), original_url=f"https://d/{i}",
                price=65000, currency="USD", price_usd=65000.0, rooms=2, area_total=55.0,
                floor=5, floors_total=9, location="вул. Незалежності, 146",
                market_type=MarketType.SECONDARY, condition=Condition.RENOVATED,
                first_seen=NOW, last_seen=NOW, quality_status="ok")
    base.update(kw)
    return Listing(**base)


def test_owner_decision_survives_rebuilds(Session):
    with Session() as s:
        s.add_all([_l(1), _l(2), _l(3)])
        s.commit()
        rebuild(s, rules=())
        s.commit()
        assert len({l.property_id for l in s.scalars(select(Listing))}) == 1
        s.add(DedupDecision(kind="different", left=[3], right=[1, 2]))
        s.commit()
    for _ in range(2):
        with Session() as s:
            rebuild(s, rules=())
            s.commit()
    with Session() as s:
        pid = {l.id: l.property_id for l in s.scalars(select(Listing))}
        assert pid[1] == pid[2] != pid[3]
        assert s.scalar(select(Property).where(Property.id == pid[3])) is not None
