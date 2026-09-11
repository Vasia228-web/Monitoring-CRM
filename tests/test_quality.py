"""Шар контролю якості: пороги, карантин, ескалація, аудит."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from realty.models import Base, Condition, Listing, MarketType
from realty.quality.rules import (
    ESCALATION_MIN_BATCH, ESCALATION_RATE, PRICE_JUMP_LIMIT, Band, Thresholds,
    compute_thresholds, validate, validate_llm_output,
)
from realty.quality.staging import QualityGate


def _thresholds() -> Thresholds:
    return Thresholds(
        price_usd=Band(20_000, 220_000, 8_000, 570_000),
        price_per_sqm=Band(400, 3_900, 170, 9_300),
        area_total=Band(21, 138, 10, 280),
        sample_size=1000,
    )


def _rec(**over) -> dict:
    rec = {"source": "domria", "external_id": "1", "original_url": "https://x/1",
           "price": 70000, "price_usd": 70000.0, "rooms": 2, "location": "вул. Тестова, 1",
           "area_total": 60.0, "price_per_sqm": 1166.0}
    rec.update(over)
    return rec


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'q.db'}", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        yield s


# --- правила ------------------------------------------------------------------

def test_required_fields_block_promotion():
    t = _thresholds()
    for field in ("price", "rooms", "location", "original_url"):
        verdict, reasons = validate(_rec(**{field: None}), t)
        assert verdict == "rejected", f"{field} має бути обов'язковим"
        assert field in reasons[0]


def test_two_tier_price_bands():
    """Підозріле йде на перегляд, зламане — не пускається взагалі."""
    t = _thresholds()
    assert validate(_rec(price_usd=70_000), t)[0] == "ok"
    assert validate(_rec(price_usd=250_000), t)[0] == "review"      # поза p-межами
    assert validate(_rec(price_usd=900_000), t)[0] == "rejected"    # поза межами злому
    assert validate(_rec(price_usd=3_000), t)[0] == "rejected"


def test_sharp_price_change_goes_to_review_not_silently():
    """Регресія з реальних даних: оголошення стрибнуло з $1 504 до $77 967."""
    t = _thresholds()
    verdict, reasons = validate(_rec(price_usd=77_967), t, previous_price_usd=1_504)
    assert verdict == "review", "різкий стрибок не можна приймати мовчки"
    assert "змінилась" in " ".join(reasons)

    # Звичайна корекція ціни проходить.
    assert validate(_rec(price_usd=66_000), t, previous_price_usd=70_000)[0] == "ok"
    assert PRICE_JUMP_LIMIT < 1.0


def test_impossible_room_count_rejected():
    assert validate(_rec(rooms=40), _thresholds())[0] == "rejected"


def test_thresholds_are_derived_not_hardcoded(session):
    """Пороги мають рахуватись із даних, а не бути вписаними в код."""
    for i in range(200):
        session.add(Listing(source="t", external_id=str(i), original_url=f"u{i}",
                            currency="USD", price_usd=50_000 + i * 100,
                            price_per_sqm=1000 + i, area_total=50.0 + i * 0.1))
    session.commit()
    t = compute_thresholds(session)
    assert t.sample_size == 200
    assert 0 < t.price_usd.review_low < 50_000 < t.price_usd.review_high
    assert t.price_usd.reject_low < t.price_usd.review_low
    assert t.price_usd.reject_high > t.price_usd.review_high


# --- карантин -----------------------------------------------------------------

def test_nothing_reaches_production_unchecked(session):
    gate = QualityGate(_thresholds())
    out = gate.screen(session, [_rec(), _rec(external_id="2", price_usd=250_000)])
    assert all(r["quality_status"] in ("ok", "review", "rejected") for r in out)
    assert not any(r["quality_status"] == "pending" for r in out)
    assert gate.report.accepted == 1 and gate.report.review == 1


def test_rejected_records_are_kept_with_reason(session):
    """Дані не видаляються: відхилений запис лишається з причиною."""
    gate = QualityGate(_thresholds())
    out = gate.screen(session, [_rec(price_usd=900_000)])
    assert len(out) == 1
    assert out[0]["quality_status"] == "rejected"
    assert out[0]["quality_reason"]


def test_bad_batch_triggers_escalation_and_reports(session):
    """Критерій приймання: змодельований поганий батч зупиняє пайплайн."""
    gate = QualityGate(_thresholds())
    good = [_rec(external_id=f"g{i}", original_url=f"https://x/g{i}") for i in range(20)]
    broken = [_rec(external_id=f"b{i}", original_url=f"https://x/b{i}",
                   price_usd=900_000) for i in range(15)]

    out = gate.screen(session, good + broken)

    assert out == [], "жоден запис підозрілого пакета не має пройти"
    assert gate.report.escalated, "не спрацювала ескалація"
    assert "domria" in gate.report.escalated[0]
    assert gate.report.samples, "звіт має містити приклади"
    assert "ЕСКАЛАЦІЯ" in gate.report.render()


def test_small_batch_does_not_escalate(session):
    """На дрібному пакеті відсоток нічого не означає — не зупиняємось."""
    gate = QualityGate(_thresholds())
    tiny = [_rec(external_id=f"s{i}", original_url=f"https://x/s{i}",
                 price_usd=900_000) for i in range(ESCALATION_MIN_BATCH - 1)]
    out = gate.screen(session, tiny)
    assert out and not gate.report.escalated
    assert gate.report.rejected == len(tiny)


def test_escalation_threshold_is_sane():
    assert 0 < ESCALATION_RATE <= 0.5


# --- контроль відповідей моделі ----------------------------------------------

def test_llm_output_sanity():
    class Ok:
        price, rooms, area_total, location = 70000, 2, 60.0, "вул. Тестова"

    class Broken:
        price, rooms, area_total, location = 0, 40, 3.0, None

    assert validate_llm_output(Ok())[0] is True
    ok, problems = validate_llm_output(Broken())
    assert ok is False and len(problems) == 3
    assert validate_llm_output(None)[0] is False


# --- аудит дедуплікації -------------------------------------------------------

def test_confidence_separates_strong_and_weak_matches():
    from realty.dedup import Shape
    from realty.quality.audit import confidence

    def sh(i, **over):
        base = dict(source="domria", url=None, rooms=2, area=60.0, floor=5,
                    street="галицька", house=frozenset({"64"}), district=None,
                    price=70000.0)
        base.update(over)
        return Shape(i, base["source"], base["url"], base["rooms"], base["area"],
                     base["floor"], base["street"], base["house"], base["district"],
                     base["price"])

    strong = confidence(sh(1), sh(2))
    weak = confidence(sh(1), sh(3, house=frozenset(), street=None, floor=None))
    assert strong > weak
    assert 0.0 <= weak < strong <= 1.0


# --- сегментний поріг ціни ----------------------------------------------------

def _segmented() -> Thresholds:
    t = _thresholds()
    t.segment_median_sqm = {
        "1|renovated|primary": {"median": 1948.0, "n": 2114},
        "2|renovated|primary": {"median": 1835.0, "n": 1350},
        "3|unknown|unknown": {"median": 1000.0, "n": 5},        # замала вибірка
    }
    return t


def test_price_far_below_its_own_segment_goes_to_review():
    """Глобальна смуга такого не бачить — вона розтягнута під увесь ринок.

    Нижня межа по всій базі близько $400/м², бо туди входить і сирець. Квартира
    «з ремонтом у новобудові» за $717/м² при медіані сегмента $1 948 у цю смугу
    вписується вільно, хоч вона втричі дешевша за схожі.
    """
    rec = _rec(rooms=1, condition=Condition.RENOVATED, market_type=MarketType.PRIMARY,
               price_per_sqm=717.0, area_total=39.7, price_usd=28_476.0)
    verdict, reasons = validate(rec, _segmented())
    assert verdict == "review"
    assert any("свого сегмента" in r for r in reasons)


def test_normal_price_for_the_segment_passes():
    rec = _rec(rooms=1, condition=Condition.RENOVATED, market_type=MarketType.PRIMARY,
               price_per_sqm=2015.0, area_total=39.7, price_usd=80_000.0)
    assert validate(rec, _segmented())[0] == "ok"


def test_segment_with_too_few_objects_gives_no_verdict():
    """Медіана по п'яти об'єктах нічого не описує — не робимо з неї порогу."""
    rec = _rec(rooms=3, condition=Condition.UNKNOWN, market_type=MarketType.UNKNOWN,
               price_per_sqm=100.0, area_total=60.0, price_usd=6_000.0)
    t = _segmented()
    assert t.segment_floor(rec) is None


def test_unknown_segment_is_not_a_reason_to_flag():
    rec = _rec(rooms=9, condition=Condition.UNKNOWN, market_type=MarketType.UNKNOWN,
               price_per_sqm=500.0)
    assert _segmented().segment_floor(rec) is None


def test_rooms_above_three_share_one_segment():
    """Чотири- й п'ятикімнатні окремо — це вибірки по кілька штук."""
    t = _segmented()
    a = _rec(rooms=4, condition=Condition.RENOVATED, market_type=MarketType.PRIMARY)
    b = _rec(rooms=6, condition=Condition.RENOVATED, market_type=MarketType.PRIMARY)
    assert t.segment_key(a) == t.segment_key(b)


