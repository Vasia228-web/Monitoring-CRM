"""Зведення квартир і район/ЖК (Блок 4, E10, D57): кластери ті самі, row_* — значення
квартири після перебудови, «розділити» й «злити»; нові види перевірки зведення."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import places_kit as kit  # noqa: E402
from realty import dedup, dedup_audit  # noqa: E402
from realty.models import Listing, Property  # noqa: E402
from realty.places import directory  # noqa: E402

RULES = ("ria_flat", "korpus", "geo", "condition", "newbuild", "price")   # варіант E (D44)


@pytest.fixture(autouse=True)
def _fresh_directory():
    dedup._place_directory.__dict__.pop("value", None)
    yield
    dedup._place_directory.__dict__.pop("value", None)


@pytest.fixture
def db(tmp_path):
    engine = kit.make_engine(tmp_path)
    kit.build(engine, n=200)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _mapping(Session) -> dict:
    with Session() as s:
        return dict(s.execute(select(Listing.id, Listing.property_id)).all())


def _rows_match_properties(Session) -> None:
    with Session() as s:
        props = {p.id: (p.district_key, p.complex_key, p.place_area)
                 for p in s.scalars(select(Property))}
        for l in s.scalars(select(Listing)):
            want = (props[l.property_id] if l.property_id is not None
                    else (l.district_key, l.complex_key, l.place_area))
            assert (l.row_district, l.row_complex, l.row_area) == want, l.id


def test_rebuild_clusters_unchanged_and_rows_synced(db):
    engine, Session = db
    with kit.scope_for(Session) as s:
        dedup.rebuild(s, rules=RULES)
    without = _mapping(Session)
    before = kit.raw_checksum(engine)
    kit.assign(engine)
    with kit.scope_for(Session) as s:
        st = dedup.rebuild(s, rules=RULES)
    # Ключі місця правил зведення не змінюють: ті самі квартири, жодне оголошення не
    # переїхало, last_seen і сирі поля ті самі.
    assert _mapping(Session) == without and st["listings_moved"] == 0
    assert kit.raw_checksum(engine) == before
    _rows_match_properties(Session)
    # Квартира — з ключів її оголошень (places.resolve.property_place).
    from realty.places.resolve import property_place

    d = directory.load()
    with Session() as s:
        for p in s.scalars(select(Property)):
            members = [(l.district_key, l.district_how, l.complex_key, l.complex_how)
                       for l in p.listings]
            assert (p.district_key, p.complex_key, p.place_area) == \
                property_place(members, d)[:3], p.id


def test_split_and_merge_keep_rows_in_sync(db):
    engine, Session = db
    kit.assign(engine)            # квартири набору (1–3 оголошення) отримали район і ЖК
    _rows_match_properties(Session)
    with Session() as s:
        multi = [p for p in s.scalars(select(Property).order_by(Property.id))
                 if len(p.listings) >= 2]
        # Квартира, оголошення якої мають різні ключі місця (якщо є), — щоб поділ щось
        # змінив; інакше — будь-яка з кількох оголошень.
        target = next((p for p in multi
                       if len({(l.district_key, l.complex_key) for l in p.listings}) > 1),
                      multi[0])
        pid, lids = target.id, sorted(l.id for l in target.listings)
        other = next(p.id for p in multi if p.id != pid)
    with kit.scope_for(Session) as s:
        new_pid = dedup.split_off(s, pid, lids[:1])
    _rows_match_properties(Session)
    with kit.scope_for(Session) as s:
        dedup.merge_into(s, new_pid, other)
    _rows_match_properties(Session)


def _shape(i, complex_key):
    return SimpleNamespace(flat=None, korpus=None, building=None, osm=None, area=50.0,
                           floor=3, condition=None, lat=None, lon=None, geo=None,
                           start=None, end=None, prices=(), price=None, complex=complex_key)


def test_different_complexes_flagged():
    d = directory.load()
    c = lambda a, b: dedup_audit.contradictions(_shape(1, a), _shape(2, b), d)  # noqa: E731
    assert c("shepit", "skygarden") == {"complex"}
    assert c("family-plaza", "family-plaza-2") == {"complex_phase"}
    assert c("kniahynyn", "kniahynyn-center") == set()          # парасолька
    assert c(None, "shepit") == set()                           # «не в ЖК» Shape не несе


def test_audit_counts_suspicious_core_without_new_kinds(db):
    engine, Session = db
    with Session() as s:
        s.add(Property(id=9001, fingerprint="x", rooms=2, area_total=50.0,
                       first_seen=kit.NOW, last_seen=kit.NOW))
        s.flush()
        for i, (cx, ria) in enumerate((("ЖК SHEPIT", 6420), ("ЖК SKYGARDEN", 11061))):
            s.add(kit.listing(9100 + i, 9001, "domria", complex_name=cx,
                              identity={"complex": f"ria:{ria}"}, floor=3, area_total=50.0,
                              rooms=2, condition=kit.Condition.RENOVATED))
        s.commit()
    kit.assign(engine)
    with Session() as s:
        res = dedup_audit.audit(s)
    assert res["by_kind"].get("complex", 0) >= 1
    q = next(q for q in res["queue"] if q["property_id"] == 9001)
    assert q["kinds"] == ["complex"]
    # suspicious_core рахує лише «старі» види (rules.audit.core_kinds).
    core_only = sum(1 for q in res["queue"]
                    if set(q["kinds"]) - {"complex", "complex_phase"})
    assert res["suspicious_core"] == core_only < res["suspicious"]
