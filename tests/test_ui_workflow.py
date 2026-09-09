"""Критерії приймання щоденного інтерфейсу: сортування, обробка, фільтри."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty.web.app import app
from realty.web.queries import DEFAULT_SORT, SORTS, listing_query, order_clause


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _ids(client, url: str) -> list[dict]:
    return client.get(url).json()


# --- сортування ---------------------------------------------------------------

def test_default_sort_is_price_descending():
    assert DEFAULT_SORT == "price_desc"


def test_every_tab_starts_with_the_most_expensive(client):
    """Критерій приймання: у кожній вкладці перший елемент — найдорожчий."""
    for url in ("/api/listings?limit=200",
                "/api/listings?limit=200&in_progress=true",
                "/api/listings?limit=200&rooms=2",
                "/api/listings?limit=200&condition=renovated"):
        rows = _ids(client, url)
        if len(rows) < 2:
            continue
        prices = [r["price_usd"] for r in rows if r["price_usd"] is not None]
        assert prices == sorted(prices, reverse=True), f"порушено порядок: {url}"
        assert rows[0]["price_usd"] == max(prices), f"вгорі не найдорожчий: {url}"


def test_records_without_price_go_last():
    """Об'єкти без ціни мають однозначне місце — у кінці, у будь-якому напрямку."""
    for sort in SORTS:
        clause = order_clause(sort)
        assert clause, sort
        # nullslast застосовано до кожної колонки сортування
        assert all("nulls last" in str(c).lower() for c in clause[:-1]), sort


def test_new_tab_inherits_sorting_without_extra_code():
    """Будь-яка нова вкладка бере сортування зі спільного шару."""
    for kwargs in ({}, {"in_progress": True}, {"rooms": "3"}, {"source": "olx"}):
        sql = str(listing_query(**kwargs))
        assert "ORDER BY" in sql and "price_usd DESC" in sql


# --- взято в обробку ----------------------------------------------------------

def _some_id(client) -> int:
    import re

    return int(re.search(r'data-id="(\d+)"', client.get("/").text).group(1))


def test_processing_status_lives_in_the_database(client):
    """Статус має переживати перезапуск і бути видимим з іншого пристрою,
    тому зберігається полем об'єкта, а не в стані сторінки."""
    from sqlalchemy import select

    from realty.db import SessionLocal
    from realty.models import Listing

    lid = _some_id(client)
    try:
        r = client.post(f"/api/listings/{lid}/processing", json={"in_progress": True}).json()
        assert r["ok"] and r["in_progress"] is True and r["in_progress_at"]

        # Нова сесія до бази — те саме, що інший пристрій після перезапуску.
        with SessionLocal() as s:
            row = s.get(Listing, lid)
            assert row.in_progress is True and row.in_progress_at is not None

        page = client.get("/processing").text
        assert f'data-id="{lid}"' in page, "об'єкт не з'явився на сторінці обробки"
    finally:
        client.post(f"/api/listings/{lid}/processing", json={"in_progress": False})


def test_removing_from_processing_keeps_the_record_and_history(client):
    """«Прибрати з обробки» знімає позначку й повертає об'єкт у загальний
    список. Ніякого видалення: історія цін має лишитись цілою."""
    from sqlalchemy import func, select

    from realty.db import SessionLocal
    from realty.models import Listing, PriceEvent

    lid = _some_id(client)
    with SessionLocal() as s:
        events_before = s.scalar(
            select(func.count()).select_from(PriceEvent).where(PriceEvent.listing_id == lid))

    client.post(f"/api/listings/{lid}/processing", json={"in_progress": True})
    r = client.post(f"/api/listings/{lid}/processing", json={"in_progress": False}).json()
    assert r["in_progress"] is False and r["in_progress_at"] is None

    with SessionLocal() as s:
        assert s.get(Listing, lid) is not None, "запис зник із бази"
        assert s.scalar(select(func.count()).select_from(PriceEvent)
                        .where(PriceEvent.listing_id == lid)) == events_before

    assert f'data-id="{lid}"' not in client.get("/processing").text
    assert f'data-id="{lid}"' in client.get("/?limit=1000").text