# --- перевірка класифікації ---------------------------------------------------

def test_declared_repair_on_an_assignment_is_flagged():
    """Карантин досі не дивився на стан узагалі — саме ця помилка найпомітніша."""
    rec = _rec(condition=Condition.RENOVATED,
               description="Вид об'єкта: Новобудова. Тип угоди: Переуступка.")
    verdict, reasons = validate(rec, _thresholds())
    assert verdict == "review"
    assert any("переуступка" in r for r in reasons)


def test_declared_repair_in_an_unfinished_building_is_flagged():
    rec = _rec(condition=Condition.RENOVATED,
               description="Здача ЖК заявлена в 2 кварталі 2028 року.")
    assert validate(rec, _thresholds())[0] == "review"


def test_repair_flag_contradicting_the_description_is_flagged():
    rec = _rec(condition=Condition.RENOVATED,
               description="Квартира продається без ремонту, сирець.")
    verdict, reasons = validate(rec, _thresholds())
    assert verdict == "review"
    assert any("протилежне" in r for r in reasons)


def test_needs_repair_contradicting_a_finished_repair_is_flagged():
    rec = _rec(condition=Condition.NEEDS_REPAIR,
               description="Зроблено дизайнерський ремонт, заходь і живи.")
    assert validate(rec, _thresholds())[0] == "review"


