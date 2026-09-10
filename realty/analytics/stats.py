"""Робастна статистика: медіана, розкид, відсікання викидів.

Скрізь, де йдеться про «типову ціну», основне число — медіана. Середнє
рахуємо теж, але як довідкове: кілька елітних об'єктів зміщують його так, що
воно перестає описувати ринок.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass

from .settings import Settings, load


@dataclass(frozen=True)
class Summary:
    """Опис вибірки. `n` — після відсікання, `n_raw` — до нього."""

    n: int
    n_raw: int
    median: float
    mean: float
    q1: float
    q3: float
    low: float | None      # межі відсікання; None — вибірка була замала
    high: float | None

    @property
    def iqr(self) -> float:
        return self.q3 - self.q1

    @property
    def trimmed(self) -> int:
        return self.n_raw - self.n


def quantile(ordered: list[float], p: float) -> float:
    """Квантиль лінійною інтерполяцією. Вибірка має бути відсортована."""
    if not ordered:
        raise ValueError("порожня вибірка")
    if len(ordered) == 1:
        return ordered[0]
    pos = p * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def fences(values: list[float], k: float, min_n: int,
           sided: str = "both") -> tuple[float, float] | None:
    """Межі Тьюкі, пораховані в логарифмічному просторі.

    Ціни мають скіс вправо: у лінійному просторі верхня межа Q3 + k·IQR
    відсікає нормальні дорогі квартири, а нижня йде в мінус і не відсікає
    нічого. Логарифм робить розподіл ближчим до симетричного, тож межі
    виходять осмисленими з обох боків.

    `sided="upper"` лишає нижню межу відкритою. Це потрібно для величин, у
    яких нуль — законне значення, а не одруківка: оголошення, опубліковане
    сьогодні, має вік 0 днів, і відсікати його як викид означало б викидати
    саме найсвіжіші записи.
    """
    positive = [v for v in values if v and v > 0]
    if len(positive) < min_n:
        return None
    logs = sorted(math.log10(v) for v in positive)
    q1, q3 = quantile(logs, 0.25), quantile(logs, 0.75)
    spread = q3 - q1
    if spread <= 0:
        return None
    low = 0.0 if sided == "upper" else 10 ** (q1 - k * spread)
    return low, 10 ** (q3 + k * spread)


def summarise(values: list[float], cfg: Settings | None = None,
              sided: str = "both") -> Summary | None:
    """Зводить вибірку до медіани, розкиду й меж — з відсіканням викидів.

    Викиди відсікаються ДО агрегації: інакше одна одруківка в нулях зміщує
    і межі квартилів, і саму медіану.
    """
    cfg = cfg or load()
    raw = [v for v in values if v is not None]
    if not raw:
        return None
    bounds = fences(raw, cfg.outlier_k, cfg.outlier_min_n, sided)
    kept = sorted(v for v in raw if bounds is None or bounds[0] <= v <= bounds[1])
    if not kept:
        return None
    return Summary(n=len(kept), n_raw=len(raw), median=st.median(kept),
                   mean=st.fmean(kept), q1=quantile(kept, 0.25), q3=quantile(kept, 0.75),
                   low=bounds[0] if bounds else None,
                   high=bounds[1] if bounds else None)


def percentile_of(value: float, ordered: list[float]) -> float:
    """Місце значення у вибірці, у відсотках. Вибірка має бути відсортована."""
    if not ordered:
        return 0.0
    below = sum(1 for v in ordered if v < value)
    equal = sum(1 for v in ordered if v == value)
    return round(100 * (below + equal / 2) / len(ordered), 1)


def diff_pct(value: float, reference: float) -> float | None:
    """На скільки відсотків значення відрізняється від еталона."""
    if not reference:
        return None
    return round(100 * (value / reference - 1), 1)
