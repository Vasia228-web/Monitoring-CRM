"""Сторінка оголошення DIM.RIA: характеристики, яких немає в JSON-API."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.models import Condition, MarketType
from realty.sources.domria import (
    _CONDITION_MAP, DomRiaSource, extract_state, parse_detail,
)

ROOT = Path(__file__).resolve().parent.parent / "probes"
PAGE = ROOT / "_ria_detail.html"              # має «стан квартири: хороший»
FACADE = ROOT / "_ria_detail_facade.html"     # має лише «Зовнішня обробка: без оздоблення»


@pytest.fixture(scope="module")
def html() -> str:
    if not PAGE.exists():
        pytest.skip("немає збереженої сторінки DIM.RIA")
    return PAGE.read_text(encoding="utf-8")


def test_state_carries_characteristics_api_omits(html):
    """JSON-ендпоінт віддає characteristics_values порожнім, HTML — заповненим."""
    realty = ((extract_state(html).get("listing") or {}).get("data") or {}).get("realty") or {}
    assert realty.get("characteristics_values")
    assert realty.get("secondaryParams")


def test_parses_condition_and_market(html):
    d = parse_detail(html)
    assert d["condition"] is Condition.RENOVATED     # «стан квартири: хороший»
    assert d["market_type"] is MarketType.PRIMARY    # mainCharacteristics -> «Новобудова»


def test_facade_finish_is_not_flat_condition():
    """Пастка: «Зовнішня обробка: без оздоблення» — про фасад, а не про квартиру.

    Без перевірки назви групи це стало б хибним «без ремонту».
    """
    if not FACADE.exists():
        pytest.skip("немає сторінки з фасадною обробкою")
    d = parse_detail(FACADE.read_text(encoding="utf-8"))
    assert "condition" not in d


def test_condition_vocabulary():
    assert _CONDITION_MAP["дизайнерський ремонт"] is Condition.RENOVATED
    assert _CONDITION_MAP["косметичний ремонт"] is Condition.RENOVATED
    assert (_CONDITION_MAP["потребує ремонту/ без ремонту / ремонт не завершений"]
            is Condition.NEEDS_REPAIR)


def test_enrich_does_not_overwrite(html):
    src = DomRiaSource()
    rec = {"source": "domria", "condition": Condition.NEEDS_REPAIR,
           "market_type": MarketType.UNKNOWN, "description": "власний опис"}
    out = src.enrich(dict(rec), html)
    assert out["condition"] is Condition.NEEDS_REPAIR      # наявне не чіпаємо
    assert out["market_type"] is MarketType.PRIMARY        # порожнє — доповнено
    assert out["description"] == "власний опис"
    assert out["detail_enriched"] is True


def test_extract_state_survives_broken_page():
    assert extract_state("<html>без стану</html>") == {}
    assert parse_detail("<html>без стану</html>") == {}


def test_market_falls_back_to_build_year_and_series(html):
    """Мітку ринку RIA ставить не завжди — тоді працюють рік і серія будинку."""
    d = parse_detail(html)
    assert d["built_year"] == 2024

    if FACADE.exists():
        # На цій сторінці мітки ринку немає, зате серія — «Чеський проект».
        f = parse_detail(FACADE.read_text(encoding="utf-8"))
        assert f["market_type"] is MarketType.SECONDARY
