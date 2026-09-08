"""Шар контролю якості: пороги, карантин, ескалація, аудит."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from realty.models import Base, Listing
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
