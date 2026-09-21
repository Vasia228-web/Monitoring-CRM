"""Запис у СПРАВЖНЮ схему бази (копію, яку дає conftest).

20.09.2026 я ввімкнув PRAGMA foreign_keys, і запис нових оголошень та змін цін
падав добу: у схемі `price_events` лишилось посилання на `listings_legacy` від
старої міграції. Тести тоді проходили, бо будували схему з нуля. Цей тест
пише в реальну схему — і впаде, якщо щось знову зламає запис.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import func, select

from realty.db import SessionLocal, engine, init_db
from realty.models import Listing, PriceEvent
from realty.pipeline import Pipeline


def _has_real_data() -> bool:
    try:
        with SessionLocal() as s:
            return (s.scalar(select(func.count()).select_from(Listing)) or 0) > 0
    except Exception:
        return False


@pytest.mark.skipif(not _has_real_data(), reason="немає копії робочої бази")
def test_new_listing_and_price_change_are_written_into_the_real_schema():
    assert "realty-tests-" in str(engine.url), "тест мусить писати в копію, а не в робочу базу"
    init_db()
    rec = {"source": "olx", "external_id": "TEST-SCHEMA-1",
           "original_url": "https://example.test/schema-1",
           "price": 60000, "currency": "USD", "price_usd": 60000.0, "rooms": 2,
           "area_total": 55.0, "location": "вул. Тестова", "quality_status": "ok"}
    with SessionLocal() as s:
        assert Pipeline._upsert(s, dict(rec)) == "inserted"      # новий + перша подія ціни
        s.commit()
        assert Pipeline._upsert(s, {**rec, "price": 58000, "price_usd": 58000.0}) == "updated"
        s.commit()
        lid = s.scalar(select(Listing.id).where(Listing.external_id == "TEST-SCHEMA-1"))
        events = s.scalar(select(func.count()).select_from(PriceEvent)
                          .where(PriceEvent.listing_id == lid))
    assert events == 2                                           # і зміна ціни записалась
