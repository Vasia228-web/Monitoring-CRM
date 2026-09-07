"""Перевірка LLM-фолбеку без звернення до моделі.

Виклик до Claude тут підмінений: тестуємо саме те, що можна зламати
непомітно — чи фолбек доповнює ЛИШЕ порожні поля, чи не затирає роботу
парсера і чи перераховує похідні величини після доповнення.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.llm import ExtractedListing, html_to_text
from realty.models import Condition, MarketType
from realty.pipeline import Pipeline


class _StubLLM:
    """Вдає з себе LLMExtractor і повертає наперед задану відповідь."""

    def __init__(self, answer: ExtractedListing) -> None:
        self.answer = answer
        self.available = True
        self.calls = 0

    def extract(self, html_or_text: str, url: str = "") -> ExtractedListing:
        self.calls += 1
        return self.answer


PAGE = "<html><body>сторінка оголошення</body></html>"


def _pipeline(answer: ExtractedListing):
    p = Pipeline(use_llm=False)
    p.llm = _StubLLM(answer)
    p._page_text = lambda rec: PAGE          # без мережі
    return p


def test_fills_only_missing_fields():
    # Парсер дістав ціну й площу, але не кімнати — саме цей випадок дає OLX.
    rec = {
        "source": "olx", "original_url": "https://www.olx.ua/d/uk/obyavlenie/x-ID1.html",
        "price": 64000, "currency": "USD", "price_usd": 64000.0,
        "rooms": None, "area_total": 55.0, "location": "Івано-Франківськ",
        "market_type": MarketType.UNKNOWN, "condition": Condition.UNKNOWN,
    }
    p = _pipeline(ExtractedListing(
        price=99999, rooms=3, area_total=12.3, location="вул. Інша",
        currency="USD", market_type="secondary", condition="renovated",
    ))
    out = p._apply_llm(dict(rec), PAGE)

    assert out["rooms"] == 3                      # порожнє поле — доповнено
    assert out["price"] == 64000                  # наявне — НЕ перезаписано
    assert out["area_total"] == 55.0              # наявне — НЕ перезаписано
    assert out["location"] == "Івано-Франківськ"  # наявне — НЕ перезаписано
    assert out["market_type"] is MarketType.SECONDARY   # було UNKNOWN — уточнено
    assert out["condition"] is Condition.RENOVATED
    assert out["llm_extracted"] is True


def test_recomputes_derived_values():
    """Після доповнення площі ціна за м² має перерахуватись, а не лишитись None."""
    rec = {
        "source": "olx", "original_url": "https://www.olx.ua/d/uk/obyavlenie/y-ID2.html",
        "price": 64000, "currency": "USD", "price_usd": 64000.0,
        "rooms": 3, "area_total": None, "price_per_sqm": None,
        "market_type": MarketType.UNKNOWN, "condition": Condition.UNKNOWN,
    }
    out = _pipeline(ExtractedListing(area_total=55.0))._apply_llm(dict(rec), PAGE)
    assert out["area_total"] == 55.0
    assert out["price_per_sqm"] == pytest.approx(64000 / 55.0, rel=1e-3)
    assert out["price_uah"] and out["price_uah"] > out["price_usd"]


def test_untouched_when_model_returns_nothing():
    rec = {"source": "olx", "original_url": "https://www.olx.ua/d/uk/obyavlenie/z-ID3.html",
           "price": 64000, "currency": "USD", "rooms": None, "area_total": 55.0,
           "market_type": MarketType.UNKNOWN, "condition": Condition.UNKNOWN}
    out = _pipeline(ExtractedListing())._apply_llm(dict(rec), PAGE)
    assert out["rooms"] is None
    assert not out.get("llm_extracted")


def test_no_call_without_credentials():
    """Без ключа фолбек має мовчки віддати запис як є, а не впасти."""
    p = Pipeline(use_llm=True)
    p.llm._unavailable_reason = "немає облікових даних"   # як після 401
    rec = {"source": "olx", "original_url": "u", "rooms": None}
    assert p._apply_llm(dict(rec), PAGE) == rec


def test_html_to_text_keeps_olx_detail_fields():
    """Очищення не має з'їдати саме ті рядки, заради яких кличемо модель.

    На вхід фолбеку завжди йде сторінка оголошення, а не стрічка пошуку.
    """
    page = Path(__file__).resolve().parent.parent / "probes" / "_olx_detail.html"
    if not page.exists():
        pytest.skip("немає збереженої сторінки OLX")
    text = html_to_text(page.read_text(encoding="utf-8"))
    assert "<script" not in text and "<div" not in text
    assert "Кількість кімнат" in text
    assert "Загальна площа" in text


def test_available_is_false_without_credentials(monkeypatch):
    """Регресія: SDK створює клієнт і без ключа, тож `available` мусить
    перевіряти креденшели сам — інакше фолбек марно тягне сторінку."""
    from realty.llm import LLMExtractor

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    assert LLMExtractor().available is False


def test_available_is_true_with_key(monkeypatch):
    from realty.llm import LLMExtractor

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    assert LLMExtractor().available is True
