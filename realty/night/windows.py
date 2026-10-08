"""Вікна ночі (config/night.toml [[windows]]) → дедлайни в секундах epoch (E9, D53).

Вікна — місцевий час машини (Fedora: Europe/Kyiv, як і OnCalendar таймерів):
наївний місцевий час → `datetime.timestamp()` (переходи літнього часу о 03:00/04:00
припадають між вікнами 01:10–02:55 і 04:10–05:55). Дедлайни — epoch, бо смуги — окремі
процеси, а `time.monotonic()` між процесами не порівнюють.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta


@dataclass(frozen=True)
class Window:
    label: str              # «01:10» — старт вікна з конфігу
    night_date: str         # місцева дата ночі (обидва вікна однієї ночі — та сама)
    start: float            # epoch — старт за конфігом
    stop_requests: float    # epoch — після цього смуги не видають нових запитів
    release_lock: float     # epoch — замок циклу звільнено до цього часу

    def describe(self) -> str:
        return (f"вікно {self.label}: запити до {_hm(self.stop_requests)}, "
                f"замок до {_hm(self.release_lock)}")


def _hm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def _epoch(day, minutes: int) -> float:
    return datetime.combine(day, dtime(minutes // 60, minutes % 60)).timestamp()


def _make(w, day, budget_min: float | None, started: float | None) -> Window:
    s, q, r = w.minutes()
    stop = _epoch(day, q)
    if budget_min is not None and started is not None:
        # --budget-min — стеля видачі запитів від фактичного старту процесу; замок —
        # однаково до release_lock вікна (інтеграція: `--budget-min 100`, нові запити
        # до 02:47).
        stop = min(stop, started + budget_min * 60)
    return Window(label=w.start, night_date=day.isoformat(), start=_epoch(day, s),
                  stop_requests=stop, release_lock=_epoch(day, r))


def current(ncfg, now: float, *, budget_min: float | None = None) -> Window | None:
    """Вікно, у якому зараз `now` (start ≤ now < release_lock), або None."""
    day = datetime.fromtimestamp(now).date()
    for w in ncfg.windows:
        win = _make(w, day, budget_min, now)
        if win.start <= now < win.release_lock:
            return win
    return None


def upcoming(ncfg, now: float) -> Window:
    """Поточне або найближче наступне вікно (для `--dry-run` удень)."""
    win = current(ncfg, now)
    if win is not None:
        return win
    day = datetime.fromtimestamp(now).date()
    for offset in (0, 1):
        d = day + timedelta(days=offset)
        for w in sorted(ncfg.windows, key=lambda w: w.minutes()):
            cand = _make(w, d, None, None)
            if cand.start > now:
                return cand
    raise RuntimeError("у config/night.toml немає вікон")        # схема це не пропускає
