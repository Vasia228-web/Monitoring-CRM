"""Регресійні тести нормалізації — сюди йдуть усі знайдені баги парсерів."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.models import Condition, MarketType
from realty.normalize import (
    is_assignment,
    classify_condition, classify_market, compute_price_per_sqm, in_ivano_frankivsk,
    parse_area, parse_date, parse_price, parse_rooms,
)


def test_parse_price():
    assert parse_price("3 741 608.64 грн.") == (3741608.64, "UAH")
    assert parse_price("$ 84 тис.") == (84000.0, "USD")
    assert parse_price("170 000 USD") == (170000.0, "USD")
    assert parse_price("1.2 млн грн") == (1200000.0, "UAH")
    assert parse_price(None) == (None, "USD")


def test_parse_rooms_word_forms_win_over_nearby_numbers():
    # Регресія: «двокімнатна 60,2м» раніше давала 6 (ловилась площа).
    assert parse_rooms("Опришівська Слобода, двокімнатна 60,2м, 2 386 522 грн") == 2
    assert parse_rooms("Терміново продам 1к квартиру Височана 18") == 1
    assert parse_rooms("Продаж видової 2 кім. квартири") == 2
    assert parse_rooms("3-кім квартира з лоджією 18м²") == 3
    assert parse_rooms("кімн. 3") == 3
    assert parse_rooms("К-сть кімнат | 2") == 2
    assert parse_rooms("Квартира на 1 поверсі в центрі, 55 м²") is None


def test_parse_area():
    assert parse_area("85 м²") == 85.0
    assert parse_area("56.50 м²") == 56.5
    assert parse_area("5000 м²") is None  # поза правдоподібним діапазоном


def test_parse_date():
    assert parse_date("2026-09-05T17:50:58").isoformat().startswith("2026-09-05")
    assert parse_date("Івано-Франківськ - 05 вересня 2026 р.").date().isoformat() == "2026-09-05"
    assert parse_date("30 квіт").month == 4
    assert parse_date(None) is None


def test_classify_condition_negation_first():
    # «без ремонту» містить «ремонт» — заперечення має вигравати.
    assert classify_condition("квартира без ремонту") is Condition.NEEDS_REPAIR
    assert classify_condition("однокімнатний сирець") is Condition.NEEDS_REPAIR
    assert classify_condition("свіжий ремонт, заїжджай і живи") is Condition.RENOVATED
    assert classify_condition("продам квартиру") is Condition.UNKNOWN


def test_classify_market():
    assert classify_market("квартира від забудовника") is MarketType.PRIMARY
    assert classify_market("хрущовка в центрі") is MarketType.SECONDARY
    # Сама назва ЖК — недостатній сигнал: у ЖК буває і перепродаж.
    assert classify_market(complex_name="ЖК Паркова Алея") is MarketType.UNKNOWN


def test_geo_filter_is_strict():
    assert in_ivano_frankivsk("вул. Галицька, Івано-Франківськ")
    assert in_ivano_frankivsk(lat=48.9188, lon=24.7097)
    assert in_ivano_frankivsk("Івано-Франківськ - 05 вересня 2026 р.")
    # Регресія: назва області не робить об'єкт міським.
    assert not in_ivano_frankivsk("м. Коломия, Івано-Франківська обл.")
    assert not in_ivano_frankivsk("с. Угорники, Івано-Франківський район")
    assert not in_ivano_frankivsk("Тернопіль, вул. Валова")
    assert not in_ivano_frankivsk(lat=49.55, lon=25.59)  # Тернопіль за координатами


def test_price_per_sqm():
    assert compute_price_per_sqm(75000, 42.5) == 1764.71
    assert compute_price_per_sqm(75000, 42.5, given=1765) == 1765
    assert compute_price_per_sqm(None, 42.5) is None


def test_ria_price_item_is_not_usd():
    """Регресія: RIA віддає price_item у валюті оголошення.

    Для гривневого оголошення 75 000 грн / 86 м² = 872 грн/м². Якщо взяти це
    число як $/м², виходить абсурд — доларову шкалу треба брати з priceItemArr.
    """
    from realty.sources.domria import _CURRENCY_BY_ID, _USD_KEY

    assert _CURRENCY_BY_ID[3] == "UAH"
    card = {"currency_type_id": 3, "price_item": 872, "priceItemArr": {"1": 20, "3": 872}}
    usd = card["priceItemArr"][_USD_KEY]
    assert usd == 20 and usd != card["price_item"]


def test_condition_from_ria_tags():
    """Теги RIA «З обробкою» / «Обставлена» означають готовність до проживання."""
    assert classify_condition("З обробкою, З балконом") is Condition.RENOVATED
    assert classify_condition("Обставлена, Без АН") is Condition.RENOVATED
    # Заперечення все одно сильніше.
    assert classify_condition("Без обробки, З балконом") is Condition.NEEDS_REPAIR


def test_unbuilt_primary_counts_as_needs_repair():
    """Про ремонт у ЖК, який ще будується, не пишуть — квартири поки немає."""
    txt = "Продається квартира від забудовника. Здача ЖК City заявлена в 1 кварталі 2028 року."
    assert classify_condition(txt, market=MarketType.PRIMARY) is Condition.NEEDS_REPAIR
    # Без підказки про ринок правило не спрацьовує — здогад має бути обґрунтованим.
    assert classify_condition(txt) is Condition.UNKNOWN
    # Раніше тут поверталось «з ремонтом»: мовляв, явна згадка перемагає
    # правило. Але ремонт у квартирі, якої ще немає, — це обіцянка забудовника,
    # а не факт; саме такі оголошення й потрапляли у фільтр «з ремонтом», після
    # чого покупець відкривав недобудову. Чесна відповідь — «не визначено».
    assert classify_condition(txt + " Квартира з дизайнерським ремонтом.",
                              market=MarketType.PRIMARY) is Condition.UNKNOWN


def test_building_series_imply_secondary_market():
    """RIA й OLX позначають серію забудови тегом — це надійна ознака вторинки."""
    assert classify_market("З цегли, Чеський проект") is MarketType.SECONDARY
    assert classify_market("Тип будинку: Житловий фонд 2011-2020-і") is MarketType.SECONDARY
    assert classify_market("гостинка в центрі") is MarketType.SECONDARY
    # Новобудова не має випадково потрапити у вторинку.
    assert classify_market("Житловий фонд 2021-2025, від забудовника") is MarketType.PRIMARY


# --- ремонт, якого ще немає ---------------------------------------------------

def test_future_tense_repair_is_not_a_repair():
    """«Дозволяє зробити ремонт» — це запрошення, а не стан квартири.

    Саме на цій фразі шаблон «сучасн\\w+ ремонт» спрацьовував і ставив
    «з ремонтом» оголошенню про голі стіни.
    """
    for text in (
        "Площа дозволяє зробити сучасний ремонт під себе",
        "Зроблю ремонт під покупця",
        "Можливий ремонт за домовленістю",
        "Потрібно зробити капітальний ремонт",
        "Ремонт під себе",
        "Ремонт на ваш смак",
    ):
        assert classify_condition(text) is Condition.UNKNOWN, text


def test_past_tense_repair_still_counts():
    """Різниця між «зробити» і «зроблено» — один склад, і вона вирішальна."""
    for text in ("Зроблено дизайнерський ремонт",
                 "Зроблений капітальний ремонт",
                 "Квартира після ремонту"):
        assert classify_condition(text) is Condition.RENOVATED, text


def test_assignment_cancels_a_declared_repair():
    """Переуступка — продаж права за договором; квартири фізично немає.

    Продавець ставить у картці «Ремонт: Євроремонт», маючи на увазі, якою її
    здадуть. Покупець читає це як опис того, що побачить. Кажемо «не
    визначено» — стверджувати «без ремонту» ми теж не маємо підстав.
    """
    text = "Вид об'єкта: Новобудова | Тип угоди: Переуступка | Ремонт: Євроремонт"
    assert classify_condition(text, declared=Condition.RENOVATED) is Condition.UNKNOWN
    assert is_assignment(text)
    assert not is_assignment("Вид об'єкта: Новобудова | Ремонт: Євроремонт")


def test_declared_field_wins_when_the_flat_exists():
    """Поле, заповнене продавцем, — найкращий сигнал для готової квартири."""
    text = "Вид об'єкта: Новобудова | Ремонт: Авторський проект | Поверх: 14"
    assert classify_condition(text, declared=Condition.RENOVATED) is Condition.RENOVATED


def test_explicit_denial_beats_the_declared_field():
    """Якщо в описі сказано «без ремонту», поле в картці не рятує."""
    text = "Ремонт: Євроремонт. Квартира продається без ремонту, сирець."
    assert classify_condition(text, declared=Condition.RENOVATED) is Condition.NEEDS_REPAIR


def test_unbuilt_without_any_repair_claim_is_still_needs_repair():
    """Правило про сирець у новобудові лишилось там, де воно обґрунтоване."""
    txt = "Продається квартира від забудовника. Здача ЖК заявлена в 1 кварталі 2028 року."
    assert classify_condition(txt, market=MarketType.PRIMARY) is Condition.NEEDS_REPAIR
