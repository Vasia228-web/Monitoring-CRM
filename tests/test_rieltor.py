"""Картка rieltor.ua — сайт-першоджерело для оголошень, які агрегує LUN."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.models import Condition, MarketType
from realty.sources.lun import LunSource
from realty.sources.rieltor import is_rieltor, parse_detail

PAGE = Path(__file__).resolve().parent.parent / "probes" / "_rieltor_detail.html"
pytestmark = pytest.mark.skipif(not PAGE.exists(), reason="немає збереженої картки rieltor.ua")


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8", errors="ignore")


def test_parses_card(html):
    d = parse_detail(html)
    assert d["rooms"] == 1
    assert d["area_total"] == 43.0
    assert d["built_year"] == 2021
    assert d["condition"] is Condition.RENOVATED     # «Загальний стан квартири: З ремонтом»
    assert len(d["description"]) > 200


def test_site_field_beats_text_guess(html):
    """Пряме поле сайту точніше за здогад із опису, тож має пріоритет."""
    from realty.sources.rieltor import _STATE_MAP

    assert _STATE_MAP["без ремонту"] is Condition.NEEDS_REPAIR
    assert _STATE_MAP["з ремонтом"] is Condition.RENOVATED
    # «Частковий ремонт» свідомо не мапиться: у бінарній схемі це ні те, ні те.
    assert "частковий ремонт" not in _STATE_MAP


def test_host_guard():
    assert is_rieltor("https://rieltor.ua/ivano-frankovsk/flats-sale/view/1/")
    assert not is_rieltor("https://lun.ua/uk/realty/1")
    assert not is_rieltor(None)


def test_lun_enrich_only_for_rieltor_urls(html):
    src = LunSource()
    base = {"source": "lun", "condition": Condition.UNKNOWN,
            "market_type": MarketType.UNKNOWN, "description": None}

    other = src.enrich({**base, "original_url": "https://lun.ua/uk/realty/1"}, html)
    assert other["condition"] is Condition.UNKNOWN
    assert not other.get("detail_enriched")

    ours = src.enrich({**base, "original_url": "https://rieltor.ua/x/view/1/"}, html)
    assert ours["condition"] is Condition.RENOVATED
    assert ours["detail_enriched"] is True


def test_enrich_does_not_overwrite_existing(html):
    src = LunSource()
    rec = {"source": "lun", "original_url": "https://rieltor.ua/x/view/1/",
           "condition": Condition.NEEDS_REPAIR, "rooms": 3,
           "market_type": MarketType.SECONDARY, "description": "власний опис"}
    out = src.enrich(dict(rec), html)
    assert out["condition"] is Condition.NEEDS_REPAIR
    assert out["rooms"] == 3
    assert out["description"] == "власний опис"
