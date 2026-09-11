"""Аналіз виживання: чи справді метод враховує ще не продані об'єкти.

Перевірка йде на синтетичних даних з ВІДОМОЮ відповіддю: генеруємо вибірку з
експоненційного розподілу із заданою медіаною, ховаємо частину спостережень
цензуруванням і дивимось, чи повертає оцінка правильне число.
"""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.analytics.settings import Settings
from realty.analytics.survival import (
    Observation, estimate, kaplan_meier, naive_median,
)

TRUE_MEDIAN = 90.0          # відома відповідь: медіанний строк — 90 днів
RATE = math.log(2) / TRUE_MEDIAN


def sample(n: int, *, censor_after: float | None, staggered: bool = True,
           seed: int = 20260910):
    """Експоненційні строки життя з правостороннім цензуруванням.

    `censor_after` — верхня межа спостереження. За замовчуванням момент
    цензурування у кожного об'єкта свій (рівномірно до цієї межі): так і буває
    насправді, бо оголошення з'являються в різний час, а дивимось ми на них
    одного дня. `staggered=False` дає спільну межу для всіх — це моделює
    ситуацію, коли спостереження тільки почалось.
    """
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        true_days = rng.expovariate(RATE)
        limit = (rng.uniform(0, censor_after) if staggered else censor_after) \
            if censor_after is not None else None
        if limit is not None and true_days > limit:
            out.append(Observation(days=limit, event=False))
        else:
            out.append(Observation(days=true_days, event=True))
    return out


def test_recovers_known_median_without_censoring():
    """Без цензурування оцінка має збігатися з відомою медіаною."""
    curve = kaplan_meier(sample(4000, censor_after=None))
    assert curve is not None
    assert curve.events == 4000 and curve.censored == 0
    assert abs(curve.quantile(0.5) - TRUE_MEDIAN) < 0.1 * TRUE_MEDIAN


def test_heavy_censoring_does_not_break_the_estimate():
    """Головна перевірка: більшість об'єктів ще на ринку, а відповідь та сама.

    Саме тут наївний підхід ламається — і тест фіксує, наскільки саме.
    """
    data = sample(4000, censor_after=150.0)
    censored = sum(1 for o in data if not o.event)
    assert censored / len(data) > 0.55        # більшість спостережень незавершені

    km = kaplan_meier(data).quantile(0.5)
    naive = naive_median(data)

    assert abs(km - TRUE_MEDIAN) < 0.15 * TRUE_MEDIAN
    # Наївна медіана по знятих занижує строк щонайменше в півтора раза.
    assert naive < TRUE_MEDIAN * 0.7
    assert abs(km - TRUE_MEDIAN) < abs(naive - TRUE_MEDIAN)


def test_censored_observations_contribute_to_the_risk_set():
    """Цензуровані не є подіями, але до свого моменту тримають знаменник.

    Якщо їх просто викинути, кожна подія ділитиметься на менший `at_risk`,
    і крива впаде швидше, ніж має.
    """
    data = [Observation(10, True), Observation(20, False), Observation(30, True)]
    curve = kaplan_meier(data)
    first, second = curve.points
    assert first.at_risk == 3 and first.events == 1
    assert first.survival == pytest.approx(2 / 3)
    # На 30-й день під ризиком лишився один об'єкт: цензурований на 20-му вибув.
    assert second.at_risk == 1
    assert second.survival == pytest.approx(0.0)


def test_survival_never_increases():
    """Крива виживання не може зростати — це властивість, а не деталь."""
    curve = kaplan_meier(sample(500, censor_after=45.0))
    values = [p.survival for p in curve.points]
    assert values == sorted(values, reverse=True)


def test_confidence_interval_widens_as_the_risk_set_shrinks():
    """Наприкінці кривої під ризиком лишається мало об'єктів — коридор ширшає."""
    curve = kaplan_meier(sample(600, censor_after=200.0))
    widths = [p.high - p.low for p in curve.points]
    early = sum(widths[:5]) / 5
    late = sum(widths[-5:]) / 5
    assert late > early


def test_median_is_none_when_curve_never_reaches_half():
    """Якщо половина ще на ринку — чесна відповідь «не знаємо», а не максимум."""
    data = [Observation(10, True)] + [Observation(30, False) for _ in range(20)]
    curve = kaplan_meier(data)
    assert curve.quantile(0.5) is None