def test_future_tense_repair_is_not_a_contradiction():
    """«Дозволяє зробити ремонт» у записі «без ремонту» — це згода, а не конфлікт."""
    rec = _rec(condition=Condition.NEEDS_REPAIR,
               description="Площа дозволяє зробити сучасний ремонт під себе.")
    assert validate(rec, _thresholds())[0] == "ok"


def test_classification_check_needs_text_to_work_with():
    rec = _rec(condition=Condition.RENOVATED, description=None, title=None)
    assert validate(rec, _thresholds())[0] == "ok"


def test_revalidate_passes_every_field_that_validate_reads(session, monkeypatch):
    """Перевірка мовчки не працює, якщо їй не передати полів, які вона читає.

    Саме так сталося з класифікацією: правила додали б у `validate`, а
    `revalidate` далі слав би лише ціну й площу — і перевірка не спрацювала б
    жодного разу, при цьому нічого б не зламалось помітно.
    """
    import inspect

    from realty.quality import housekeeping

    source = inspect.getsource(housekeeping.revalidate)
    for field in ("condition", "market_type", "title", "description",
                  "price_per_sqm", "area_total", "rooms"):
        assert f'"{field}": row.' in source, f"revalidate не передає {field}"


def test_future_commissioning_year_contradicts_a_repair():
    """Рік введення в експлуатацію в майбутньому — квартири ще немає.

    Ознака найнадійніша з усіх: вона не залежить від того, як продавець
    сформулював опис, і саме її бракувало, щоб упіймати оголошення з
    «Рік введення в експлуатацію: 2027» і позначкою «з ремонтом».
    """
    from datetime import datetime

    rec = _rec(condition=Condition.RENOVATED, description="Гарна квартира",
               built_year=datetime.now().year + 2)
    verdict, reasons = validate(rec, _thresholds())
    assert verdict == "review"
    assert any("вводять в експлуатацію" in r for r in reasons)


def test_past_commissioning_year_is_fine():
    rec = _rec(condition=Condition.RENOVATED, description="Гарна квартира",
               built_year=2021)
    assert validate(rec, _thresholds())[0] == "ok"


def test_reclassify_only_fills_gaps_never_overwrites(tmp_path, monkeypatch):
    """Перекласифікація не має права скасовувати рішення джерела.

    Вона перечитує лише те, що збережено — заголовок, опис, рік. Параметрів
    картки («Вид об'єкта: Вторинний ринок») у базі немає, тож її здогад
    слабший за те, що вже стоїть. Перший варіант цієї функції перезаписував
    усе підряд і за один прогін стер 810 явних заяв джерела.
    """
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from realty.quality import housekeeping

    engine = create_engine(f"sqlite:///{tmp_path/'rc.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(housekeeping, "session_scope", scope)
    with Session() as s:
        # Текст кричить «новобудова», але джерело сказало «вторинний ринок».
        declared = Listing(source="olx", external_id="1", original_url="https://x/1",
                           title="Квартира в ЖК Манхеттен, новобудова",
                           market_type=MarketType.SECONDARY,
                           condition=Condition.RENOVATED)
        blank = Listing(source="olx", external_id="2", original_url="https://x/2",
                        title="Квартира в ЖК Манхеттен, новобудова",
                        market_type=MarketType.UNKNOWN,
                        condition=Condition.UNKNOWN)
        s.add_all([declared, blank])
        s.commit()
        ids = (declared.id, blank.id)

    housekeeping.reclassify()

    with Session() as s:
        kept = s.get(Listing, ids[0])
        filled = s.get(Listing, ids[1])
        assert kept.market_type is MarketType.SECONDARY, "рішення джерела скасовано"
        assert filled.market_type is MarketType.PRIMARY, "порожнє поле не заповнене"
