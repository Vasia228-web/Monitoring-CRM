"""Перелистування: згортання номерів, межі, стабільність порядку."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.web.pagination import (
    DEFAULT_PAGE_SIZE, PAGE_SIZES, Page, build, clamp_page, clamp_size,
)


def test_counts_and_bounds():
    p = build(total=15503, page=2, per_page=50)
    assert p.pages == 311
    assert p.offset == 50
    assert (p.first, p.last) == (51, 100)
    assert p.has_prev and p.has_next


def test_last_page_is_not_padded():
    p = build(total=15503, page=311, per_page=50)
    assert (p.first, p.last) == (15501, 15503)
    assert not p.has_next


def test_empty_result_is_a_single_empty_page():
    p = build(total=0, page=1, per_page=50)
    assert p.pages == 1 and (p.first, p.last) == (0, 0)
    assert not p.has_prev and not p.has_next


def test_page_beyond_the_end_is_pulled_back():
    """Посилання на сторінку 999 не має давати порожній екран без пояснення."""
    assert build(total=100, page=999, per_page=50).number == 2
    assert build(total=100, page=0, per_page=50).number == 1
    assert build(total=100, page="казна-що", per_page=50).number == 1


def test_only_offered_page_sizes_are_accepted():
    for size in PAGE_SIZES:
        assert clamp_size(size) == size
    for bad in (7, 10_000, -5, None, "abc"):
        assert clamp_size(bad) == DEFAULT_PAGE_SIZE


def test_middle_is_collapsed_with_both_ends_visible():
    numbers = build(total=15503, page=150, per_page=50).numbers()
    assert numbers[0] == 1 and numbers[-1] == 311
    assert None in numbers
    assert 150 in numbers and 148 in numbers and 152 in numbers


def test_short_lists_show_every_number():
    numbers = build(total=300, page=3, per_page=50).numbers()
    assert numbers == [1, 2, 3, 4, 5, 6]
    assert None not in numbers


def test_no_gap_marker_right_next_to_an_edge():
    """«1 … 2» виглядало б безглуздо — пропуск ставиться лише за розрив."""
    for page in (1, 2, 3, 309, 310, 311):
        numbers = build(total=15503, page=page, per_page=50).numbers()
        for i, n in enumerate(numbers[:-1]):
            if n is None:
                assert numbers[i + 1] - numbers[i - 1] > 2


def test_page_size_changes_the_number_of_pages():
    assert build(total=15503, page=1, per_page=50).pages == 311
    assert build(total=15503, page=1, per_page=200).pages == 78


def test_clamp_page_needs_at_least_one_page():
    assert clamp_page(5, pages=0) == 1


# --- наскрізний прохід по сторінках -------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from realty.web.app import app

    with TestClient(app) as c:
        yield c


def _ids(html: str) -> list[int]:
    import re

    return [int(x) for x in re.findall(r'data-id="(\d+)"', html)]


def _total(html: str) -> int:
    import re

    m = re.search(r"із <b class=\"num\">([\d\s\u202f]+)</b>", html)
    return int(re.sub(r"\D", "", m.group(1)))


def test_every_id_appears_exactly_once_across_all_pages(client):
    """Головна перевірка: пройти всі сторінки поспіль і нічого не загубити.

    Без другого, унікального ключа сортування порядок усередині групи з
    однаковою ціною не визначений — а таких груп у базі повно: підряд стоять
    кілька записів по $146 500. База має право віддавати їх щоразу
    по-різному, і тоді одні записи з'являються двічі, інші зникають зовсім.
    Помітити це можна лише пройшовши всі сторінки підряд.
    """
    seen: list[int] = []
    total = 0
    for page in range(1, 400):
        html = client.get("/", params={"rooms": "1", "per_page": 200,
                                       "page": page}).text
        ids = _ids(html)
        if not ids:
            break
        total = _total(html)
        seen.extend(ids)
        if len(seen) >= total:
            break

    assert seen, "вибірка не має бути порожньою"
    duplicates = len(seen) - len(set(seen))
    assert duplicates == 0, f"{duplicates} записів трапились двічі"
    assert len(seen) == total, f"зібрано {len(seen)} із {total}"


def test_order_is_identical_between_two_requests(client):
    """Той самий запит має двічі дати той самий порядок."""
    params = {"per_page": 100, "page": 2, "sort": "price_desc"}
    first = _ids(client.get("/", params=params).text)
    second = _ids(client.get("/", params=params).text)
    assert first == second and first


def test_pages_do_not_overlap(client):
    params = {"per_page": 50, "sort": "price_desc"}
    a = set(_ids(client.get("/", params={**params, "page": 1}).text))
    b = set(_ids(client.get("/", params={**params, "page": 2}).text))
    assert a and b and not (a & b)


def test_changing_sort_returns_to_the_first_page(client):
    """Лишитись на 150-й сторінці після перевертання порядку — безглуздо."""
    import re

    html = client.get("/", params={"page": 5, "sort": "price_desc"}).text
    links = re.findall(r'href="(/\?[^"]*sort=price_asc[^"]*)"', html)
    assert links
    assert all("page=" not in link for link in links), links[:2]


def test_changing_filters_returns_to_the_first_page(client):
    """Вибірка з трьох результатів не має відкриватись на 40-й сторінці."""
    html = client.get("/", params={"page": 5}).text
    form = html[html.index('<form class="filters"'):html.index("</form>")]
    assert 'name="page"' not in form


def test_slicing_happens_in_the_database(client):
    """Сторінка не має вибирати всю базу в пам'ять заради сорока рядків."""
    import inspect

    from realty.web import app as app_mod

    source = inspect.getsource(app_mod._render_list)
    assert ".limit(pager.size).offset(pager.offset)" in source


def test_merged_listings_collapse_into_one_row(client):
    """Та сама квартира з трьох майданчиків — один рядок, а не три.

    Дедуплікація зводила їх в один об'єкт і раніше, але список показував по
    рядку на оголошення, і виглядало це як провал дедуплікації.
    """
    collapsed = _total(client.get("/", params={"per_page": 50}).text)
    every = _total(client.get("/", params={"per_page": 50, "all_ads": "1"}).text)
    assert collapsed < every
    assert "Показати всі оголошення" in client.get("/").text
