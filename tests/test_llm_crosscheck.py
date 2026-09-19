"""Незалежна перевірка LLM-фолбеку: відповідь моделі звіряється з парсером."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from realty.llm import ExtractedListing
from realty.models import Base, Condition, Listing, MarketType
from realty.pipeline import Pipeline
from realty.quality import llm_check
from realty.quality.rules import load_thresholds
from realty.quality.staging import QualityGate

PAGE = "<html><body>сторінка</body></html>"


class _Stub:
    def __init__(self, answer):
        self.answer, self.available, self.calls = answer, True, 0

    def extract(self, *_a, **_k):
        self.calls += 1
        return self.answer


def _pipeline(answer):
    p = Pipeline(use_llm=False)
    p.llm = _Stub(answer)
    p._page_text = lambda rec: PAGE
    return p


def _rec(**kw):
    base = {"source": "olx", "external_id": "Z1", "original_url": "https://www.olx.ua/x-IDZ1.html",
            "price": 65000, "currency": "USD", "price_usd": 65000.0, "rooms": None,
            "area_total": 111.84, "market_type": MarketType.UNKNOWN,
            "condition": Condition.UNKNOWN}
    base.update(kw)
    return base


def test_compare_only_where_both_have_values():
    got = ExtractedListing(price=65000, currency="USD", rooms=5, area_total=111.8)
    compared, conflicts = llm_check.compare(_rec(), got)
    assert set(compared) == {"price", "area_total"}     # кімнат у парсера нема — не звіряємо
    assert conflicts == []


def test_price_in_other_currency_is_converted_before_comparing():
    from realty.normalize import to_uah
    got = ExtractedListing(price=to_uah(65000, "USD"), currency="UAH")
    assert llm_check.compare(_rec(), got) == (["price"], [])


def test_disagreement_blocks_every_llm_field_and_is_counted():
    """Модель прочитала площу інакше — її кімнатам вірити нема підстав."""
    p = _pipeline(ExtractedListing(price=65000, currency="USD", rooms=5, area_total=73.0))
    out = p._apply_llm(_rec(), PAGE)
    assert out["rooms"] is None                  # консервативно: лишили порожнім
    assert not out.get("llm_extracted")
    assert "площа" in out["llm_conflict"]
    assert p.llm_check == {"agreed": 0, "disagreed": 1, "uncomparable": 0}


def test_agreement_fills_the_gap_and_is_counted():
    p = _pipeline(ExtractedListing(price=65000, currency="USD", rooms=5, area_total=111.84))
    out = p._apply_llm(_rec(), PAGE)
    assert out["rooms"] == 5 and out["llm_extracted"]
    assert p.llm_check["agreed"] == 1


def test_nothing_to_compare_is_counted_separately():
    p = _pipeline(ExtractedListing(rooms=2))
    out = p._apply_llm(_rec(price=None, area_total=None, price_usd=None), PAGE)
    assert out["rooms"] == 2
    assert p.llm_check["uncomparable"] == 1


def test_llm_currency_never_overwrites_parsers_price_currency():
    """Регресія: `not rec.get("price") is None` означало «ціна від парсера є»,
    і валюта від моделі затирала валюту парсера разом із ціною в доларах."""
    p = _pipeline(ExtractedListing(rooms=3, currency="UAH"))
    out = p._apply_llm(_rec(area_total=None), PAGE)
    assert out["currency"] == "USD"
    assert out["price_usd"] == pytest.approx(65000.0)


def test_conflict_goes_to_quarantine_not_to_the_table():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    rec = _rec(rooms=3, area_total=85.0, llm_conflict="кімнат: парсер 3, LLM 5",
               price_per_sqm=765.0, location="вул. Тестова")
    with Session() as s:
        out = QualityGate(load_thresholds()).screen(s, [rec])
    assert out[0]["quality_status"] in ("review", "rejected")
    assert "LLM розійшовся" in out[0]["quality_reason"]


def test_llm_fill_updates_the_existing_row_instead_of_adding_one():
    """Перевірка підозри: фолбек НЕ створює другий запис — ключ (джерело, id)."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        Pipeline._upsert(s, _rec(rooms=None))
        s.commit()
        p = _pipeline(ExtractedListing(price=65000, currency="USD", rooms=5, area_total=111.84))
        filled = p._apply_llm(_rec(), PAGE)
        Pipeline._upsert(s, {k: v for k, v in filled.items()})
        s.commit()
        assert s.scalar(select(func.count()).select_from(Listing)) == 1
        assert s.scalar(select(Listing.rooms)) == 5
