"""«Аналітика» рахує раз на знімок, а не на кожен запит (Блок 2, D48).

Досі кожне відкриття «Аналітики» заново рахувало криву виживання (88% часу
сторінки) і частини, що від фільтра не залежать, а сторінка квартири шукала
«схожих» повним проходом по всіх 15 тис. об'єктів на кожному щаблі драбини.
Тут перевірено лічильниками (а не секундоміром — на Fedora пороги часу
ненадійні), що повторна робота зникла, і окремо — що вивід той самий.
"""
from __future__ import annotations

import random
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty import configfiles  # noqa: E402
from realty.analytics import cache, segments, survival  # noqa: E402
from realty.analytics.segments import Item, Universe, compare, ladder, rooms_band  # noqa: E402
from realty.analytics.settings import Settings, load  # noqa: E402
from realty.analytics.stats import summarise  # noqa: E402
from realty.db import SessionLocal  # noqa: E402
from realty.web import analytics_routes  # noqa: E402
from realty.web.app import app  # noqa: E402

CFG = Settings()


@pytest.fixture
def client(monkeypatch):
    for var in ("AUTH_USER", "AUTH_PASSWORD", "FRIEND_USER", "FRIEND_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    cache.invalidate()                      # свіжий знімок — його пам'ять порожня
    with TestClient(app) as c:
        yield c
    cache.invalidate()


def _count_calls(monkeypatch, name: str, *modules) -> list:
    """Рахує виклики функції `name`, хоч би звідки її брали (модуль чи імпорт у маршруті)."""
    calls: list = []
    for mod in modules:
        real = getattr(mod, name, None)
        if real is None:
            continue

        def wrapper(*a, _real=real, **k):
            calls.append(1)
            return _real(*a, **k)
        monkeypatch.setattr(mod, name, wrapper)
    return calls


def test_analytics_curve_is_not_recomputed_for_the_same_filter(client, monkeypatch):
    calls = _count_calls(monkeypatch, "kaplan_meier", survival)
    assert client.get("/analytics?rooms=2").status_code == 200
    assert calls, "перший запит мав порахувати криву"
    calls.clear()
    for _ in range(2):
        assert client.get("/analytics?rooms=2").status_code == 200
    assert calls == [], "крива виживання перераховується на кожен запит"


def test_filter_independent_parts_are_computed_once_per_snapshot(client, monkeypatch):
    names = ("primary_vs_secondary", "days_by_condition", "liquidity_proxy", "by_rooms",
             "by_condition", "by_market")
    counters = {n: _count_calls(monkeypatch, n, segments, analytics_routes) for n in names}
    for url in ("/analytics", "/analytics?rooms=1", "/analytics?condition=renovated",
                "/analytics?market=primary&rooms=3"):
        assert client.get(url).status_code == 200
    # Раніше — по разу (а зрізи — по два) на КОЖЕН із чотирьох запитів.
    assert {n: len(c) for n, c in counters.items()} == {n: 1 for n in names}


def test_property_page_reuses_the_segment_curve(client, monkeypatch):
    with SessionLocal() as s:
        universe = cache.get(s).universe
    groups: dict = {}
    for o in universe.items:
        groups.setdefault((o.band, o.condition, o.market), []).append(o)
    # Дві квартири одного великого сегмента: друга має взяти криву з пам'яті.
    first, second = max(groups.values(), key=len)[:2]
    calls = _count_calls(monkeypatch, "kaplan_meier", survival)
    assert client.get(f"/property/{first.property_id}?verify=0").status_code == 200
    assert calls, "перша сторінка сегмента мала порахувати криву"
    calls.clear()
    assert client.get(f"/property/{second.property_id}?verify=0").status_code == 200
    assert client.get(f"/property/{first.property_id}?verify=0").status_code == 200
    assert calls == [], "крива сегмента перераховується на кожну сторінку квартири"


def test_broken_speed_config_does_not_take_down_analytics_pages(client, tmp_path, monkeypatch):
    """«Аналітика» й сторінка квартири до Блоку 2 від speed.toml не залежали:
    зламаний ключ пам'яті кривих не дає 500 — криві рахуються без пам'яті, вивід
    той самий (D49)."""
    with SessionLocal() as s:
        universe = cache.get(s).universe
    pid = max(universe.items, key=lambda o: (o.observation is not None, o.property_id)).property_id
    urls = ["/analytics", "/analytics?rooms=2&condition=renovated",
            f"/property/{pid}?verify=0", f"/api/analytics/property/{pid}"]
    good = {u: client.get(u) for u in urls}
    assert all(r.status_code == 200 for r in good.values()), \
        {u: r.status_code for u, r in good.items()}

    broken = tmp_path / "config"
    shutil.copytree(Path(configfiles.ROOT) / "config", broken)
    toml = broken / "speed.toml"
    toml.write_text(toml.read_text().replace("km_memo_keys = 128", 'km_memo_keys = "lots"'))
    monkeypatch.setenv(configfiles.ENV_DIR, str(broken))
    with pytest.raises(configfiles.ConfigError):
        configfiles.get("speed")
    calls = _count_calls(monkeypatch, "kaplan_meier", survival)
    for url in urls:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
        assert r.text == good[url].text, url
    assert calls, "криві мали рахуватися без пам'яті, а не братися з неї"


def test_curve_memory_is_bounded_by_config(monkeypatch):
    """Фільтри — довільні рядки з адреси: пам'ять кривих не росте без меж."""
    universe = Universe(items=[_item(i) for i in range(5)])
    monkeypatch.setattr(segments, "curve_capacity", lambda: 3)
    for k in range(10):
        segments.filter_curve(universe, CFG, rooms=f"сміття-{k}")
    assert len(universe.curves) == 3


# --- Пошук «схожих» лише в кошику своєї смуги ---------------------------------------------


def _item(pid, **over) -> Item:
    base = dict(property_id=pid, rooms=2, area=60.0, price_usd=60_000.0, ppsqm=1000.0,
                condition="renovated", market="secondary", district="Центр",
                days_listed=30.0, delisted_at=None)
    base.update(over)
    return Item(**base)


def test_compare_looks_only_at_flats_of_the_same_rooms_band(monkeypatch):
    """1 000 однокімнатних і 40 двокімнатних: порівняння двокімнатної не має
    торкатися жодної однокімнатної (раніше — проходило по всіх на кожному щаблі)."""
    universe = Universe(items=[_item(i, rooms=1) for i in range(1000)]
                        + [_item(5000 + i, ppsqm=1000.0 + i) for i in range(40)])
    target = universe.items[-1]
    compare(universe, target, CFG)          # покажчики знімка будуються тут, один раз
    touched: list[int] = []
    monkeypatch.setattr(Item, "band", property(
        lambda self: touched.append(self.property_id) or rooms_band(self.rooms)))
    result = compare(universe, target, CFG)
    assert result is not None and result.n >= CFG.min_sample
    assert not [pid for pid in touched if pid < 5000], "переглянуто квартири іншої смуги"


def _reference_compare(universe, item, cfg, value=lambda o: o.ppsqm, sided="both"):
    """Давній compare — повний прохід по всіх об'єктах (еталон)."""
    for level in ladder(item, cfg):
        peers = [v for o in universe.items
                 if o.property_id != item.property_id and level.match(o)
                 and (v := value(o)) is not None]
        if len(peers) < cfg.min_sample:
            continue
        summary = summarise(peers, cfg, sided)
        if summary is None:
            continue
        kept = sorted(v for v in peers
                      if summary.low is None or summary.low <= v <= summary.high)
        return (level.key, level.label, summary, kept)
    return None


@pytest.mark.parametrize("seed", range(6))
def test_compare_gives_exactly_the_old_result(seed):
    rng = random.Random(seed)
    items = [_item(i, rooms=rng.choice([None, 1, 2, 3, 4, 5]),
                   area=rng.choice([None, round(rng.uniform(25, 120), 1)]),
                   ppsqm=rng.choice([None, round(rng.uniform(500, 2500), 2)]),
                   price_usd=round(rng.uniform(20_000, 150_000), 2),
                   days_listed=rng.choice([None, round(rng.uniform(0, 400), 3)]),
                   condition=rng.choice(["renovated", "needs_repair", "unknown"]),
                   market=rng.choice(["primary", "secondary"]),
                   district=rng.choice([None, "Центр", "Пасічна"]))
             for i in range(900)]
    universe = Universe(items=items)
    for target in rng.sample(items, 25):
        for value, sided in ((lambda o: o.ppsqm, "both"), (lambda o: o.price_usd, "both"),
                             (lambda o: o.days_listed, "upper")):
            new = compare(universe, target, CFG, value=value, sided=sided)
            old = _reference_compare(universe, target, CFG, value=value, sided=sided)
            got = None if new is None else (new.level, new.label, new.summary, new.peers)
            assert got == old


def test_indexes_follow_items_added_after_build():
    """Тести (і будь-хто) можуть доповнити items після першого пошуку — покажчики
    перебудовуються, а не відповідають за старий список."""
    universe = Universe(items=[_item(i) for i in range(3)])
    assert universe.by_id.get(99) is None
    universe.items.append(_item(99, rooms=4))
    assert universe.by_id[99].property_id == 99
    assert [o.property_id for o in universe.by_band[4]] == [99]


def test_reset_derived_forgets_indexes_curves_and_parts_after_an_in_place_change():
    """Заміна квартири в знімку на місці (так крок E5 оновлюватиме квартиру після
    «розділити/злити») — тієї самої довжини, тож покажчики самі її не бачать.
    Після reset_derived() усе похідне — як у щойно зібраного знімка."""
    items = [_item(i, rooms=1 + i % 3, ppsqm=900.0 + 7 * i, days_listed=10.0 + i,
                   condition=("renovated", "needs_repair")[i % 2],
                   market=("secondary", "primary")[i % 4 == 0])
             for i in range(90)]
    universe = Universe(items=list(items))
    old = universe.items[5]
    before = (universe.by_id[5], segments.filter_curve(universe, CFG, rooms="2"),
              segments.analytics_parts(universe, CFG))
    universe.items[5] = _item(5, rooms=2, ppsqm=5000.0, days_listed=400.0,
                              delisted_at=None, condition="renovated", market="secondary")
    # Без скидання покажчик іще показує стару квартиру — тому скидання й обов'язкове.
    assert universe.by_id[5] is old
    universe.reset_derived()
    fresh = Universe(items=list(universe.items))
    assert universe.by_id[5] is universe.items[5]
    assert universe.by_id == fresh.by_id and universe.by_band == fresh.by_band
    curve = segments.filter_curve(universe, CFG, rooms="2")
    parts = segments.analytics_parts(universe, CFG)
    assert curve == segments.filter_curve(fresh, CFG, rooms="2")
    assert parts == segments.analytics_parts(fresh, CFG)
    # Перевірка самого тесту: заміна справді змінює відповідь.
    assert curve != before[1] and parts != before[2]


def test_analyse_finds_the_flat_by_index_and_matches_a_scan():
    with SessionLocal() as s:
        universe = cache.get(s).universe
        sample = random.Random(1).sample(universe.items, min(30, len(universe.items)))
    for item in sample:
        assert universe.by_id[item.property_id] is item
        assert next(o for o in universe.items if o.property_id == item.property_id) is item


def test_filter_curve_equals_a_fresh_estimate():
    """Крива з пам'яті — та сама, що порахована заново по тому самому знімку."""
    cfg = load()
    with SessionLocal() as s:
        universe = cache.get(s).universe
    for rooms, condition, market in (("", "", ""), ("2", "", ""), ("1", "renovated", ""),
                                     ("3", "needs_repair", "secondary"), ("x", "", "")):
        peers = [o for o in universe.items
                 if (not rooms or str(o.band or "") == rooms)
                 and (not condition or o.condition == condition)
                 and (not market or o.market == market)]
        fresh = survival.estimate([survival.Observation(days=d, event=e, entry=n)
                                   for o in peers if (obs := o.observation) is not None
                                   for d, e, n in [obs]], cfg)
        got = segments.filter_curve(universe, cfg, rooms=rooms, condition=condition,
                                    market=market)
        assert got == fresh
