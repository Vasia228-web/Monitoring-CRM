"""Строк продажу: оцінка Каплана—Меєра для цензурованих спостережень.

Задача тут не «порахувати середнє по знятих оголошеннях». Об'єкти, які досі
висять на сайті, ще не продані, і викинути їх з розрахунку означає взяти до
уваги лише швидкі продажі — тобто систематично занизити строк. Це класична
задача з цензурованими даними, і метод для неї — крива виживання.

Окреме застереження, яке має бути видно й на графіку: зникнення оголошення
не дорівнює продажу. Власник міг передумати, оголошення могло протермінуватись
або бути знятим на паузу. Ми міряємо «скільки об'єкт протримався на сайті»,
а не «скільки продавався».
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .settings import Settings, load

DISCLAIMER = ("Зникнення оголошення — не обов'язково продаж: власник міг "
              "передумати або оголошення протермінувалось.")


@dataclass(frozen=True)
class Observation:
    """Одне спостереження: скільки днів тривало і чи завершилось подією.

    `event=False` означає цензуроване спостереження — об'єкт ще на ринку,
    ми знаємо лише, що він протримався щонайменше `days`.

    `entry` — вік, у якому об'єкт потрапив під наше спостереження. Це не
    дрібниця, а виправлення систематичного перекосу: ми почали збирати дані
    недавно й бачимо лише ті оголошення, які на той момент ще висіли. Ті, що
    продались раніше, у базу не потрапили взагалі. Якщо вважати, що всі вони
    спостерігались із нульового віку, крива завищить виживання — виживуть
    саме ті, кого ми й відібрали за фактом виживання.

    Правильно: об'єкт входить у групу ризику не з нуля, а з віку `entry`.
    """

    days: float
    event: bool
    entry: float = 0.0


@dataclass
class Point:
    time: float
    at_risk: int
    events: int
    survival: float
    low: float      # 95% довірчий інтервал за формулою Грінвуда
    high: float


@dataclass
class Curve:
    points: list[Point]
    n: int
    events: int
    censored: int

    def survival_at(self, day: float) -> float:
        """Частка об'єктів, що лишились на ринку на день `day`."""
        value = 1.0
        for p in self.points:
            if p.time > day:
                break
            value = p.survival
        return value

    def quantile(self, q: float = 0.5) -> float | None:
        """Час, коли частка тих, що лишились, уперше падає до 1-q.

        Для медіани це класичний «медіанний строк». Повертає None, якщо крива
        не встигла опуститись так низько — і це чесна відповідь: за наявними
        спостереженнями половина об'єктів ще не зникла, тож медіани ми не
        знаємо, а не «вона дорівнює максимуму спостережень».
        """
        target = 1 - q
        for p in self.points:
            if p.survival <= target:
                return p.time
        return None


def kaplan_meier(observations: list[Observation]) -> Curve | None:
    """Оцінка функції виживання з урахуванням цензурованих спостережень.

    На кожен момент події: S ← S · (1 − d/n), де d — скільки зникло саме
    тоді, а n — скільки ще лишалось під ризиком. Цензуровані спостереження
    не рахуються як події, але до свого моменту вони входять у n — саме так
    їхня інформація потрапляє в оцінку.
    """
    data = [o for o in observations if o.days is not None and o.days >= 0]
    if not data:
        return None

    times = sorted({o.days for o in data if o.event})
    n_total = len(data)
    survival = 1.0
    cumulative = 0.0          # сума d / (n·(n−d)) для формули Грінвуда
    points: list[Point] = []

    for t in times:
        # Під ризиком у момент t — ті, хто вже увійшов у спостереження
        # (entry < t) і ще не вибув (days >= t). Умова на `entry` і є
        # поправкою на відкладений вхід.
        at_risk = sum(1 for o in data if o.entry < t <= o.days)
        events = sum(1 for o in data if o.event and o.days == t)
        if at_risk <= 0:
            continue
        survival *= 1 - events / at_risk
        if at_risk > events:
            cumulative += events / (at_risk * (at_risk - events))
        se = survival * math.sqrt(cumulative) if survival > 0 else 0.0
        points.append(Point(time=t, at_risk=at_risk, events=events,
                            survival=round(survival, 6),
                            low=round(max(0.0, survival - 1.96 * se), 6),
                            high=round(min(1.0, survival + 1.96 * se), 6)))

    return Curve(points=points, n=n_total,
                 events=sum(1 for o in data if o.event),
                 censored=sum(1 for o in data if not o.event))


def _risk_floor(observations: list[Observation], point_time: float) -> int:
    return sum(1 for o in observations if o.entry < point_time <= o.days)


def naive_median(observations: list[Observation]) -> float | None:
    """Медіана лише по завершених спостереженнях — навмисно хибний спосіб.

    Лишається в коді як еталон для порівняння: тест показує, наскільки саме
    вона занижує строк, коли більшість об'єктів ще на ринку.
    """
    done = sorted(o.days for o in observations if o.event)
    if not done:
        return None
    mid = len(done) // 2
    return done[mid] if len(done) % 2 else (done[mid - 1] + done[mid]) / 2


def estimate(observations: list[Observation], cfg: Settings | None = None) -> dict:
    """Готовий блок для інтерфейсу — з кривою або з поясненням, чого бракує."""
    cfg = cfg or load()
    curve = kaplan_meier(observations)
    events = curve.events if curve else 0
    if curve is None or events < cfg.survival_min_events:
        return {
            "available": False, "events": events, "needed": cfg.survival_min_events,
            "n": len(observations),
            "message": (
                f"Строк продажу поки не рахується: потрібно щонайменше "
                f"{cfg.survival_min_events} зафіксованих зникнень оголошення, "
                f"зараз їх {events}. Крива виживання по такій кількості подій "
                f"була б плоскою лінією без змісту."),
            "disclaimer": DISCLAIMER,
        }
    median = curve.quantile(0.5)
    # Поки крива не опустилась до половини, медіани немає — але сама крива вже
    # щось каже. Контрольні точки відповідають на питання, яке має сенс і
    # зараз: скільки об'єктів ще на ринку через стільки-то днів.
    horizon = max((p.time for p in curve.points), default=0)
    checkpoints = [
        {"day": day, "survival": round(100 * curve.survival_at(day), 1)}
        for day in (30, 60, 90, 180, 365) if day <= horizon
    ]
    return {
        "available": True, "n": curve.n, "events": curve.events,
        "censored": curve.censored,
        "checkpoints": checkpoints,
        "horizon_days": round(horizon),
        "median_days": round(median) if median is not None else None,
        "median_note": None if median is not None else
        (f"Медіанного строку поки не видно: зникло лише "
         f"{round(100 * curve.events / curve.n, 1)}% об'єктів, і крива ще не "
         f"опустилась до половини. Контрольні точки нижче вже осмислені."),
        "q25_days": (lambda v: round(v) if v is not None else None)(curve.quantile(0.25)),
        "curve": [{"day": p.time, "survival": p.survival,
                   "low": p.low, "high": p.high, "at_risk": p.at_risk}
                  for p in curve.points],
        "disclaimer": DISCLAIMER,
    }
