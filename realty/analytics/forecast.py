"""Прогноз ціни — з горизонтом, порахованим із реальної глибини історії.

Головне правило модуля: якщо даних мало, він не малює лінію. Намалювати
правдоподібну пряму по трьох точках технічно нескладно, і саме тому тут
стоїть заборона, а не застереження дрібним шрифтом.

Горизонт не задається бажанням, а рахується: не довше за третину періоду,
який ми справді спостерігали. З 4 днів історії не виходить прогноз на 36
місяців — виходить повідомлення про те, скільки ще треба чекати.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Listing, PriceEvent
from .settings import Settings, load

WEEK = 7


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Чи взагалі можна прогнозувати -------------------------------------------

@dataclass
class Readiness:
    """Відповідь на питання «чи можна показувати прогноз і коли можна буде»."""

    span_days: float
    points: int                 # скільки тижневих точок вже є
    points_needed: int          # скільки треба на підгонку + бектест
    ready: bool
    days_to_wait: int
    available_from: date | None
    horizon_days: int           # чесна стеля горизонту на СЬОГОДНІ
    horizon_when_ready: int     # яким буде горизонт у момент готовності

    @property
    def horizon_months(self) -> float:
        return round(self.horizon_days / 30.4, 1)

    def message(self) -> str:
        """Текст для порожнього блоку — з конкретним числом і датою.

        Людською мовою, без назв методів: читач має зрозуміти, чому цифри
        поки немає і коли вона буде, а не як саме вона рахується.
        """
        if self.ready:
            return (f"Можемо заглядати приблизно на {self.horizon_months:.0f} "
                    f"міс. вперед — це третина того часу, що ми спостерігаємо.")
        date = (self.available_from.strftime("%d.%m.%Y")
                if self.available_from else "—")
        return (f"Ми стежимо за цінами {self.span_days:.0f} днів. Щоб показати, "
                f"куди вони рухаються, і не збрехати, треба спостерігати до "
                f"{date}.")


def observed_span(session) -> float:
    """Скільки днів ми справді спостерігаємо ціни.

    Рахуємо по власних записах історії, а не по даті публікації оголошень:
    у базі є оголошення 2022 року, але це не означає, що ми чотири роки
    міряли ринок. Ми бачимо лише ті старі оголошення, які досі висять, тобто
    саме непродані, — будувати по них динаміку означало б малювати тренд із
    відбору, а не з ринку.
    """
    first, last = session.execute(
        select(func.min(PriceEvent.observed_at), func.max(PriceEvent.observed_at))).one()
    if not first or not last:
        return 0.0
    return (last - first).total_seconds() / 86400


def readiness(session, cfg: Settings | None = None) -> Readiness:
    cfg = cfg or load()
    span = observed_span(session)
    points = int(span // WEEK)
    needed = cfg.forecast_fit_points + cfg.forecast_backtest_points
    ready = points >= needed
    days_to_wait = max(0, needed * WEEK - int(span))
    ratio = cfg.forecast_horizon_ratio
    return Readiness(
        span_days=round(span, 1), points=points, points_needed=needed, ready=ready,
        days_to_wait=days_to_wait,
        available_from=(_now() + timedelta(days=days_to_wait)).date() if not ready else None,
        horizon_days=int(span * ratio),
        horizon_when_ready=int(needed * WEEK * ratio),
    )


def horizon_schedule(session, cfg: Settings | None = None) -> list[dict]:
    """Коли який горизонт стане доступним — таблиця замість обіцянок.

    Потрібна, щоб на місце «прогнозу на 36 місяців» стало видно реальний
    графік зростання можливостей: скільки чекати і що саме отримаєш.
    """
    cfg = cfg or load()
    span = observed_span(session)
    start = _now() - timedelta(days=span)
    rows = []
    for months in (3, 6, 12, 24):
        span_at = months * 30.4
        horizon = span_at * cfg.forecast_horizon_ratio
        when = (start + timedelta(days=span_at)).date()
        rows.append({
            "history_months": months,
            "date": when,
            "reached": span >= span_at,
            "horizon_months": round(horizon / 30.4, 1),
            "note": ("сезонність можна заявляти" if months >= cfg.seasonality_years * 12
                     else "тренд без сезонної складової"),
        })
    return rows


# --- Ряд спостережень ---------------------------------------------------------

def weekly_series(session, *, rooms: int | None = None, condition: str = "",
                  market: str = "") -> list[dict]:
    """Тижневі медіани ціни за м² по власних спостереженнях.

    Точка ряду — медіана всіх цін, зафіксованих за тиждень. Тиждень, а не
    місяць: сегменти мають сотні об'єктів, тижнева медіана стабільна, а
    місячна дає замало точок за прийнятний час.
    """
    stmt = (select(PriceEvent.observed_at, PriceEvent.price_usd, Listing.area_total)
            .join(Listing, Listing.id == PriceEvent.listing_id)
            .where(PriceEvent.price_usd.isnot(None), Listing.area_total > 0,
                   Listing.quality_status == "ok"))
    if rooms is not None:
        stmt = stmt.where(Listing.rooms >= 4) if rooms >= 4 else stmt.where(Listing.rooms == rooms)
    if condition:
        stmt = stmt.where(Listing.condition == condition)
    if market:
        stmt = stmt.where(Listing.market_type == market)

    buckets: dict[date, list[float]] = {}
    for observed, price, area in session.execute(stmt):
        week = (observed.date() - timedelta(days=observed.weekday()))
        buckets.setdefault(week, []).append(price / area)
    return [{"week": w, "median": st.median(v), "n": len(v)}
            for w, v in sorted(buckets.items())]


# --- Моделі -------------------------------------------------------------------

@dataclass
class Model:
    """Підігнана модель. `predict` повертає (значення, півширина інтервалу)."""

    name: str
    params: dict = field(default_factory=dict)

    def predict(self, x: float) -> tuple[float, float]:
        raise NotImplementedError


@dataclass
class LastValue(Model):
    """Базова модель: завтра буде як сьогодні.

    Її сенс не в точності, а в тому, щоб складнішій моделі було з чим
    змагатися. Якщо лінія тренду не б'є цю модель на бектесті — тренду немає,
    є шум, і показувати його як прогноз не можна.
    """

    def predict(self, x: float) -> tuple[float, float]:
        value = self.params["value"]
        sigma = self.params["sigma"]
        # Невизначеність зростає як корінь із горизонту — так поводиться
        # випадкове блукання, а саме ним ціна й виглядає без тренду.
        steps = max(1.0, x - self.params["last_x"])
        return value, 1.96 * sigma * math.sqrt(steps)


@dataclass
class LogLinear(Model):
    """Постійний темп зростання: пряма в логарифмі ціни.

    Інтервал — передбачувальний інтервал регресії. Він розширюється в міру
    віддалення від центру вибірки сам по собі, без штучних коефіцієнтів: чим
    далі екстраполяція, тим ширший коридор.
    """

    def predict(self, x: float) -> tuple[float, float]:
        p = self.params
        mu = p["a"] + p["b"] * x
        if p["n"] > 2 and p["sxx"] > 0:
            se = p["s"] * math.sqrt(1 + 1 / p["n"] + (x - p["xbar"]) ** 2 / p["sxx"])
        else:
            se = p["s"]
        value = math.exp(mu)
        # Переносимо інтервал із логарифма назад у гривні/долари.
        return value, (math.exp(mu + 1.96 * se) - math.exp(mu - 1.96 * se)) / 2


def fit_last_value(series: list[dict]) -> LastValue | None:
    if len(series) < 2:
        return None
    values = [p["median"] for p in series]
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
    return LastValue("остання ціна", {
        "value": values[-1], "last_x": float(len(series) - 1),
        "sigma": st.pstdev(diffs) if len(diffs) > 1 else abs(diffs[0]),
    })


def fit_log_linear(series: list[dict]) -> LogLinear | None:
    if len(series) < 3:
        return None
    xs = [float(i) for i in range(len(series))]
    ys = [math.log(p["median"]) for p in series if p["median"] > 0]
    if len(ys) != len(xs):
        return None
    n = len(xs)
    xbar, ybar = st.fmean(xs), st.fmean(ys)
    sxx = sum((x - xbar) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / sxx
    a = ybar - b * xbar
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    dof = max(1, n - 2)
    s = math.sqrt(sum(r * r for r in resid) / dof)
    return LogLinear("лінійний тренд", {"a": a, "b": b, "n": n, "xbar": xbar,
                                        "sxx": sxx, "s": s})


FITTERS = {"остання ціна": fit_last_value, "лінійний тренд": fit_log_linear}


# --- Бектест ------------------------------------------------------------------

def mape(actual: list[float], predicted: list[float]) -> float | None:
    """Середня абсолютна відсоткова помилка."""
    pairs = [(a, p) for a, p in zip(actual, predicted) if a]
    if not pairs:
        return None
    return round(100 * st.fmean(abs(a - p) / abs(a) for a, p in pairs), 2)


@dataclass
class Backtest:
    """Результат перевірки моделі на схованому хвості ряду."""

    model: str
    mape: float | None
    holdout: int
    fitted_on: int


def backtest(series: list[dict], holdout: int, fitter) -> Backtest | None:
    """Ховає останні `holdout` точок, будує прогноз по решті, порівнює з фактом."""
    if len(series) <= holdout + 2:
        return None
    train, test = series[:-holdout], series[-holdout:]
    model = fitter(train)
    if model is None:
        return None
    predicted = [model.predict(float(len(train) + i))[0] for i in range(len(test))]
    return Backtest(model=model.name, mape=mape([p["median"] for p in test], predicted),
                    holdout=holdout, fitted_on=len(train))


def select_model(series: list[dict], cfg: Settings | None = None) -> tuple[Model | None, list[Backtest]]:
    """Обирає модель за результатом бектесту, а не за складністю.

    Якщо проста модель дає меншу помилку — перемагає проста. Якщо жодна не
    вкладається в дозволену помилку, не повертається жодна: краще порожній
    блок, ніж лінія, про яку ми знаємо, що вона хибна.
    """
    cfg = cfg or load()
    results = []
    for fitter in FITTERS.values():
        result = backtest(series, cfg.forecast_backtest_points, fitter)
        if result is not None:
            results.append(result)
    scored = [r for r in results if r.mape is not None]
    if not scored:
        return None, results
    best = min(scored, key=lambda r: r.mape)
    if best.mape > cfg.forecast_max_mape:
        return None, results
    return FITTERS[best.model](series), results


def project(series: list[dict], weeks: int, cfg: Settings | None = None) -> dict:
    """Прогноз на `weeks` тижнів уперед — або чесна відмова.

    Повертає структуру з полем `available`: інтерфейс показує або лінію з
    коридором, або повідомлення. Третього варіанта — лінії без коридору —
    тут не існує.
    """
    cfg = cfg or load()
    model, tests = select_model(series, cfg)
    if model is None:
        return {"available": False, "backtests": tests, "points": []}
    start = len(series)
    last_week = series[-1]["week"]
    points = []
    for i in range(1, weeks + 1):
        value, half = model.predict(float(start + i - 1))
        points.append({"week": last_week + timedelta(days=WEEK * i),
                       "value": round(value, 2),
                       "low": round(max(0.0, value - half), 2),
                       "high": round(value + half, 2)})
    best = min((t for t in tests if t.mape is not None), key=lambda t: t.mape)
    return {"available": True, "model": model.name, "mape": best.mape,
            "backtests": tests, "points": points,
            "extrapolation_note":
                "Це екстраполяція поточного тренду, а не передбачення. "
                "Зовнішні шоки — курс, війна, зміна ставок — у ній не враховані."}


def state(session=None, cfg: Settings | None = None) -> dict:
    """Повний стан прогнозування для інтерфейсу."""
    cfg = cfg or load()
    own = session is None
    session = session or SessionLocal()
    try:
        r = readiness(session, cfg)
        return {"readiness": r, "message": r.message(),
                "schedule": horizon_schedule(session, cfg),
                "seasonality_years": cfg.seasonality_years}
    finally:
        if own:
            session.close()
