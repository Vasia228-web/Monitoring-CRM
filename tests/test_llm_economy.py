"""Правила економії LLM-рівня: модель, обсяг, повтори, вміст запиту."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty import config
from realty.llm import ExtractedListing, LLMExtractor, clean_html

OLX_PAGE = Path(__file__).resolve().parent.parent / "probes" / "_olx_detail.html"


def test_uses_cheapest_model_not_opus_or_sonnet():
    """Для парсингу дорожчі моделі заборонені."""
    assert "haiku" in config.LLM_MODEL.lower()
    assert "opus" not in config.LLM_MODEL.lower()
    assert "sonnet" not in config.LLM_MODEL.lower()


def test_text_limit_is_within_agreed_range():
    assert 2000 <= config.LLM_MAX_CHARS <= 3000


def test_clean_html_strips_noise_and_respects_limit():
    html = """<html><body>
      <header>Шапка сайту</header><nav>Меню</nav>
      <script>var x=1</script><style>.a{}</style><svg><path/></svg>
      <div class="advert-banner">Реклама</div>
      <main><p>Продам 2-кімнатну квартиру, 60 м², 55000 $</p></main>
      <footer>Підвал</footer></body></html>"""
    out = clean_html(html, limit=2500)
    for noise in ("Шапка", "Меню", "var x", ".a{", "Реклама", "Підвал"):
        assert noise not in out
    assert "60 м²" in out and "55000 $" in out
    assert "<" not in out


@pytest.mark.skipif(not OLX_PAGE.exists(), reason="немає збереженої сторінки OLX")
def test_real_page_is_compressed_hard():
    html = OLX_PAGE.read_text(encoding="utf-8")
    out = clean_html(html)
    assert len(out) <= config.LLM_MAX_CHARS
    assert len(html) / len(out) > 50        # у модель іде частка відсотка від HTML


def test_price_survives_truncation():
    """Регресія: ціна стояла нижче за блок характеристик і зрізалась лімітом.

    Просте обрізання по довжині губило її, і модель повертала price=None.
    """
    filler = "\n".join(f"Інфраструктура: об'єкт {i}" for i in range(200))
    text = f"<html><body><main>Кількість кімнат: 2\n{filler}\n76 500 $</main></body></html>"
    out = clean_html(text, limit=2500)
    assert len(out) <= 2500
    assert "76 500 $" in out
    assert "Кількість кімнат: 2" in out


class _Stub:
    """Підмінює клієнт Anthropic і запам'ятовує аргументи виклику."""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[dict] = []
        self.boom = boom
        self.api_key = "sk-ant-test"
        self.auth_token = None
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("500 overloaded")
        return type("R", (), {"stop_reason": "end_turn", "usage": None,
                              "parsed_output": ExtractedListing(rooms=2)})()


def _extractor(boom: bool = False) -> tuple[LLMExtractor, _Stub]:
    e = LLMExtractor()
    stub = _Stub(boom)
    e._client = stub
    return e, stub


def test_failed_page_is_not_retried_in_the_same_run():
    e, stub = _extractor(boom=True)
    page = "<html><body><main>" + "Продам квартиру 60 м². " * 12 + "</main></body></html>"
    assert e.extract(page, "https://x/1") is None
    assert e.extract(page, "https://x/1") is None      # той самий URL
    assert len(stub.calls) == 1, "повторний запит до сторінки, що вже впала"
    e.extract(page, "https://x/2")                     # інший URL — можна
    assert len(stub.calls) == 2


def test_call_cap_is_enforced():
    e, stub = _extractor()
    e.max_calls = 2
    page = "<html><body><main>" + "Продам квартиру 60 м². " * 12 + "</main></body></html>"
    for i in range(5):
        e.extract(page, f"https://x/{i}")
    assert len(stub.calls) == 2


def test_request_shape_is_cheap_and_haiku_compatible():
    """Haiku не приймає `output_config.effort` та adaptive-thinking — 400."""
    e, stub = _extractor()
    page = "<html><body><main>" + "Продам квартиру 60 м². " * 12 + "</main></body></html>"
    e.extract(page, "https://x/1")
    kw = stub.calls[0]
    assert "thinking" not in kw and "output_config" not in kw
    assert kw["max_tokens"] <= 1024                     # відповідь — короткий JSON
    assert kw["output_format"] is ExtractedListing      # строга схема, без Markdown
    assert len(kw["messages"][0]["content"]) <= config.LLM_MAX_CHARS
    assert "<" not in kw["messages"][0]["content"]      # HTML у запит не потрапляє
