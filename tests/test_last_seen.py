"""«Коли бачили» означає «бачили у стрічці джерела», а не «щось записало рядок».

До 23.09.2026 у моделі стояв `onupdate`: дату оновлював будь-який запис —
контроль якості, перевірка актуальності, перебудова квартир. Наслідок на
робочій базі: всі 2 681 зняті оголошення мали «бачили» ПІСЛЯ дати зняття, а
діагностика «давно не бачили» не знаходила жодного оголошення.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from realty.models import Base, Condition, Listing, MarketType
from realty.pipeline import Pipeline

OLD = datetime(2026, 9, 1, 12, 0)


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _seed(Session):
    with Session() as s:
        s.add(Listing(source="domria", external_id="1", original_url="https://d/1",
                      price=60000, currency="USD", price_usd=60000.0, rooms=2,
                      area_total=55.0, floor=5, location="вул. Мазепи, 1",
                      market_type=MarketType.SECONDARY, condition=Condition.RENOVATED,
                      quality_status="ok", first_seen=OLD, last_seen=OLD))
        s.commit()


def test_quality_and_verify_updates_do_not_pretend_we_saw_the_listing(Session):
    _seed(Session)
    with Session() as s:
        row = s.scalar(select(Listing))
        row.quality_status = "review"                  # контроль якості
        row.last_checked = datetime(2026, 9, 23)       # перевірка актуальності
        row.last_alive_at = datetime(2026, 9, 23)
        row.views = 5                                  # хтось відкрив картку
        s.commit()
        assert s.scalar(select(Listing)).last_seen == OLD


def test_delisted_listing_keeps_the_date_we_last_saw_it(Session):
    _seed(Session)
    with Session() as s:
        row = s.scalar(select(Listing))
        row.is_active = False
        row.delisted_at = datetime(2026, 9, 20)
        s.commit()
        row = s.scalar(select(Listing))
        assert row.last_seen == OLD and row.last_seen < row.delisted_at


def test_collection_does_set_the_date(Session):
    _seed(Session)
    rec = {"source": "domria", "external_id": "1", "original_url": "https://d/1",
           "price": 61000, "currency": "USD", "price_usd": 61000.0, "rooms": 2,
           "area_total": 55.0, "floor": 5, "location": "вул. Мазепи, 1",
           "market_type": MarketType.SECONDARY, "condition": Condition.RENOVATED}
    with Session() as s:
        assert Pipeline._upsert(s, rec) == "updated"
        s.commit()
        row = s.scalar(select(Listing))
    assert row.last_seen > OLD and row.price_usd == 61000.0
