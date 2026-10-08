"""Запобіжник лише на випадкових і контрольних перевірках (рішення власника 08.10, D55; D56).

Що виправлено після першого циклу з tiered (08.10, 09:05, DOM.RIA тримався за «знято»
23 з 76):
  * сліпий обхід sweep навмисно йде від найімовірніше знятих (`queue._order`): 17
    «знято» з 45 = 38% проти 13% у рівномірній вибірці Етапу 0 — він тепер у пулі
    підказаних, а 20% стоїть на новому ярусі random (рівномірно випадкові) і контрольних;
  * поки «актуальні» ще містять ~13–16% насправді знятих, 5 з 20 випадкових — звичайна
    випадковість: тримаємо, коли НИЖНЯ межа 95% інтервалу Вілсона понад 20%;
  * контрольний — лише за свіжою появою у ВЛАСНІЙ стрічці сайту: рядок LUN, що вказував
    на оголошення OLX, зробив контрольним уже зняте оголошення;
  * DOM.RIA назвала дату зняття, пізнішу за нашу останню появу ключа в стрічці, —
    справжнє зняття контрольного, не зламаний підпис.
На коді до D56 кожен тест тут падає.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, add, clean_fuse, db, get, olx_url, ria_page, ria_url,
)

from realty import configfiles, verify  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")

NOW = datetime(2026, 10, 8, 3, 0)


@pytest.fixture
def frozen(monkeypatch):
    from realty.liveness import queue

    monkeypatch.setattr(queue, "_now", lambda: NOW)
    return NOW


def _oc(host, source, tier, i, removed, *, removed_at=None, seen=None):
    from realty.liveness.signatures import ALIVE, REMOVED

    row = SimpleNamespace(source=source, is_active=True, last_seen=seen or NOW)
    item = SimpleNamespace(host=host, tier=tier, key=f"{source}:{i}", rows=[row],
                           url=ria_url(i) if source == "domria" else olx_url(f"10Ev{i:03d}"))
    verdict = SimpleNamespace(kind=REMOVED if removed else ALIVE,
                              source_removed_at=removed_at)
    return SimpleNamespace(item=item, verdict=verdict)


def _domria_run(random_removed: int, sweep_removed: int = 0):
    out = [_oc("dom.ria.com", "domria", "canary", 100 + i, False) for i in range(4)]
    out += [_oc("dom.ria.com", "domria", "random", 200 + i, i < random_removed)
            for i in range(20)]
    out += [_oc("dom.ria.com", "domria", "sweep", 300 + i, i < sweep_removed)
            for i in range(45)]
    return out


def test_wilson_lower_bound_matches_the_reference_formula():
    from realty.liveness.fuse import wilson_lower

    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(0, 20) == 0.0
    assert wilson_lower(20, 20) == pytest.approx(0.8389, abs=1e-4)
    assert wilson_lower(5, 20) == pytest.approx(0.1119, abs=1e-4)
    # 20 випадкових + 4 контрольні: тримаємо з 9 «знято», 8 — ще випадковість.
    assert wilson_lower(9, 24) > 0.20 > wilson_lower(8, 24)


def test_biased_sweep_of_08_10_does_not_trip_but_a_broken_signature_does():
    """Форма прогону 08.10: обхід 17/45, випадкові ~15% (3/20), контрольні живі — не
    тримаємо; зламаний підпис (16 з 20 випадкових «знято») — тримаємо з першого прогону."""
    from realty.liveness import fuse

    cfg = configfiles.load("liveness")
    assert cfg.fuse.mode == "tiered" and cfg.fuse.share_test == "wilson95"
    assert fuse.evaluate(_domria_run(3, sweep_removed=17), cfg) == []
    trips = fuse.evaluate(_domria_run(16), cfg)
    assert [t.source for t in trips] == ["domria"]
    assert trips[0].lower is not None and trips[0].lower > cfg.fuse.share


def test_small_hosts_are_judged_with_the_random_window():
    """lun.ua: 2 випадкових + 2 контрольні за прогін — частка лише разом із вікном
    fuse.random_window_hours. Звичайне «знято» не тримає, зламаний підпис — тримає."""
    from realty.liveness import fuse

    cfg = configfiles.load("liveness")
    run = [_oc("lun.ua", "lun", "canary", i, False) for i in range(2)]
    run += [_oc("lun.ua", "lun", "random", 10 + i, True) for i in range(2)]
    calm = {("source", "lun", "random"): (20, 1), ("host", "lun", "random"): (20, 1)}
    assert fuse.evaluate(run, cfg, prior=calm) == []
    broken = {("source", "lun", "random"): (20, 10), ("host", "lun", "random"): (20, 10)}
    trips = fuse.evaluate(run, cfg, prior=broken)
    assert [t.source for t in trips] == ["lun"] and trips[0].reason == "share_window"


def test_random_tier_takes_its_budget_from_the_sweep(db, frozen):
    """DOM.RIA: 24 випадкових + 21 обходу = ті самі 45 запитів на прогін."""
    for i in range(100):
        add(db, ria_url(36000000 + i), source="domria", external_id=str(36000000 + i))
    net = FakeNet({f"domria:{36000000 + i}": (200, ria_page(36000000 + i)) for i in range(100)})
    stats = verify.verify_batch(http=net)
    tiers = stats["tiers"]["dom.ria.com"]
    assert tiers.get("random") == 24 and tiers.get("sweep") == 21, tiers


def test_a_lun_copy_of_an_olx_ad_is_not_an_olx_canary(db, frozen):
    """Свіжий рядок LUN на оголошення OLX — не доказ «живе» для OLX; свіжий рядок самого
    OLX — так. На коді до D56 обидва ключі ставали контрольними."""
    add(db, olx_url("10Cn001"), source="lun", external_id="lun-copy",
        last_seen=datetime(2026, 10, 8, 2, 30))
    add(db, olx_url("10Cn002"), source="olx", external_id="own",
        last_seen=datetime(2026, 10, 8, 2, 30))
    net = FakeNet({"olx:10Cn001": 410, "olx:10Cn002": 200})
    stats = verify.verify_batch(http=net)
    assert stats["tiers"]["olx.ua"].get("canary") == 1, stats["tiers"]
    from realty.liveness import fuse
    assert fuse.held_sources() == set()


def _ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _canaries(db, start, deleted_at):
    fresh = [add(db, ria_url(start + i), source="domria", external_id=str(start + i),
                 last_seen=datetime(2026, 10, 8, 1, 0)) for i in range(4)]
    for i in range(30):
        add(db, ria_url(start + 100 + i), source="domria", external_id=str(start + 100 + i),
            last_seen=datetime(2026, 9, 1))
    pages = {f"domria:{start + i}": (200, ria_page(start + i, archived=i == 0,
                                                   deleted_ts=_ts(deleted_at)))
             for i in range(4)}
    pages.update({f"domria:{start + 100 + i}": (200, ria_page(start + 100 + i))
                  for i in range(30)})
    return fresh, FakeNet(pages)


def test_dom_ria_canary_removed_after_we_last_saw_it_is_a_genuine_removal(db, frozen):
    """DOM.RIA: дата зняття 02:00 пізніша за останню появу в стрічці 01:00 — справжнє
    зняття: записуємо й знімаємо, джерело не тримаємо."""
    from realty.liveness import fuse

    fresh, net = _canaries(db, 36100000, datetime(2026, 10, 8, 2, 0))
    stats = verify.verify_batch(http=net)
    assert stats["tiers"]["dom.ria.com"].get("canary") == 4
    assert fuse.held_sources() == set()
    assert get(db, fresh[0]).is_active is False
    assert [g["key"] for g in stats["canary_genuine"]] == ["domria:36100000"]


def test_dom_ria_canary_removed_before_we_last_saw_it_holds_the_source(db, frozen):
    """Дата зняття 00:30 — ДО нашої останньої появи 01:00: сайт показував оголошення
    у стрічці вже знятим — так поводиться зламаний підпис; тримаємо."""
    from realty.liveness import fuse

    fresh, net = _canaries(db, 36200000, datetime(2026, 10, 8, 0, 30))
    verify.verify_batch(http=net)
    assert fuse.held_sources() == {"domria"}
    assert all(get(db, i).is_active for i in fresh)


def test_night_m3_blind_order_is_a_new_uniform_permutation_each_night(db):
    """M3 сліпі — перестановка тих самих ключів, однакова для обох вікон ночі й нова
    щоночі; від `_order` і порядку рядків у базі не залежить."""
    from realty.liveness import queue
    from realty.night import plan

    for i in range(40):
        add(db, ria_url(36300000 + i), source="domria", external_id=str(36300000 + i))
    cfg = configfiles.load("liveness")
    with db() as s:
        u = queue.universe(s, cfg, now=NOW, history_since=plan.EPOCH)
    first = plan._m3_keys(u, cfg, queue.TIER_M3_BLIND, 7, "2026-10-09")
    again = plan._m3_keys(u, cfg, queue.TIER_M3_BLIND, 7, "2026-10-09")
    next_night = plan._m3_keys(u, cfg, queue.TIER_M3_BLIND, 7, "2026-10-10")
    assert len(first) == 40 and sorted(first) == sorted(next_night)
    assert first == again and first != next_night


# --- Рецензія D56 (три незалежні рецензенти, 08.10) ---------------------------------------------


def _olx_run(random_removed: int, sweep_removed: int):
    out = [_oc("olx.ua", "olx", "canary", i, False) for i in range(4)]
    out += [_oc("olx.ua", "olx", "random", 100 + i, i < random_removed) for i in range(30)]
    out += [_oc("olx.ua", "olx", "sweep", 1000 + i, i < sweep_removed) for i in range(420)]
    return out


def test_a_broken_signature_in_the_sweep_trips_even_if_random_checks_look_calm():
    """Приклад рецензента: OLX — 11 з 30 випадкових (Вілсон ще не певен), 380 з 420 обходу
    «знято». У пулі підказаних (97%) обхід знімав би сотні живих; у пулі обходу з межею
    OLX 15% — тримаємо з першого прогону. Звичайна частка обходу OLX (2%) — ні."""
    from realty.liveness import fuse

    cfg = configfiles.load("liveness")
    trips = fuse.evaluate(_olx_run(11, 380), cfg)
    assert [(t.source, t.reason) for t in trips] == [("olx", "sweep_share")]
    assert fuse.evaluate(_olx_run(1, 9), cfg) == []


def test_the_rows_of_one_key_are_one_check_not_several():
    """Ключ із рядком DOM.RIA і копією LUN — одна відповідь сайту. Рахуємо ключ, а не рядки:
    інакше 8 «знято» з 24 ключів (33%) стали б 16 з 48 і Вілсон хибно «певнішав»."""
    from realty.liveness import fuse

    cfg = configfiles.load("liveness")
    run = [_oc("dom.ria.com", "domria", "canary", i, False) for i in range(4)]
    for i in range(24):
        oc = _oc("dom.ria.com", "domria", "random", 500 + i, i < 8)
        oc.item.rows.append(SimpleNamespace(source="lun", is_active=True, last_seen=NOW))
        run.append(oc)
    report: dict = {}
    assert fuse.evaluate(run, cfg, report=report) == []
    pool = next(p for p in report["pools"] if p["scope"] == "host" and p["pool"] == "random")
    assert (pool["checked"], pool["removed"]) == (28, 8)


def test_the_window_adds_only_the_newest_checks_that_are_missing():
    """lun: 2 з 2 випадкових «знято» + 2 живі контрольні; у вікні — 6 свіжих «знято» (поломка
    почалась) і 100 давніх «живе». Добираємо лише 16 найсвіжіших → 8 з 20 — тримаємо; усе
    вікно (8 з 110) розмило б поломку."""
    from datetime import timedelta

    from realty.liveness import fuse

    cfg = configfiles.load("liveness")
    run = [_oc("lun.ua", "lun", "canary", i, False) for i in range(2)]
    run += [_oc("lun.ua", "lun", "random", 10 + i, True) for i in range(2)]
    window = [(NOW - timedelta(hours=1 + i / 10), 1) for i in range(6)]
    window += [(NOW - timedelta(hours=5 + i / 10), 0) for i in range(100)]
    prior = {("source", "lun", "random"): window, ("host", "lun", "random"): window}
    trips = fuse.evaluate(run, cfg, prior=prior)
    assert [t.source for t in trips] == ["lun"] and trips[0].checked == 20


def _genuine_canaries(db, start, archived: int, deleted_at):
    for i in range(4):
        add(db, ria_url(start + i), source="domria", external_id=str(start + i),
            last_seen=datetime(2026, 10, 8, 1, 0))
    for i in range(30):
        add(db, ria_url(start + 100 + i), source="domria", external_id=str(start + 100 + i),
            last_seen=datetime(2026, 9, 1))
    pages = {f"domria:{start + i}": (200, ria_page(start + i, archived=i < archived,
                                                   deleted_ts=_ts(deleted_at)))
             for i in range(4)}
    pages.update({f"domria:{start + 100 + i}": (200, ria_page(start + 100 + i))
                  for i in range(30)})
    return FakeNet(pages)


def test_two_genuine_canary_removals_in_one_run_hold_the_source(db, frozen):
    """Справжнє зняття контрольного — не більше fuse.canary_genuine_max (1) на хост за
    прогін: два одразу схожі на зламаний стан сторінки, а не на збіг — тримаємо."""
    from realty.liveness import fuse

    net = _genuine_canaries(db, 36400000, 2, datetime(2026, 10, 8, 2, 0))
    verify.verify_batch(http=net)
    assert fuse.held_sources() == {"domria"}


def test_a_removal_date_in_the_future_is_not_a_genuine_removal(db, frozen):
    from realty.liveness import fuse

    net = _genuine_canaries(db, 36500000, 1, datetime(2027, 1, 1, 0, 0))
    verify.verify_batch(http=net)
    assert fuse.held_sources() == {"domria"}


def test_a_key_with_an_already_removed_row_is_not_drawn_as_random(db):
    """«Знято» на змішаному ключі (рядок уже знято, копія ще актуальна) — здебільшого
    справжнє: такий ключ у випадкові не береться, щоб не здувати частку."""
    import random

    from realty.liveness import queue
    from realty.night import plan

    for i in range(40):
        add(db, ria_url(36600000 + i), source="domria", external_id=str(36600000 + i))
    add(db, ria_url(36609999), source="domria", external_id="old", is_active=False)
    add(db, ria_url(36609999), source="lun", external_id="lun-copy")
    cfg = configfiles.load("liveness")
    with db() as s:
        u = queue.universe(s, cfg, now=NOW, history_since=plan.EPOCH)
    for seed in range(20):
        drawn = queue.random_keys(u, cfg, random.Random(seed))["dom.ria.com"]
        assert len(drawn) == 24 and "domria:36609999" not in drawn


def test_night_canaries_go_into_every_batch():
    """Вночі прогін — 15-хвилинний пакет: після повної порції на початку — по одному
    контрольному через кожні ~batch × 60 / pace запитів (рішення власника 08.10)."""
    from realty.night import plan

    ncfg = configfiles.load("night")
    assert plan.night_batches(ncfg) == 7                     # 01:10–02:47 = 97 хв / 15
    items = [SimpleNamespace(tier="canary") for _ in range(4 + 6)]
    items += [SimpleNamespace(tier="onetime_blind") for _ in range(2000)]
    hp = plan.HostPlan(host="olx.ua", pace=3.0, items=list(items))
    plan.spread_canaries(hp, 4, 1, 15)
    tiers = [i.tier for i in hp.items]
    assert tiers[:4] == ["canary"] * 4 and len(tiers) == len(items)
    step = 15 * 60 // 3                                      # 300 запитів на пакет
    body = tiers[4:]
    for b in range(6):
        assert "canary" in body[b * (step + 1):(b + 1) * (step + 1)], b
    assert hp.tiers["canary"] == 10
