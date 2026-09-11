"""Стан фільтрів: чи переживає він перехід між вкладками, «назад» і перезавантаження."""
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty.web.app import app
from realty.web.navstate import ANALYTICS_KEYS, LIST_KEYS, carry, reset_url

STATE = "condition=renovated&market=primary&rooms=1&sort=price_asc"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _nav(html: str) -> dict[str, str]:
    """Посилання верхньої навігації: назва вкладки → адреса."""
    out = {}
    for href, name in re.findall(
            r'<a href="(/[^"]*)"[^>]*>(Моніторинг|В обробці|Аналітика|Стан системи)',
            html):
        out[name] = href.replace("&amp;", "&")
    return out


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# --- причина, через яку стан не тримався --------------------------------------

def test_tab_links_carry_the_current_filters(client):
    """Корінь проблеми: посилання вкладок були голі, і клік стирав вибірку.

    Стан справді лежав в адресі — працювали і «назад», і перезавантаження, і
    передане посилання. Але навігація вела на «/» та «/processing» без жодного
    параметра, тож будь-який перехід між вкладками починав усе спочатку.
    """
    nav = _nav(client.get(f"/?{STATE}").text)
    for tab in ("Моніторинг", "В обробці"):
        got = _params(nav[tab])
        assert got["rooms"] == ["1"]
        assert got["condition"] == ["renovated"]
        assert got["market"] == ["primary"]
        assert got["sort"] == ["price_asc"]


def test_analytics_link_carries_only_what_it_understands(client):
    """Аналітика не знає про сортування списку — не тягнемо туди зайве."""
    nav = _nav(client.get(f"/?{STATE}").text)
    got = _params(nav["Аналітика"])
    assert set(got) <= set(ANALYTICS_KEYS)
    assert got["rooms"] == ["1"]
    assert "sort" not in got


def test_state_survives_the_round_trip_between_tabs(client):
    """Пройти «Моніторинг → В обробці → Моніторинг» і не втратити вибірку."""
    nav = _nav(client.get(f"/?{STATE}").text)
    processing = client.get(nav["В обробці"])
    assert processing.status_code == 200
    back = _nav(processing.text)["Моніторинг"]
    assert _params(back) == _params(nav["Моніторинг"])


def test_status_page_gets_no_parameters(client):
    """Сторінці стану фільтри не потрібні — не засмічуємо адресу."""
    assert _nav(client.get(f"/?{STATE}").text)["Стан системи"] == "/status"


# --- посилання лишається читабельним і передаваним ----------------------------

def test_link_with_state_opens_the_same_selection(client):
    """Те саме посилання в іншого користувача має дати ту саму вибірку."""
    first = client.get(f"/?{STATE}").text
    second = client.get(f"/?{STATE}").text
    rows = lambda html: re.findall(r'data-id="(\d+)"', html)      # noqa: E731
    assert rows(first) == rows(second)
    assert rows(first), "вибірка не має бути порожньою"


def test_empty_values_never_reach_the_address():
    """Порожній фільтр не пишеться в адресу — інакше вона стає нечитабельною."""
    url = carry("/", {"rooms": "1", "market": "", "source": None, "sort": "price_desc"})
    assert "market=" not in url and "source=" not in url
    assert _params(url) == {"rooms": ["1"], "sort": ["price_desc"]}


def test_key_order_is_stable():
    """Однакова вибірка має давати однакове посилання, хай як його зібрали."""
    a = carry("/", {"sort": "price_asc", "rooms": "1", "condition": "renovated"})
    b = carry("/", {"condition": "renovated", "rooms": "1", "sort": "price_asc"})
    assert a == b


def test_unknown_page_gets_a_bare_link():
    assert carry("/status", {"rooms": "1"}) == "/status"
    assert carry("/something", {"rooms": "1"}) == "/something"


# --- кнопка скидання ----------------------------------------------------------

def test_reset_stays_on_the_current_page():
    """Раніше кнопка вела на «/» звідки б її не натиснули."""
    assert reset_url("/processing") == "/processing"
    assert reset_url("/") == "/"


def test_reset_button_is_always_visible(client):
    """Кнопка не має з'являтись і зникати — інакше форма стрибає під рукою."""
    for url in ("/", f"/?{STATE}", "/processing", f"/processing?{STATE}"):
        html = client.get(url).text
        assert 'class="reset' in html, url


def test_reset_is_dimmed_when_there_is_nothing_to_reset(client):
    assert "reset off" in client.get("/").text
    assert "reset off" not in client.get(f"/?{STATE}").text


def test_sort_alone_counts_as_state_to_reset(client):
    """Змінене сортування — теж стан: кнопка скидання має бути активною."""
    assert "reset off" not in client.get("/?sort=price_asc").text
    assert "reset off" in client.get("/?sort=price_desc").text     # типове


# --- сумісність ---------------------------------------------------------------

def test_list_pages_share_one_set_of_parameters():
    """Обидві сторінки списку читають однаковий набір — інакше стан губився б."""
    from realty.web.navstate import PAGE_KEYS

    assert PAGE_KEYS["/"] == PAGE_KEYS["/processing"] == LIST_KEYS
