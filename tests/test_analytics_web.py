"""Сторінки аналітики: чи виконуються обіцянки чесності на реальних роутах."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty.analytics import cache
from realty.analytics.settings import load
from realty.web.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _text(html: str) -> str:
    """Видимий текст сторінки. Стилі й скрипти — не текст для читача."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- сегментна сторінка -------------------------------------------------------

def test_segments_page_opens_and_says_how_many_flats(client):
    """Скільки квартир за графіком — має бути сказано людською мовою.

    Назва методу прибрана з інтерфейсу навмисно, а от кількість — ні:
    графік, який виглядає впевненіше, ніж є, гірший за складний.
    """
    r = client.get("/analytics")
    assert r.status_code == 200
    text = _text(r.text)
    assert "Порахували по" in text
    assert re.search(r"\d[\d\s\u202f]*\s+квартир", text)


def test_every_segment_row_states_how_many_objects_it_stands_on(client):
    """Графік без кількості об'єктів — цифра, якій не можна вірити."""
    rows = client.get("/api/analytics/segments").json()["segments"]
    assert rows
    assert all(r["n"] >= load().min_sample for r in rows)
    assert all(r["q1"] <= r["median_ppsqm"] <= r["q3"] for r in rows)


def test_no_line_is_drawn_below_the_sample_threshold(client):
    """Критерій приймання: жоден графік не будується на вибірці менше порога."""
    data = client.get("/api/analytics/segments").json()
    threshold = data["min_sample"]
    assert all(r["n"] >= threshold for r in data["segments"])
    assert data["below_threshold"]["threshold"] == threshold


def test_forecast_block_is_a_refusal_with_numbers_not_a_line(client):
    """Поки історії мало, замість лінії має бути число й дата."""
    forecast = client.get("/api/analytics/segments").json()["forecast"]
    assert forecast["available"] is False
    assert forecast["points_needed"] > forecast["points"]
    assert forecast["available_from"]
    text = _text(client.get("/analytics").text)
    assert "Поки мало даних" in text
    assert "поки рано казати" in text
    # Дата, до якої треба спостерігати, має бути названа.
    assert re.search(r"\d\d\.\d\d\.\d{4}", text)


def test_forecast_horizon_stays_below_a_third_of_history(client):
    """Стеля горизонту — третина спостереженого періоду, на кожній сходинці."""
    schedule = client.get("/api/analytics/segments").json()["forecast"]["schedule"]
    assert schedule
    for row in schedule:
        assert row["horizon_months"] <= row["history_months"] / 3 + 0.1


def test_sources_are_compared_within_segments_with_sample_sizes(client):
    """Критерій приймання: порівняння джерел іде по однакових сегментах."""
    sources = client.get("/api/analytics/segments").json()["sources"]
    for row in sources["table"]:
        assert len(row["cells"]) >= 2
        for cell in row["cells"].values():
            assert cell["n"] >= sources["threshold"]
    text = _text(client.get("/analytics").text)
    # Пояснення лишилось, але сказане людською мовою, без слова «сегмент».
    assert "тільки однакові квартири" in text
    assert "сегмент" not in text.lower()


def test_filters_combine_and_empty_result_is_explained(client):
    """Комбінація фільтрів, що не лишає даних, має пояснити себе."""
    r = client.get("/analytics", params={"rooms": "1", "condition": "renovated",
                                         "market": "secondary"})
    assert r.status_code == 200
    r = client.get("/analytics", params={"rooms": "4", "condition": "unknown",
                                         "market": "secondary"})
    text = _text(r.text)
    assert "Поки мало даних" in text or "квартир" in text


def test_liquidity_is_either_a_curve_or_a_numbered_refusal(client):
    """Два допустимі стани блоку — і в жодному з них немає голої цифри.

    Поки зникнень мало, блок називає, скільки їх ще треба. Коли достатньо —
    показує криву, але поруч лишає кількість подій і застереження, що
    зникнення оголошення не дорівнює продажу.
    """
    from realty.analytics import cache
    from realty.analytics.survival import Observation, estimate
    from realty.db import SessionLocal

    with SessionLocal() as s:
        universe = cache.get(s).universe
    result = estimate([Observation(days=o[0], event=o[1], entry=o[2])
                       for item in universe.items if (o := item.observation)], load())

    text = _text(client.get("/analytics").text)
    assert "не обов" in text          # застереження «зникнення ≠ продаж» завжди
    if result["available"]:
        # Числа лишаються, назва методу — ні.
        assert "зникло з продажу" in text
        assert "ще продаються" in text
    else:
        assert "зникли з продажу" in text
        assert str(load().survival_min_events) in text


