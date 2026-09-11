"""Подача аналітики: без назв методів, але без втрати чесності.

Прибирається СПОСІБ РОЗРАХУНКУ, а не застереження. Графік, який виглядає
впевненіше, ніж є насправді, гірший за складний — тому кількість об'єктів,
межі невизначеності й підпис про те, що зникнення оголошення не дорівнює
продажу, лишаються обов'язковими.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty.web.app import app

# Назви методів і математичних функцій, яким не місце в інтерфейсі.
JARGON = [
    # назви методів і математичних функцій
    "медіан", "квартил", "міжквартильн", "регрес", "кореляц",
    "каплан", "меєр", "mape", "тьюкі", "цензур", "довірчий інтервал",
    "бектест", "аналіз виживання", "крива виживання", "екстраполяц",
    "дисперс", "стандартне відхилення", "p-значення",
    # технічна мова, якою легко описати те саме простіше
    "вибірк", "сегмент", "горизонт прогноз", "тижнев", "порог", "поріг",
    "ліквідніст", "тренд", "дедуплікац", "спостережен",
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def visible(html: str) -> str:
    """Те, що бачить читач: без стилів, скриптів і розмітки."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    import html as html_mod

    return html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))


def _property_id() -> int:
    from sqlalchemy import select

    from realty.db import SessionLocal
    from realty.models import Property

    with SessionLocal() as s:
        return s.scalars(select(Property.id).limit(1)).first()


# --- чого не має бути --------------------------------------------------------

@pytest.mark.parametrize("url", ["/analytics", "/"])
def test_no_method_names_in_the_interface(client, url):
    text = visible(client.get(url).text).lower()
    found = [w for w in JARGON if w in text]
    assert not found, f"{url}: у тексті лишились назви методів: {found}"


def test_property_page_is_free_of_method_names(client):
    text = visible(client.get(f"/property/{_property_id()}",
                              params={"verify": "0"}).text).lower()
    found = [w for w in JARGON if w in text]
    assert not found, f"у картці об'єкта лишились назви методів: {found}"


def test_chart_titles_are_conclusions_not_variable_names(client):
    """Заголовок має говорити, що видно, а не як називається величина."""
    html = client.get("/analytics").text
    titles = [visible(t).strip() for t in re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S)]
    assert titles
    for title in titles:
        assert "динаміка" not in title.lower()
        assert "розподіл" not in title.lower()
        assert "у розрізі" not in title.lower()
    # Хоча б один заголовок має містити число — тобто бути висновком.
    assert any(re.search(r"\d", t) for t in titles), titles


# --- що має лишитись обов'язково ---------------------------------------------

def test_every_chart_says_how_many_flats_it_stands_on(client):
    """Не «n=34», а «34 квартири». Але сказати треба."""
    text = visible(client.get("/analytics").text)
    assert "Порахували по" in text
    assert re.search(r"\d[\d\s ]*\s+квартир", text)


def test_uncertainty_is_shown_as_a_range_not_hidden(client):
    """Межі невизначеності лишаються — просто названі людською мовою."""
    html = client.get("/analytics").text
    assert 'class="spread"' in html, "смуга розкиду має бути на графіку"
    assert "у більшості квартир" in visible(html)


def test_missing_data_looks_like_a_normal_state(client):
    """«Поки мало даних» — штатний елемент, а не технічна помилка."""
    text = visible(client.get("/analytics").text)
    assert "Поки мало даних" in text
    for bad in ("помилка", "error", "exception", "недостатньо даних"):
        assert bad not in text.lower(), bad


def test_disappearance_is_not_called_a_sale(client):
    """Застереження лишається одним простим реченням."""
    text = visible(client.get("/analytics").text)
    assert "не обов" in text and "продаж" in text


def test_forecast_never_appears_without_its_limits(client):
    """Лінії прогнозу без меж невизначеності не існує.

    Поки прогнозу немає, сторінка має називати дату, з якої він з'явиться, —
    а не просто мовчати.
    """
    data = client.get("/api/analytics/segments").json()["forecast"]
    text = visible(client.get("/analytics").text)
    if data["available"]:
        assert "десь у цих межах" in text or "розкид" in text
    else:
        assert re.search(r"\d\d\.\d\d\.\d{4}", text), "дата готовності не названа"


# --- порівняння з ринком ------------------------------------------------------

def test_list_compares_with_similar_flats_not_the_whole_base(client):
    """Раніше кожен рядок показував «вище медіани по всій базі».

    Медіана по всій базі змішує однокімнатні з п'ятикімнатними, новобудови з
    сирцем і ремонт із його відсутністю — через це вся видача виходила «вище
    медіани», і цифра переставала бути порівнянням.
    """
    text = visible(client.get("/", params={"per_page": 50}).text)
    assert "від медіани" not in text
    assert "за схожі" in text or "як у схожих" in text


def test_comparison_is_an_unambiguous_phrase(client):
    """Дорожче чи дешевше й на скільки — словами, а не самим лише кольором."""
    html = client.get("/", params={"per_page": 50, "sort": "price_asc"}).text
    text = visible(html)
    assert re.search(r"(дорожче|дешевше) за схожі", text)
    # Значення дублюється стрілкою: колір сам по собі не несе змісту.
    assert "↑" in text or "↓" in text


def test_thin_segments_say_so_instead_of_inventing_a_number(client):
    """Якщо схожих квартир мало — так і написано, а не порожнє місце."""
    from realty.web.app import _peer_comparison
    from realty.quality.rules import Thresholds, Band

    class Row:
        price_per_sqm = 1000.0
        rooms = 9
        condition = None
        market_type = None

    t = Thresholds(price_usd=Band(1, 2, 3, 4), price_per_sqm=Band(1, 2, 3, 4),
                   area_total=Band(1, 2, 3, 4))
    result = _peer_comparison(Row(), t)
    assert result["known"] is False
    assert "мало схожих" in result["reason"]
