"""Інвентаризація бази: чи правильно вона рахує те, від чого залежать рішення."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from realty.analytics import inventory
from realty.models import Base, Condition, Listing, MarketType, PriceEvent, Property


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'inv.db'}", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        yield s


def _listing(s, prop=None, **over):
    rec = dict(source="domria", external_id=str(_listing.n), original_url="https://x",
               price_usd=70000.0, rooms=2, area_total=60.0, price_per_sqm=1166.0,
               quality_status="ok", first_seen=datetime(2026, 9, 1))
    rec.update(over)
    _listing.n += 1
    row = Listing(**rec)
    if prop is not None:
        row.property = prop
    s.add(row)
    s.flush()
    return row


_listing.n = 0


def _event(s, listing, price, day):
    s.add(PriceEvent(listing_id=listing.id, source=listing.source, price=price,
                     price_usd=price, observed_at=datetime(2026, 9, day)))


def test_price_change_counted_only_when_price_actually_moved(session):
    """Базова подія при появі оголошення — це не зміна ціни.

    Найдорожча помилка інвентаризації: порахувати 16 тисяч базових записів як
    «історію» і на цій підставі дозволити прогноз.
    """
    prop_still = Property(fingerprint="a")
    prop_moved = Property(fingerprint="b")
    session.add_all([prop_still, prop_moved])
    session.flush()

    still = _listing(session, prop_still)
    _event(session, still, 70000, 1)                      # тільки поява

    moved = _listing(session, prop_moved)
    _event(session, moved, 70000, 1)
    _event(session, moved, 68000, 3)                      # справжня зміна
    session.flush()

    h = inventory.price_history_depth(session)
    assert h["events"] == 3
    assert h["masters_with_any_history"] == 2
    assert h["listings_with_price_change"] == 1
    assert h["masters_with_price_change"] == 1
    assert h["span_days"] == 2.0


def test_two_listings_of_one_master_are_not_a_price_change(session):
    """Дві появи того самого об'єкта з різних джерел — не рух ціни.

    Обидва оголошення дають по базовій події в різні дні; наївний підрахунок
    «більше однієї дати» зарахував би це за зміну ціни, якої не було.
    """
    prop = Property(fingerprint="c")
    session.add(prop)
    session.flush()
    a = _listing(session, prop, source="domria")
    b = _listing(session, prop, source="lun")
    _event(session, a, 70000, 1)
    _event(session, b, 71000, 4)
    session.flush()

    h = inventory.price_history_depth(session)
    assert h["masters_with_price_change"] == 0


def test_segments_are_counted_on_masters_not_listings(session):
    """Та сама квартира з трьох майданчиків не робить сегмент утричі більшим."""
    for i in range(3):
        prop = Property(fingerprint=f"p{i}", rooms=2, area_total=60.0,
                        price_per_sqm=1100.0, condition=Condition.RENOVATED,
                        market_type=MarketType.SECONDARY)
        session.add(prop)
        session.flush()
        for _ in range(3):                                # три джерела на об'єкт
            _listing(session, prop)
    session.flush()

    seg = inventory.segments(session, min_size=2)
    assert seg["rooms_condition_market"]["total"] == 3
    assert seg["table"][0]["n"] == 3


def test_rooms_above_three_collapse_into_one_band(session):
    """4к і 5к окремо — це вибірки по кілька об'єктів; зводимо їх у «3+»."""
    for i, rooms in enumerate((4, 5, 6)):
        session.add(Property(fingerprint=f"r{i}", rooms=rooms, area_total=100.0,
                             price_per_sqm=1000.0))
    session.flush()
    seg = inventory.segments(session, min_size=1)
    assert [r["rooms"] for r in seg["table"]] == [4]
    assert seg["table"][0]["n"] == 3


def test_lifecycle_reports_sample_size_with_the_median(session):
    """Медіана строку життя без кількості спостережень — небезпечна цифра."""
    live = _listing(session)
    gone = _listing(session, first_seen=datetime(2026, 9, 1),
                    delisted_at=datetime(2026, 9, 11), last_checked=datetime(2026, 9, 11))
    session.flush()
    lc = inventory.lifecycle(session)
    assert lc["listings"] == 2
    assert lc["delisted"] == 1
    assert lc["observed_days_median"] == 10.0
    assert lc["ever_checked"] == 1


def test_source_composition_exposes_the_assortment_difference(session):
    """Наївна медіана по джерелу має йти в парі з часткою новобудов."""
    for _ in range(3):
        _listing(session, source="a", market_type=MarketType.PRIMARY, price_per_sqm=1000.0)
    _listing(session, source="b", market_type=MarketType.SECONDARY, price_per_sqm=1600.0)
    session.flush()
    comp = inventory.source_composition(session)
    assert comp["a"]["primary_share"] == 100.0
    assert comp["b"]["primary_share"] == 0.0
    assert comp["a"]["n"] == 3


def test_report_renders_on_an_empty_database(tmp_path, monkeypatch):
    """«Даних немає» — штатний стан, а не падіння."""
    from realty.analytics import report

    engine = create_engine(f"sqlite:///{tmp_path/'empty.db'}", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(inventory, "SessionLocal",
                        sessionmaker(bind=engine, future=True))
    text = report.render()
    assert "ІНВЕНТАРИЗАЦІЯ" in text


def test_check_rate_is_measured_not_assumed(session, monkeypatch):
    """Швидкість обходу бази береться з фактичних позначок перевірки.

    Розмір порції в налаштуваннях і реальна швидкість — різні речі: частина
    прогонів упирається в блокування або не доходить до кінця. Від цієї цифри
    залежить прогноз, коли з'явиться крива виживання, тож вигадувати її не можна.
    """
    now = datetime(2026, 9, 10, 12, 0)
    monkeypatch.setattr(inventory, "_now", lambda: now)
    for _ in range(10):
        _listing(session, last_checked=now - timedelta(hours=5))
    for _ in range(90):
        _listing(session, last_checked=None)
    session.flush()

    rate = inventory.check_rate(session)
    assert rate["total"] == 100
    assert rate["per_day"] == 10
    assert rate["checked"] == 10
    assert rate["full_cycle_days"] == 10.0


def test_check_rate_says_nothing_rather_than_guessing(session):
    """Жодної перевірки за добу — повертаємо None, а не поділ на нуль."""
    _listing(session, last_checked=None)
    session.flush()
    assert inventory.check_rate(session)["full_cycle_days"] is None
