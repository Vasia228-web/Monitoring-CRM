"""Веб-інтерфейс: фільтри, сортування, лічильники."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty.web.app import _median, _money, _relative_date, app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_empty_price_fields_do_not_break_filters(client):
    """Регресія: форма надсилає незаповнені поля як `price_min=`.

    З типом `float | None` FastAPI відповідав 422 і сторінка ламалась повністю
    щоразу, коли фільтр міняли без введеної ціни.
    """
    r = client.get("/", params={"condition": "renovated", "market": "", "rooms": "",
                                "source": "", "price_min": "", "price_max": "",
                                "sort": "date_desc"})
    assert r.status_code == 200
    assert client.get("/api/listings", params={"price_min": "", "limit": 3}).status_code == 200


def test_garbage_price_is_ignored_not_fatal(client):
    assert client.get("/", params={"price_min": "abc"}).status_code == 200


def test_all_sorts_render(client):
    for sort in ("price_asc", "price_desc", "rooms_asc", "rooms_desc",
                 "sqm_asc", "sqm_desc", "date_asc", "date_desc", "hacked"):
        assert client.get("/", params={"sort": sort}).status_code == 200


def test_condition_filter_narrows_results(client):
    """Порівнювати розміри сторінок не можна: на великій базі обидві впираються
    в стелю рядків. Перевіряємо семантику фільтра й лічильник збігів."""
    rows = client.get("/api/listings", params={"condition": "renovated"}).json()
    # Помилка валідації теж має len(), тому спершу переконуємось, що це список.
    assert isinstance(rows, list) and rows
    assert all(r["condition"] == "renovated" for r in rows)

    total = client.get("/api/stats").json()["total"]
    body = client.get("/", params={"condition": "renovated"}).text
    assert "за фільтром" in body          # показано кількість збігів, а не всю базу
    assert total > len(rows)


def test_api_keeps_its_row_cap(client):
    """У сторінки тепер перелистування, а в API лишилась стеля на відповідь."""
    from realty.web.app import MAX_ROWS

    assert client.get("/api/listings", params={"limit": MAX_ROWS}).status_code == 200
    assert client.get("/api/listings", params={"limit": MAX_ROWS + 1}).status_code == 422


def test_page_size_is_limited_to_the_offered_values(client):
    """Довільний розмір сторінки в адресі не має ставати запитом на всю базу."""
    import re

    from realty.web.pagination import DEFAULT_PAGE_SIZE

    body = client.get("/", params={"per_page": 100000}).text
    rows = len(re.findall(r'data-id="(\d+)"', body))
    assert rows <= DEFAULT_PAGE_SIZE


def test_summary_shows_how_many_matched_not_how_many_fit(client):
    """Зведення має показувати кількість збігів, а не розмір сторінки."""
    import re

    body = client.get("/", params={"condition": "needs_repair"}).text
    m = re.search(r"Показано <b class=\"num\">(\d+)–(\d+)</b>\s*із", body)
    assert m, "рядок про діапазон має бути на сторінці"
    assert int(m.group(1)) == 1


def test_median_ignores_outlier_pull():
    # Середнє тут 200, медіана — 30: саме тому в зведенні медіана.
    assert _median([10, 20, 30, 40, 900]) == 30
    assert _median([10, 20]) == 15
    assert _median([]) is None


def test_money_and_date_filters():
    assert _money(138842).replace(" ", " ") == "138 842"
    assert _money(None) == "—"
    assert _relative_date(None) == "—"
