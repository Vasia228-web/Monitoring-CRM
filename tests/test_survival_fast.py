"""Крива виживання за O(n log n) дає ТОЙ САМИЙ результат, що й давня O(n × моменти).

Блок 2 (D48): давня реалізація рахувала групу ризику повним проходом по всіх
спостереженнях для кожного моменту події — 88% часу сторінки «Аналітика»
(1,06 з 1,2 с на M4, 11,7 с на Fedora). Нова — двійковий пошук. Вимога
власника «ті самі дані»: точки кривої, n, події, цензуровані й готовий блок
`estimate()` мають збігатися до біта, а не «приблизно». Еталон — давня
реалізація, переписана сюди дослівно.

Швидкість перевіряється не секундоміром (на Pentium N3540 і під навантаженням
пороги часу ненадійні — інтеграційна перевірка), а лічильником роботи: скільки
разів алгоритм читає поля спостережень.
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.analytics import survival  # noqa: E402
from realty.analytics.settings import Settings  # noqa: E402
from realty.analytics.survival import Curve, Observation, Point, estimate, kaplan_meier  # noqa: E402


def reference_kaplan_meier(observations):
    """Давня реалізація (до Блоку 2) — дослівно, як еталон."""
    data = [o for o in observations if o.days is not None and o.days >= 0]
    if not data:
        return None

    times = sorted({o.days for o in data if o.event})
    n_total = len(data)
    s = 1.0
    cumulative = 0.0
    points = []
    for t in times:
        at_risk = sum(1 for o in data if o.entry < t <= o.days)
        events = sum(1 for o in data if o.event and o.days == t)
        if at_risk <= 0:
            continue
        s *= 1 - events / at_risk
        if at_risk > events:
            cumulative += events / (at_risk * (at_risk - events))
        se = s * math.sqrt(cumulative) if s > 0 else 0.0
        points.append(Point(time=t, at_risk=at_risk, events=events,
                            survival=round(s, 6),
                            low=round(max(0.0, s - 1.96 * se), 6),
                            high=round(min(1.0, s + 1.96 * se), 6)))
    return Curve(points=points, n=n_total,
                 events=sum(1 for o in data if o.event),
                 censored=sum(1 for o in data if not o.event))


def _same(a: Curve | None, b: Curve | None) -> bool:
    if a is None or b is None:
        return a is b
    # Point — dataclass: == порівнює всі поля точно (float без допусків), а
    # repr ловить різницю 1 і 1.0 у моменті, яку == пропустив би.
    return (a.points == b.points and repr(a.points) == repr(b.points)
            and (a.n, a.events, a.censored) == (b.n, b.events, b.censored))


def _random_set(rng: random.Random, n: int, *, moments: int, entry_share: float,
                event_share: float, integer_days: bool) -> list[Observation]:
    out = []
    for _ in range(n):
        days = rng.randint(0, moments) if integer_days else round(rng.uniform(0, moments), 1)
        entry = 0.0
        if rng.random() < entry_share:
            # Іноді вхід ПІЗНІШЕ за вихід (такі ніколи не під ризиком) і рівно в
            # момент виходу — крайні випадки умови entry < t <= days.
            entry = rng.choice([round(rng.uniform(0, moments), 1), float(days),
                                days + 5.0])
        out.append(Observation(days=days, event=rng.random() < event_share, entry=entry))
    return out


SEEDS = range(40)


@pytest.mark.parametrize("seed", SEEDS)
def test_km_matches_reference_on_random_sets(seed):
    rng = random.Random(20261007 + seed)
    data = _random_set(rng, rng.randint(1, 600), moments=rng.choice([3, 40, 400]),
                       entry_share=rng.choice([0.0, 0.3, 0.9]),
                       event_share=rng.choice([0.0, 0.05, 0.5, 1.0]),
                       integer_days=bool(seed % 2))
    assert _same(kaplan_meier(data), reference_kaplan_meier(data))


EDGE_CASES = {
    "порожньо": [],
    "лише від'ємні й None": [Observation(days=-1, event=True), Observation(days=None, event=True)],
    "усі цензуровані": [Observation(days=d, event=False) for d in (1, 2, 3)],
    "одна подія": [Observation(days=5, event=True)],
    "подія в нуль": [Observation(days=0, event=True), Observation(days=0, event=False),
                     Observation(days=3, event=True)],
    "нічиї й змішані 1 та 1.0": [Observation(days=1, event=True), Observation(days=1.0, event=True),
                                 Observation(days=2.0, event=True), Observation(days=2, event=False)],
    "вхід пізніше за вихід": [Observation(days=3, event=True, entry=5.0),
                              Observation(days=4, event=True, entry=1.0)],
    "вхід рівно в момент події": [Observation(days=4, event=True, entry=4.0),
                                  Observation(days=4, event=True, entry=0.0),
                                  Observation(days=9, event=False, entry=4.0)],
    "ніхто не під ризиком": [Observation(days=2, event=True, entry=2.0)],
}


@pytest.mark.parametrize("name", list(EDGE_CASES))
def test_km_matches_reference_on_edge_cases(name):
    data = EDGE_CASES[name]
    assert _same(kaplan_meier(data), reference_kaplan_meier(data))


@pytest.mark.parametrize("seed", range(10))
def test_estimate_block_is_identical(seed, monkeypatch):
    """Готовий блок для сторінки (медіана, контрольні точки, крива) — той самий."""
    rng = random.Random(7 + seed)
    data = _random_set(rng, 400, moments=300, entry_share=0.4, event_share=0.4,
                       integer_days=False)
    cfg = Settings(survival_min_events=rng.choice([1, 50, 1000]))
    new = estimate(data, cfg)
    monkeypatch.setattr(survival, "kaplan_meier", reference_kaplan_meier)
    old = estimate(data, cfg)
    assert new == old
    assert repr(new) == repr(old)


@dataclass(frozen=True)
class _CountingObservation(Observation):
    """Спостереження, що рахує, скільки разів алгоритм читає його поля."""

    def __getattribute__(self, name):
        if name in ("days", "event", "entry"):
            _READS[0] += 1
        return object.__getattribute__(self, name)


_READS = [0]


def test_km_work_grows_linearithmically_not_with_moments():
    """2 000 спостережень, ~1 000 різних моментів подій.

    Давня реалізація читає поля кожного спостереження на КОЖЕН момент події:
    ≈ 2 000 × 1 000 × 4 ≈ 8 млн читань. Нова — кілька разів на спостереження.
    Межа 20 читань на спостереження — із запасом для дрібних змін коду і в
    тисячі разів нижча за давню поведінку; від швидкості машини не залежить.
    """
    rng = random.Random(42)
    data = [_CountingObservation(days=float(rng.randint(0, 1000)), event=rng.random() < 0.7,
                                 entry=float(rng.randint(0, 50)))
            for _ in range(2000)]
    _READS[0] = 0
    curve = kaplan_meier(data)
    reads = _READS[0]
    assert curve is not None and len(curve.points) > 500
    assert reads <= 20 * len(data), f"{reads} читань полів на {len(data)} спостережень"


def test_km_matches_reference_on_the_real_database_copy():
    """Те саме на справжніх даних: копія бази тестів (conftest), уся база і
    кожна смуга кімнатності. Повний набір 80 фільтрів + 39 сегментів на копії
    Етапу 0 — 0 розбіжностей (D49); тут — менший набір, щоб тест ішов секунди."""
    from realty.analytics import cache
    from realty.db import SessionLocal

    with SessionLocal() as s:
        universe = cache.get(s).universe
    groups = {"уся база": universe.items}
    for band in (1, 2, 3, 4):
        groups[f"смуга {band}"] = [o for o in universe.items if o.band == band]
    for name, items in groups.items():
        data = [Observation(days=d, event=e, entry=n)
                for o in items if (obs := o.observation) is not None for d, e, n in [obs]]
        assert _same(kaplan_meier(data), reference_kaplan_meier(data)), name
