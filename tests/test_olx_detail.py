"""Детермінований розбір сторінки оголошення OLX (рівень 2)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.models import Condition, MarketType
from realty.pipeline import Pipeline
from realty.sources.olx import OlxSource, parse_detail

PAGE = Path(__file__).resolve().parent.parent / "probes" / "_olx_detail.html"
pytestmark = pytest.mark.skipif(not PAGE.exists(), reason="немає збереженої сторінки OLX")


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_parses_parameter_list(html):
    d = parse_detail(html)
    assert d["rooms"] == 3            # «Кількість кімнат: 3 кімнати»
    assert d["area_total"] == 55.0    # «Загальна площа: 55 м²»
    assert d["floor"] == 1 and d["floors_total"] == 5
    assert d["price"] == 64000.0 and d["currency"] == "USD"
    assert d["market_type"] is MarketType.SECONDARY   # «Вид об'єкта: Вторинний ринок»
    assert d["published_at"] is not None


def test_enrich_fills_gaps_without_overwriting(html):
    src = OlxSource()
    rec = {"source": "olx", "rooms": None, "area_total": 55.0, "price": 999,
           "currency": "USD", "market_type": MarketType.UNKNOWN,
           "condition": Condition.UNKNOWN}
    out = src.enrich(dict(rec), html)
    assert out["rooms"] == 3               # порожнє — дібрано
    assert out["price"] == 999             # наявне — НЕ перезаписано
    assert out["area_total"] == 55.0
    assert out["market_type"] is MarketType.SECONDARY
    assert out["detail_enriched"] is True


def test_detail_tier_runs_before_llm(html):
    """Якщо сторінка деталей закрила прогалини, модель не викликається."""
    called = []

    p = Pipeline(use_llm=False)
    p._page_text = lambda rec: html
    p._apply_llm = lambda rec, page: (called.append(1), rec)[1]

    rec = {"source": "olx", "original_url": "u", "price": 64000, "currency": "USD",
           "rooms": None, "area_total": None,
           "market_type": MarketType.UNKNOWN, "condition": Condition.UNKNOWN}
    out = p.complete(rec, OlxSource())

    assert out["rooms"] == 3 and out["area_total"] == 55.0
    assert out["price_per_sqm"] == pytest.approx(64000 / 55.0, rel=1e-3)
    assert not called, "LLM не мав знадобитись"
    assert p.report.enriched == 1


def test_no_page_fetch_when_nothing_can_fill():
    """Джерело без `enrich` і без ключа не має тягнути сторінку намарно."""
    from realty.sources.base import BaseSource
    from realty.sources.blago import BlagoSource

    # Якщо цей assert впаде — у blago з'явився `enrich`, і тест треба
    # переписати на інше джерело, а не «полагодити» очікування нижче.
    assert BlagoSource.enrich is BaseSource.enrich

    fetched = []
    p = Pipeline(use_llm=False)
    p.llm = None
    p._page_text = lambda rec: fetched.append(rec) or "<html></html>"

    rec = {"source": "blago", "original_url": "u", "rooms": None, "price": 1,
           "area_total": None}
    p.complete(rec, BlagoSource())
    assert not fetched


def test_repair_parameter_vocabulary_is_complete():
    """Усі сім значень фільтра «Ремонт» на OLX мають бути враховані.

    Жодне з них не ловиться текстовими шаблонами: «Житловий стан» не містить
    слова «ремонт», а «Під чистову обробку» — це не «чорнова обробка».
    """
    from realty.sources.olx import _REPAIR_BY_PARAM

    assert set(_REPAIR_BY_PARAM) == {
        "авторський проект", "євроремонт", "косметичний ремонт", "житловий стан",
        "після будівельників", "під чистову обробку", "аварійний стан",
    }
    assert _REPAIR_BY_PARAM["житловий стан"] is Condition.RENOVATED
    assert _REPAIR_BY_PARAM["під чистову обробку"] is Condition.NEEDS_REPAIR
    assert _REPAIR_BY_PARAM["аварійний стан"] is Condition.NEEDS_REPAIR


def test_repair_parameter_beats_description(html):
    """Пряме поле сайту точніше за здогад із тексту."""
    from realty.sources.olx import _PARAM_RE, _REPAIR_BY_PARAM

    # Значення параметра розбирається тим самим шаблоном, що й решта пар.
    m = _PARAM_RE.match("Ремонт: Під чистову обробку")
    assert m and _REPAIR_BY_PARAM[m.group(2).strip().lower()] is Condition.NEEDS_REPAIR
