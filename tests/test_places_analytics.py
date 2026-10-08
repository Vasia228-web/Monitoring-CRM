"""«Схожі» квартири за нормалізованим районом (Блок 4, E10, D57).

До E10 сегменти брали сирий Property.district: у LUN там найближчий POI («ТЦ Арсен»),
тож квартири порівнювались із «схожими» біля того самого ТЦ (Етап 0: 3 370), а
«Пасiчна» з латинською «i» й «Пасічна» були різними кошиками.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import places_kit as kit  # noqa: E402
from realty.analytics import inventory, segments  # noqa: E402
from realty.analytics.settings import load as analytics_settings  # noqa: E402
from realty.models import Condition, MarketType, Property  # noqa: E402

NOW = datetime(2026, 10, 7, 8, 0, 0)


def _prop(pid, raw, key, area=50.0):
    p = Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=area,
                 price_usd_min=50_000.0, price_per_sqm=1000.0 + pid,
                 condition=Condition.RENOVATED, market_type=MarketType.SECONDARY,
                 district=raw, first_seen=NOW - timedelta(days=30), last_seen=NOW)
    p._test_key = key
    return p


def _set_keys(engine, props) -> None:
    """district_key — прямим UPDATE (і колонкою, якщо її ще немає): так тест іде через
    публічний build_items/ladder і на коді до E10 падає на поведінці, а не на моделі."""
    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        if "district_key" not in {c["name"] for c in inspect(conn).get_columns("properties")}:
            conn.execute(text("ALTER TABLE properties ADD COLUMN district_key VARCHAR(48)"))
        for p in props:
            conn.execute(text("UPDATE properties SET district_key = :k WHERE id = :id"),
                         {"k": p._test_key, "id": p.id})


def test_ladder_uses_normalized_district(tmp_path):
    engine = kit.make_engine(tmp_path)
    kit.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    props = [_prop(1, "Пасiчна", "pasichna"), _prop(2, "Пасічна", "pasichna"),
             _prop(3, 'ТЦ "Арсен"', None), _prop(4, 'ТЦ "Арсен"', None)]
    with Session() as s:
        s.add_all(props)
        s.flush()
        for pid in (1, 2, 3, 4):
            s.add(kit.listing(pid, pid, "lun", rooms=2, area_total=50.0,
                              market_type=MarketType.SECONDARY, condition=Condition.RENOVATED,
                              is_active=True, quality_status="ok"))
        s.commit()
    _set_keys(engine, props)
    with Session() as s:
        items = {i.property_id: i for i in segments.build_items(s, None, now=NOW)}
    assert items[1].district == items[2].district == "pasichna"
    cfg = analytics_settings()
    levels = {lv.key: lv for lv in segments.ladder(items[1], cfg)}
    assert "district" in levels and "Пасічна" in levels["district"].label
    assert levels["district"].match(items[2]) and not levels["district"].match(items[3])
    # POI — не район: щабля «район» немає, і «ТЦ» у підписах немає.
    poi = segments.ladder(items[3], cfg)
    assert not any(lv.key.startswith("district") for lv in poi)
    assert not any("ТЦ" in lv.label for lv in poi)


def test_inventory_counts_normalized_district(tmp_path):
    engine = kit.make_engine(tmp_path)
    kit.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    props = [_prop(1, "Пасiчна", "pasichna"), _prop(2, 'ТЦ "Арсен"', None)]
    with Session() as s:
        s.add_all(props)
        s.commit()
    _set_keys(engine, props)
    with Session() as s:
        seg = inventory.segments(s, min_size=1)
        assert seg["with_district"]["total"] == 1
        assert inventory.masters(s)["with_district"] == 1