def test_short_observation_window_admits_it_cannot_see_the_median():
    """Спільне вікно коротше за медіану — медіана непізнавана, і це не помилка.

    Рівно наш поточний стан: спостереження почалось днями тому. Оцінка не має
    вгадувати — вона має сказати, що не знає. Проте те, що вона встигла
    побачити, лишається правильним: частка тих, хто дожив до кінця вікна,
    збігається з теоретичною.
    """
    window = 60.0
    data = sample(4000, censor_after=window, staggered=False)
    curve = kaplan_meier(data)
    assert curve.quantile(0.5) is None
    expected = math.exp(-RATE * window)
    assert abs(curve.survival_at(window) - expected) < 0.03


def test_estimate_refuses_to_show_a_curve_on_too_few_events():
    """Дві події — це не крива. Блок має відмовитись, назвавши число."""
    cfg = Settings(survival_min_events=50)
    result = estimate([Observation(5, True), Observation(7, True)]
                      + [Observation(4, False)] * 100, cfg)
    assert result["available"] is False
    assert result["events"] == 2 and result["needed"] == 50
    assert "50" in result["message"] and "2" in result["message"]


def test_estimate_returns_curve_once_events_are_enough():
    cfg = Settings(survival_min_events=50)
    result = estimate(sample(500, censor_after=120.0), cfg)
    assert result["available"] is True
    assert result["events"] >= 50
    assert abs(result["median_days"] - TRUE_MEDIAN) < 0.2 * TRUE_MEDIAN
    assert "не обов'язково продаж" in result["disclaimer"]


# --- відкладений вхід (ліве зрізання) -----------------------------------------

def observed_from(start_day: float, n: int = 6000, seed: int = 7):
    """Вибірка так, як її бачить наша система: спостереження почалось пізно.

    Оголошення з'являються рівномірно в часі; ми вмикаємось у момент
    `start_day` і бачимо ЛИШЕ ті, що на цей момент ще живі. Ті, що встигли
    зникнути раніше, у базу не потрапляють зовсім — саме це й зміщує оцінку,
    якщо не врахувати вік входу.
    """
    rng = random.Random(seed)
    window = 400.0
    out = []
    for _ in range(n):
        born = rng.uniform(0, window)             # коли оголошення опублікували
        lifetime = rng.expovariate(RATE)
        if born + lifetime <= start_day:
            continue                              # зникло до початку спостережень
        entry = max(0.0, start_day - born)        # вік на момент нашого старту
        horizon = start_day + 5.0                 # наше вікно спостережень
        if born + lifetime <= horizon:
            out.append(Observation(days=lifetime, event=True, entry=entry))
        else:
            out.append(Observation(days=max(entry, horizon - born), event=False,
                                   entry=entry))
    return out


def test_delayed_entry_correction_recovers_the_true_median():
    """Головна перевірка кроку: поправка на відкладений вхід усуває перекіс.

    Без неї крива описує не ринок, а нашу вибірку: ми бачимо тільки тих, хто
    дожив до старту спостережень, і вони «виживають» тим краще, чим пізніше
    ми почали.
    """
    data = observed_from(start_day=300.0)
    assert sum(1 for o in data if o.event) >= 50

    corrected = kaplan_meier(data).quantile(0.5)
    naive = kaplan_meier([Observation(days=o.days, event=o.event) for o in data]).quantile(0.5)

    assert corrected is not None
    assert abs(corrected - TRUE_MEDIAN) < 0.2 * TRUE_MEDIAN
    # Наївна оцінка систематично завищує строк — або й зовсім не бачить медіани.
    assert naive is None or naive > corrected


def test_entry_shifts_only_the_risk_set_not_the_events():
    """Той, хто ще не увійшов у спостереження, не рахується під ризиком."""
    data = [
        Observation(days=10, event=True, entry=0),
        Observation(days=50, event=False, entry=40),   # увійшов уже після 10-го дня
    ]
    curve = kaplan_meier(data)
    first = curve.points[0]
    assert first.time == 10
    assert first.at_risk == 1, "пізній учасник не мав бути під ризиком на 10-й день"
    assert first.survival == pytest.approx(0.0)


def test_zero_entry_behaves_exactly_as_before():
    """Поправка не має міняти результат там, де відкладеного входу немає."""
    plain = sample(1500, censor_after=150.0)
    with_entry = [Observation(days=o.days, event=o.event, entry=0.0) for o in plain]
    assert kaplan_meier(plain).quantile(0.5) == kaplan_meier(with_entry).quantile(0.5)