def test_survival_accounts_for_listings_that_are_still_on_the_market():
    """Строк продажу рахується методом, що враховує ще не продані об'єкти."""
    from realty.analytics import cache
    from realty.analytics.survival import Observation, estimate
    from realty.db import SessionLocal

    with SessionLocal() as s:
        universe = cache.get(s).universe
    observations = [Observation(days=o[0], event=o[1], entry=o[2])
                    for item in universe.items if (o := item.observation)]
    result = estimate(observations, load())
    if not result["available"]:
        pytest.skip("подій ще замало — блок коректно відмовляється")
    assert result["censored"] > result["events"], "цензуровані мають бути враховані"
    assert result["n"] == result["events"] + result["censored"]


# --- сторінка об'єкта ---------------------------------------------------------

def _some_property(client) -> int:
    rows = client.get("/api/properties", params={"limit": 5}).json()
    return rows[0]["id"] if isinstance(rows, list) else rows["items"][0]["id"]


def test_property_page_gives_an_unambiguous_percentage(client):
    """Критерій приймання: однозначна відповідь у відсотках на сторінці об'єкта."""
    with TestClient(app) as c:
        pid = _some_property(c)
        data = c.get(f"/api/analytics/property/{pid}").json()
    verdict = data["verdict"]
    assert verdict is not None
    assert isinstance(verdict["delta_pct"], float)
    assert verdict["direction"] in ("дорожче", "дешевше", "як ринок")
    assert verdict["n"] >= load().min_sample
    text = _text(TestClient(app).get(f"/property/{pid}").text)
    assert "за схожі" in text or "на рівні ринку" in text


def test_property_price_and_price_per_sqm_agree_on_the_page(client):
    pid = _some_property(client)
    data = client.get(f"/api/analytics/property/{pid}").json()
    if data.get("area") and data.get("price_usd"):
        assert data["price_per_sqm"] == pytest.approx(
            data["price_usd"] / data["area"], rel=1e-6)


def test_price_history_counts_changes_within_a_listing_only(client):
    """Поява об'єкта на другому майданчику — не зміна ціни продавцем.

    Найнебезпечніша помилка сторінки: показати ріелтору дев'ять «змін ціни»,
    яких продавець не робив.
    """
    pid = _some_property(client)
    history = client.get(f"/api/analytics/property/{pid}").json()["history"]
    assert history["changes"] <= max(0, len(history["points"]) - 1)


def test_missing_property_is_a_normal_page_not_a_crash(client):
    r = client.get("/property/99999999")
    assert r.status_code == 404
    assert "не знайдено" in _text(r.text)


def test_analytics_link_is_present_in_the_shared_navigation(client):
    """Навігація живе в спільному layout, тож є на всіх сторінках одразу."""
    for url in ("/", "/processing", "/analytics"):
        assert '/analytics"' in client.get(url).text


# --- кеш ----------------------------------------------------------------------

def test_snapshot_is_reused_between_requests(client):
    """Сторінка сегментів не має перераховувати базу на кожне відкриття."""
    from realty.db import SessionLocal

    cache.invalidate()
    with SessionLocal() as s:
        first = cache.get(s)
        second = cache.get(s)
    assert first is second
    assert first.segments


def test_snapshot_is_rebuilt_after_deduplication_changes_masters(tmp_path):
    """Перезведення майстер-записів не змінює `listings`, але має скидати кеш."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from realty.analytics import cache as cache_mod
    from realty.models import Base, Property

    engine = create_engine(f"sqlite:///{tmp_path/'c.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    cache_mod.invalidate()
    with Session() as s:
        s.add(Property(fingerprint="a", rooms=2, area_total=50.0, price_per_sqm=1000.0))
        s.commit()
        first = cache_mod.get(s)
        s.add(Property(fingerprint="b", rooms=2, area_total=50.0, price_per_sqm=1100.0))
        s.commit()
        second = cache_mod.get(s)
    assert second is not first
    assert len(second.universe) == 2
    cache_mod.invalidate()


def test_wide_analytics_table_scrolls_inside_its_own_container(client):
    """Таблиця джерел не має розтягувати сторінку на вузькому екрані.

    Мобільні правила перетворюють рядки списку оголошень на картки; якби вони
    діяли й тут, широка таблиця виштовхнула б сторінку за межі екрана.
    """
    html = client.get("/analytics").text
    assert 'class="tablewrap scroll"' in html
    layout = Path(__file__).resolve().parent.parent / "realty/web/templates/_layout.html"
    css = layout.read_text()
    assert ".tablewrap:not(.scroll){overflow:visible}" in css
    assert ".tablewrap:not(.scroll) thead{display:none}" in css