def test_processing_page_shows_only_taken(client):
    lid = _some_id(client)
    try:
        client.post(f"/api/listings/{lid}/processing", json={"in_progress": True})
        rows = _ids(client, "/api/listings?in_progress=true&limit=200")
        assert rows and all(r["in_progress"] for r in rows)
    finally:
        client.post(f"/api/listings/{lid}/processing", json={"in_progress": False})


def test_processing_rejects_bad_payload(client):
    lid = _some_id(client)
    assert client.post(f"/api/listings/{lid}/processing",
                       json={"in_progress": "так"}).status_code == 400
    assert client.post("/api/listings/999999999/processing",
                       json={"in_progress": True}).status_code == 404


# --- фільтр ціни й навігація ---------------------------------------------------

def test_price_filter_applies_only_on_submit(client):
    """Поля ціни не мають data-auto, тож список не смикається на кожен символ."""
    page = client.get("/").text
    price_inputs = [line for line in page.splitlines() if 'name="price_m' in line]
    assert price_inputs, "поля ціни зникли зі сторінки"
    assert all("data-auto" not in line for line in price_inputs)
    assert 'class="apply"' in page, "немає кнопки «Застосувати»"


def test_reversed_price_range_explains_itself(client):
    """Порожній список без пояснення виглядає як поломка."""
    page = client.get("/?price_min=200000&price_max=50000").text
    assert "більша за" in page
    # Замість порожнечі показуємо звичайний список.
    assert 'data-id="' in page


def test_filters_and_sort_live_in_the_url(client):
    """Посилання на конкретну вибірку має відтворюватись в іншої людини."""
    url = "/?rooms=2&condition=renovated&sort=price_asc&price_min=40000"
    page = client.get(url).text
    assert 'value="2" selected' in page or "selected" in page
    rows = _ids(client, "/api/listings?rooms=2&condition=renovated&sort=price_asc&limit=50")
    prices = [r["price_usd"] for r in rows if r["price_usd"]]
    assert prices == sorted(prices), "сортування з посилання не застосувалось"


def test_navigation_is_on_every_page(client):
    """Кнопки переходу — у спільному layout, а не продубльовані вручну."""
    for url in ("/", "/processing", "/status"):
        page = client.get(url).text
        assert 'href="/status"' in page, url
        assert 'href="/"' in page and 'href="/processing"' in page, url


def test_pages_are_closed_from_search_engines(client):
    for url in ("/", "/processing", "/status"):
        assert "noindex" in client.get(url).text, url


# --- доступ ззовні ------------------------------------------------------------

def test_everything_is_behind_a_password(monkeypatch):
    """Панель із даними й ручними тригерами не має бути відкритою."""
    from fastapi.testclient import TestClient

    from realty.web.app import app

    monkeypatch.setenv("AUTH_USER", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    c = TestClient(app)

    for url in ("/", "/processing", "/status", "/api/listings", "/api/status"):
        assert c.get(url).status_code == 401, url
    assert c.post("/api/status/run", json={"source": "olx"}).status_code == 401
    assert c.get("/", auth=("u", "p")).status_code == 200
    assert c.get("/", auth=("u", "невірний")).status_code == 401


def test_health_and_robots_stay_open(monkeypatch):
    """Хостингу треба перевіряти живучість, а пошуковикам — бачити заборону."""
    from fastapi.testclient import TestClient

    from realty.web.app import app

    monkeypatch.setenv("AUTH_USER", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    c = TestClient(app)
    assert c.get("/healthz").status_code == 200
    assert "Disallow: /" in c.get("/robots.txt").text


def test_no_secrets_in_deployment_files():
    root = Path(__file__).resolve().parent.parent
    for name in ("Dockerfile", "docker-compose.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "sk-ant-" not in text, name
        assert "${" in text or "ENV" in text, f"{name}: секрети мають іти через оточення"


def test_non_ascii_password_works(monkeypatch):
    """Регресія: compare_digest не приймає не-ASCII, і пароль із кирилицею
    давав 500 замість 401."""
    from fastapi.testclient import TestClient

    from realty.web.app import app

    monkeypatch.setenv("AUTH_USER", "користувач")
    monkeypatch.setenv("AUTH_PASSWORD", "пароль")
    c = TestClient(app)
    assert c.get("/", auth=("користувач", "пароль")).status_code == 200
    assert c.get("/", auth=("користувач", "інше")).status_code == 401
