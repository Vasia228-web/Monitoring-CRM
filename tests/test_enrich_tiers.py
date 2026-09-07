"""Рівні добору: коли завантажується сторінка деталей і коли кличеться модель."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.models import Condition, MarketType
from realty.pipeline import Pipeline, _gaps
from realty.sources.flombu import FlombuSource, parse_detail as flombu_detail
from realty.sources.domria import DomRiaSource
from realty.sources.olx import OlxSource

FLOMBU_PAGE = Path(__file__).resolve().parent.parent / "probes" / "_flombu_detail.html"
OLX_PAGE = Path(__file__).resolve().parent.parent / "probes" / "_olx_detail.html"


def _full(**over):
    rec = {"source": "olx", "original_url": "u", "price": 60000, "currency": "USD",
           "rooms": 2, "area_total": 55.0,
           "market_type": MarketType.SECONDARY, "condition": Condition.RENOVATED}
    rec.update(over)
    return rec


def test_gaps_distinguishes_critical_from_desirable():
    assert _gaps(_full()) == (False, False)
    assert _gaps(_full(rooms=None)) == (True, False)
    assert _gaps(_full(condition=Condition.UNKNOWN)) == (False, True)
    assert _gaps(_full(market_type=MarketType.UNKNOWN)) == (False, True)


def test_complete_skips_page_when_record_is_full():
    fetched = []
    p = Pipeline(use_llm=False)
    p._page_text = lambda rec: fetched.append(1) or "<html></html>"
    p.complete(_full(), OlxSource())
    assert not fetched


@pytest.mark.skipif(not OLX_PAGE.exists(), reason="немає збереженої сторінки OLX")
def test_unknown_condition_alone_triggers_detail_page():
    """Регресія: раніше рівень 2 вмикався лише через ціну/кімнати/площу,
    тож 116 карток OLX із повними числами так і лишались без стану."""
    html = OLX_PAGE.read_text(encoding="utf-8")
    p = Pipeline(use_llm=False)
    p._page_text = lambda rec: html
    out = p.complete(_full(condition=Condition.UNKNOWN,
                           market_type=MarketType.UNKNOWN), OlxSource())
    assert out["market_type"] is MarketType.SECONDARY
    assert out.get("description")
    assert out["detail_enriched"] is True


def test_llm_not_called_for_desirable_gaps_only():
    """За стан і ринок платити викликом моделі не варто."""
    called = []

    class _LLM:
        available = True
        calls = 0

    p = Pipeline(use_llm=False)
    p.llm = _LLM()
    p._page_text = lambda rec: "<html><body>сторінка</body></html>"
    p._apply_llm = lambda rec, page: (called.append(1), rec)[1]
    p.complete(_full(source="domria", condition=Condition.UNKNOWN), DomRiaSource())
    assert not called


@pytest.mark.skipif(not FLOMBU_PAGE.exists(), reason="немає збереженої сторінки flombu")
def test_flombu_detail_extracts_description():
    html = FLOMBU_PAGE.read_text(encoding="utf-8")
    d = flombu_detail(html)
    assert len(d["description"]) > 100
    assert d["market_type"] is MarketType.PRIMARY   # «від провідного забудовника»

    rec = {"source": "flombu", "description": None,
           "market_type": MarketType.UNKNOWN, "condition": Condition.UNKNOWN}
    out = FlombuSource().enrich(dict(rec), html)
    assert out["detail_enriched"] is True
    assert out["market_type"] is MarketType.PRIMARY
