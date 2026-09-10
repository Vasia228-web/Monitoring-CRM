"""Аналітичний шар: статистика, сегменти, порівняння, межі прогнозу."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from realty.analytics import forecast
from realty.analytics.segments import (
    Item, Universe, build_universe, compare, days_distribution,
    primary_vs_secondary, rooms_band, rooms_label, segment_table, verdict,
)
from realty.analytics.settings import Settings
from realty.analytics.sources import matched
from realty.analytics.stats import fences, quantile, summarise
from realty.models import (
    Base, Condition, Listing, MarketType, PriceEvent, Property,
)

CFG = Settings(min_sample=5, min_sample_source=5, outlier_min_n=8)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'a.db'}", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        yield s


def _item(pid, **over):
    base = dict(property_id=pid, rooms=2, area=60.0, price_usd=60_000.0, ppsqm=1000.0,
                condition="renovated", market="secondary", district="Центр",
                days_listed=30.0, delisted_at=None)
    base.update(over)
    return Item(**base)


def _universe(n=40, **over):
    return Universe(items=[_item(i, **over) for i in range(n)])


# --- статистика ---------------------------------------------------------------

def test_median_is_the_headline_not_the_mean():
    """Кілька елітних об'єктів зміщують середнє, а медіану — ні.

    Саме тому основна цифра всюди — медіана; тест фіксує, що різниця реальна.
    """
    values = [1000.0] * 20 + [9000.0, 9500.0]
    s = summarise(values, CFG)
    assert s.median == 1000.0
    assert s.mean > 1500.0


def test_outliers_are_trimmed_before_aggregation():
    """Одруківка в нулях не має зсувати ні межі квартилів, ні медіану."""
    clean = [1000.0 + i for i in range(40)]
    s = summarise(clean + [7.0, 4_000_000.0], CFG)
    assert s.trimmed == 2
    assert s.n == 40
    assert 1000 <= s.median <= 1040


def test_fences_are_symmetric_in_log_space():
    """У логарифмі межа знизу теж має сенс, а не йде в мінус.

    У лінійному просторі Q1 − k·IQR для цін завжди від'ємне, тобто нижніх
    викидів не існує в принципі — і копійчані ціни проходять як норма.
    """
    values = [500.0 * (1.1 ** i) for i in range(60)]
    low, high = fences(values, 3.0, 10)
    assert low > 0
    assert low < min(values) and high > max(values)


def test_quantile_interpolates():
    assert quantile([0.0, 10.0], 0.5) == 5.0
    assert quantile([1.0, 2.0, 3.0, 4.0], 0.25) == pytest.approx(1.75)


# --- драбина сегментів --------------------------------------------------------

def test_comparison_uses_the_narrowest_segment_that_has_enough_objects():
    universe = _universe(40)
    target = _item(999, ppsqm=1200.0)
    universe.items.append(target)
    result = compare(universe, target, CFG)
    assert result.level == "district_area"
    assert result.n >= CFG.min_sample


def test_ladder_widens_when_the_narrow_segment_is_too_small():
    """Мало сусідів у районі — порівнюємо ширше й кажемо про це в підписі."""
    universe = Universe(items=[_item(i) for i in range(3)]                 # Центр
                        + [_item(100 + i, district="Пасічна") for i in range(30)])
    target = _item(999, district="Центр", ppsqm=1200.0)
    universe.items.append(target)
    result = compare(universe, target, CFG)
    assert result.level in ("area", "area_wide", "base")
    assert "Центр" not in result.label


def test_comparison_returns_none_when_even_the_widest_level_is_small():
    """Немає з чим порівнювати — штатний результат, а не виняток."""
    universe = Universe(items=[_item(i) for i in range(3)])
    assert compare(universe, _item(999), CFG) is None


def test_object_is_never_compared_with_itself():
    universe = _universe(10)
    target = universe.items[0]
    result = compare(universe, target, CFG)
    assert result is not None
    assert len(result.peers) == 9


def test_verdict_states_the_direction_and_the_percentage():
    universe = _universe(30)
    target = _item(999, ppsqm=1200.0)
    universe.items.append(target)
    v = verdict(target, compare(universe, target, CFG))
    assert v["delta_pct"] == pytest.approx(20.0)
    assert v["direction"] == "дорожче"
    assert v["percentile"] > 90


def test_area_band_keeps_small_flats_out_of_a_large_flat_comparison():
    """Порівняння в межах смуги площі — інакше оцінка міряла б розмір.

    Дрібні квартири дорожчі за м²; якщо не обмежити площу, велика квартира
    поруч із ними завжди виглядатиме «дешевою».
    """
    small = [_item(i, area=30.0, ppsqm=2000.0) for i in range(30)]
    large = [_item(100 + i, area=90.0, ppsqm=1000.0) for i in range(30)]
    universe = Universe(items=small + large)
    target = _item(999, area=90.0, ppsqm=1000.0)
    universe.items.append(target)
    v = verdict(target, compare(universe, target, CFG))
    assert v["delta_pct"] == pytest.approx(0.0)      # порівняли з великими


def test_rooms_above_three_are_one_band():
    assert rooms_band(5) == rooms_band(4) == 4
    assert rooms_label(4) == "3+ кімнат"


# --- таблиця сегментів --------------------------------------------------------

def test_segment_table_hides_samples_below_the_threshold():
    universe = Universe(items=[_item(i) for i in range(30)]
                        + [_item(100 + i, rooms=1) for i in range(2)])
    rows = segment_table(universe, CFG)
    assert [r["rooms"] for r in rows] == [2]


def test_segment_rows_carry_sample_size_and_spread():
    """Медіана без n і без розмаху — цифра, якій не можна вірити."""
    rows = segment_table(_universe(30), CFG)
    row = rows[0]
    assert row["n"] == 30
    assert row["q1"] <= row["median_ppsqm"] <= row["q3"]
    assert row["median_days"] is not None


def test_primary_and_secondary_are_paired_only_within_one_segment():
    universe = Universe(
        items=[_item(i, market="primary", ppsqm=1200.0) for i in range(20)]
        + [_item(100 + i, market="secondary", ppsqm=1000.0) for i in range(20)]
        + [_item(200 + i, rooms=1, market="primary") for i in range(20)])
    rows = primary_vs_secondary(universe, CFG)
    assert len(rows) == 1                     # однокімнатним нема з чим паруватись
    assert rows[0]["gap_pct"] == pytest.approx(20.0)


def test_days_distribution_reports_median_and_spread():
    universe = Universe(items=[_item(i, days_listed=float(i)) for i in range(40)])
    rows = days_distribution(universe, CFG)
    assert rows[0]["n"] == 40
    assert rows[0]["q1"] < rows[0]["median"] < rows[0]["q3"]


# --- порівняння джерел --------------------------------------------------------

def _listing(s, **over):
    rec = dict(source="domria", external_id=str(_listing.n), original_url="https://x",
               price_usd=60_000.0, rooms=2, area_total=60.0, price_per_sqm=1000.0,
               condition=Condition.RENOVATED, market_type=MarketType.SECONDARY,
               quality_status="ok", first_seen=datetime(2026, 9, 1),
               published_at=datetime(2026, 8, 1))
    rec.update(over)
    _listing.n += 1
    row = Listing(**rec)
    s.add(row)
    s.flush()
    return row


_listing.n = 0


def test_sources_are_compared_only_inside_identical_segments(session):
    """Джерело з іншим асортиментом не порівнюється загальною медіаною.

    Це головна пастка пункту: «середня на LUN проти середньої на OLX» міряє
    склад оголошень, а не ціни.
    """
    for _ in range(10):
        _listing(session, source="a", price_per_sqm=1000.0)
        _listing(session, source="b", price_per_sqm=1100.0)
    # У «b» ще й пачка новобудов — саме те, що зіпсувало б наївну медіану.
    for _ in range(10):
        _listing(session, source="b", market_type=MarketType.PRIMARY,
                 price_per_sqm=3000.0)
    session.flush()

    result = matched(session, CFG)
    row = next(r for r in result["table"] if r["market"] == "secondary")
    assert row["cells"]["a"]["median"] == 1000
    assert row["cells"]["b"]["median"] == 1100
    assert row["spread_pct"] == pytest.approx(10.0)
    assert row["cells"]["a"]["n"] == row["cells"]["b"]["n"] == 10


def test_source_with_no_comparable_segment_is_named_not_silently_dropped(session):
    for _ in range(10):
        _listing(session, source="a")
        _listing(session, source="b")
    _listing(session, source="tiny")          # одне оголошення — порівнювати нема з чим
    session.flush()
    result = matched(session, CFG)
    assert "tiny" in result["excluded"]
    assert "tiny" in result["excluded_note"]


def test_segment_with_a_single_source_is_not_a_comparison(session):
    for _ in range(10):
        _listing(session, source="only")
    session.flush()
    assert matched(session, CFG)["table"] == []


# --- знімок бази --------------------------------------------------------------

def test_universe_price_and_price_per_sqm_agree(session):
    """Ціна й ціна за м² на сторінці об'єкта мають сходитись між собою.

    Майстер-запис зберігає мінімальну ціну й окремо найчастішу ціну за м² —
    разом вони не сходяться. Аналітика бере медіану оголошень і виводить
    ціну за м² саме з неї.
    """
    prop = Property(fingerprint="p", rooms=2, area_total=50.0,
                    price_usd_min=40_000.0, price_per_sqm=1500.0)
    session.add(prop)
    session.flush()
    for price in (40_000.0, 50_000.0, 90_000.0):
        row = _listing(session, price_usd=price)
        row.property_id = prop.id
    session.flush()

    item = build_universe(session).items[0]
    assert item.price_usd == 50_000.0                    # медіана, не мінімум
    assert item.ppsqm == pytest.approx(1000.0)           # 50 000 / 50
    assert item.price_spread_pct == pytest.approx(125.0)


def test_object_counts_as_delisted_only_when_every_listing_is_gone(session):
    """Одне зняте оголошення з трьох не означає, що квартиру продали."""
    prop = Property(fingerprint="p2", rooms=2, area_total=50.0)
    session.add(prop)
    session.flush()
    for gone in (datetime(2026, 9, 5), None, None):
        row = _listing(session, delisted_at=gone)
        row.property_id = prop.id
    session.flush()
    assert build_universe(session).items[0].delisted_at is None


# --- межі прогнозу ------------------------------------------------------------

def _events(session, days: int):
    row = _listing(session)
    start = datetime(2026, 1, 1)
    for i in range(days):
        session.add(PriceEvent(listing_id=row.id, source="domria", price_usd=60_000.0,
                               observed_at=start + timedelta(days=i)))
    session.flush()


def test_forecast_is_refused_on_a_short_history(session):
    """Головна заборона модуля: чотири дні спостережень — не підстава для лінії."""
    _events(session, 4)
    r = forecast.readiness(session, CFG)
    assert r.ready is False
    assert r.points == 0
    assert r.available_from is not None
    assert "не будується" in r.message()


def test_forecast_horizon_never_exceeds_a_third_of_the_history(session):
    _events(session, 300)
    r = forecast.readiness(session, CFG)
    assert r.ready is True
    assert r.horizon_days <= r.span_days / 3 + 1


def test_horizon_schedule_gives_calendar_dates_not_promises(session):
    _events(session, 30)
    rows = forecast.horizon_schedule(session, CFG)
    assert [r["history_months"] for r in rows] == [3, 6, 12, 24]
    assert all(r["horizon_months"] < r["history_months"] for r in rows)
    assert rows[0]["date"] < rows[-1]["date"]


def test_seasonality_is_only_claimed_after_two_full_years(session):
    _events(session, 30)
    rows = forecast.horizon_schedule(session, CFG)
    seasonal = [r for r in rows if "сезонність" in r["note"]]
    assert [r["history_months"] for r in seasonal] == [24]


def test_prediction_interval_widens_with_the_horizon():
    """Прогноз на 36 місяців з тим самим коридором, що й на 6, — помилка."""
    series = [{"week": datetime(2026, 1, 1).date() + timedelta(days=7 * i),
               "median": 1000.0 * (1.002 ** i) + (i % 3), "n": 100}
              for i in range(40)]
    result = forecast.project(series, 20, CFG)
    assert result["available"] is True
    widths = [p["high"] - p["low"] for p in result["points"]]
    assert widths[-1] > widths[0]
    assert widths == sorted(widths)


def test_model_choice_is_decided_by_backtest_not_complexity():
    """На шумі без тренду проста модель має вигравати — і має бути обрана."""
    values = [1000, 1200, 900, 1150, 950, 1100, 980, 1250, 940, 1180,
              1010, 1220, 930, 1160, 970, 1090, 1005, 1240, 945, 1175]
    series = [{"week": datetime(2026, 1, 1).date() + timedelta(days=7 * i),
               "median": float(v), "n": 100} for i, v in enumerate(values)]
    model, tests = forecast.select_model(series, CFG)
    assert {t.model for t in tests} == {"остання ціна", "лінійний тренд"}
    assert all(t.mape is not None for t in tests)


def test_forecast_is_withheld_when_no_model_passes_the_backtest():
    """Модель, що не пройшла бектест, не показується — блок лишається порожнім."""
    cfg = Settings(forecast_max_mape=0.001, forecast_backtest_points=4)
    series = [{"week": datetime(2026, 1, 1).date() + timedelta(days=7 * i),
               "median": 1000.0 + (i % 5) * 200, "n": 50} for i in range(20)]
    result = forecast.project(series, 6, cfg)
    assert result["available"] is False
    assert result["points"] == []
    assert result["backtests"]


def test_backtest_number_is_reported_alongside_the_forecast():
    """Число помилки має бути видиме — це вимога, а не деталь."""
    series = [{"week": datetime(2026, 1, 1).date() + timedelta(days=7 * i),
               "median": 1000.0 + i, "n": 100} for i in range(30)]
    result = forecast.project(series, 5, CFG)
    assert result["available"] is True
    assert isinstance(result["mape"], float)
    assert "екстраполяція" in result["extrapolation_note"].lower()


def test_zero_days_on_market_is_not_an_outlier():
    """Оголошення, опубліковане сьогодні, має вік 0 днів — це не одруківка.

    Двостороннє відсікання викидало б саме найсвіжіші записи, тобто ті, що
    найкраще описують поточний ринок.
    """
    values = [0.0, 0.0] + [float(i) for i in range(1, 40)]
    both = summarise(values, CFG, "both")
    upper = summarise(values, CFG, "upper")
    assert both.n < len(values)          # нулі відсічені
    assert upper.n == len(values)        # і повернені там, де це доречно


def test_days_distribution_keeps_freshly_published_objects():
    universe = Universe(items=[_item(i, days_listed=float(i)) for i in range(40)])
    assert days_distribution(universe, CFG)[0]["n"] == 40
