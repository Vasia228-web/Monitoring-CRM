"""Черга перевірок Блоку 1: яруси, строки, графіки повторів, порції з конфігу (E8, D52).

На коді до E8: порції й паузи — константи verify.py; перевірка завжди HEAD; ключі,
які відомі лише через LUN, стояли після всіх (22,6% за 7 днів); безнадійні — у
вічному кінці черги (OLX до 17 діб); кандидат із різниці списків перевірявся один
раз; знятих, які знову в стрічці, ніхто не перевіряв; відкриту під час циклу
квартиру перевіряв лише процес після циклу.
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, add, clean_fuse, db, get, olx_url, ria_page, ria_url, rieltor_url,
)

from realty import configfiles, verify  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")
NOW = datetime(2026, 10, 8, 3, 0)


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    """«Зараз» черги — NOW (на коді до E8 модуля немає: тести падають на поведінці)."""
    try:
        from realty.liveness import queue
    except ImportError:
        return
    monkeypatch.setattr(queue, "_now", lambda: NOW)


def _plan(db, **kw):
    # Імпорт тут, а не вгорі: тести через verify_batch мають падати на коді до E8 на
    # поведінці, а не на імпорті модуля, якого тоді не було.
    from realty.liveness import policy as pol, queue

    with db() as s:
        return queue.plan_run(s, pol.load(), now=NOW, **kw)


def _tier_of(plan, key):
    for item in plan.items:
        if item.key == key:
            return item.tier
    return None


def _check(db, lid, at, signature, alive, code=200):
    from realty.models import CheckEvent

    with db() as s:
        s.add(CheckEvent(listing_id=lid, checked_at=at, code=code, alive=alive,
                         reason="sweep", signature=signature))
        s.commit()


def test_due_filter_prevents_starvation_of_lun_only_olx_keys(db):
    """Ключі OLX, перевірені вчора, не забирають порцію в тих, що відомі лише через LUN
    (до E8 рядки LUN ішли після всіх OLX незалежно від давності — 22,6% за 7 днів)."""
    for i in range(60):
        lid = add(db, olx_url(f"10Ow{i:03d}"), source="olx", external_id=f"o{i}",
                  last_attempt=NOW - timedelta(days=1), last_checked=NOW - timedelta(days=1))
        _check(db, lid, NOW - timedelta(days=1), "alive", True)
    for i in range(10):
        add(db, olx_url(f"10Lu{i:03d}"), source="lun", external_id=f"l{i}",
            last_attempt=NOW - timedelta(days=10))
    net = FakeNet(default=200)
    verify.verify_batch(limit=10, http=net)
    asked = {u.rsplit("-ID", 1)[1][:-5] for u, _m, _d in net.calls}
    assert asked == {f"10Lu{i:03d}" for i in range(10)}


def test_budgets_and_delays_come_from_config(db, tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for f in (configfiles.ROOT / "config").glob("*.toml"):
        shutil.copy(f, cfg_dir / f.name)
    path = cfg_dir / "liveness.toml"
    text = path.read_text(encoding="utf-8")
    text = text.replace("delay = 2.0\npace_source = \"olx\"", "delay = 0.5\npace_source = \"olx\"")
    text = text.replace("sweep_per_run = 450", "sweep_per_run = 7")
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(cfg_dir))
    for i in range(20):
        add(db, olx_url(f"10Bu{i:03d}"), source="olx", external_id=f"b{i}")
    net = FakeNet(default=200)
    stats = verify.verify_batch(http=net)
    assert stats["tiers"]["olx.ua"].get("sweep") == 7
    assert {d for _u, _m, d in net.calls} == {0.5}
    broken = text.replace("sweep_per_run = 7", "sweep_per_run = 7\nsweep_per_rnu = 1", 1)
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(configfiles.ConfigError, match="sweep_per_rnu"):
        configfiles.load("liveness")


def test_production_uses_get_for_domria_and_head_elsewhere(db):
    a = add(db, ria_url(35000001), source="domria", external_id="a")
    b = add(db, olx_url("10Meth"), source="olx", external_id="b")
    c = add(db, rieltor_url(13000002), source="lun", external_id="c")
    d = add(db, "https://lun.ua/uk/realty/4700000002", source="lun", external_id="d")
    e = add(db, "https://flombu.com/uk/estate_deal_sales/116001", source="flombu",
            external_id="e")
    net = FakeNet({"domria:35000001": (200, ria_page(35000001))}, default=200)
    verify.verify_batch(ids=[a, b, c, d, e], http=net)
    methods = {u: m for u, m, _d in net.calls}
    assert methods[ria_url(35000001)] == "GET"
    assert {m for u, m in methods.items() if "dom.ria.com" not in u} == {"HEAD"}
    # Адреси перевірки — без пасток Етапу 0: rieltor без www, flombu з www, lun.ua /uk/.
    assert any(u.startswith("https://rieltor.ua/") for u in methods)
    assert "https://www.flombu.com/uk/estate_deal_sales/116001" in methods
    assert "https://lun.ua/uk/realty/4700000002" in methods


def test_failed_keys_retry_on_backoff_not_forever_last(db):
    """Після незрозумілих відповідей — графік 3/12/24/72 год, а не вічний кінець черги."""
    add(db, olx_url("10Bo001"), source="olx", external_id="1", check_failures=1,
        last_attempt=NOW - timedelta(hours=4))
    add(db, olx_url("10Bo004"), source="olx", external_id="4", check_failures=4,
        last_attempt=NOW - timedelta(days=2))
    net = FakeNet(default=200)
    verify.verify_batch(http=net)
    asked = {u.rsplit("-ID", 1)[1][:-5] for u, _m, _d in net.calls}
    assert "10Bo004" not in asked, "після четвертої невдачі — 72 год, минуло 48"
    assert "10Bo001" in asked, "3 год після першої невдачі минуло"


def test_absent_listing_stays_in_the_hinted_tier_until_resolved(db):
    """Зник із переліку → одразу; після перевірки — через добу, 3 доби, тиждень."""
    from realty.liveness import policy as pol, queue

    since = NOW - timedelta(hours=1)
    lid = add(db, ria_url(35000002), source="domria", external_id="x", absent_since=since)
    assert _tier_of(_plan(db), "domria:35000002") == "absent"
    _check(db, lid, since + timedelta(minutes=30), "alive", True)
    assert _tier_of(_plan(db), "domria:35000002") is None, "наступна — через 24 год"
    with db() as s:
        plan = queue.plan_run(s, pol.load(), now=since + timedelta(hours=25))
    assert any(i.key == "domria:35000002" and i.tier == "absent" for i in plan.items)


def test_reseen_tier_ignores_the_d43_artefact(db):
    """«Знову бачили» з last_seen до 23.09 08:00 UTC — артефакт D43 (D47), не появи;
    справжня поява — перевірка й повернення живого."""
    gone = datetime(2026, 8, 10)                     # поза вікном вибірки знятих
    real = add(db, olx_url("10Rs001"), source="olx", external_id="r1", is_active=False,
               delisted_at=gone, last_seen=datetime(2026, 10, 5))
    fake = add(db, olx_url("10Rs002"), source="olx", external_id="r2", is_active=False,
               delisted_at=gone, last_seen=datetime(2026, 9, 20))
    net = FakeNet(default=200)
    verify.verify_batch(http=net)
    assert get(db, real).is_active is True
    assert get(db, fake).is_active is False
    assert not any("10Rs002" in u for u, _m, _d in net.calls)


def test_removed_row_seen_in_feed_is_rechecked_not_auto_returned(db):
    """Поява в стрічці сама стану не змінює: перевірка; 410 — лишається знятим, 200 —
    повертається з подією (причина — ярус reseen)."""
    from liveness_kit import events

    gone = datetime(2026, 9, 28)
    still = add(db, olx_url("10Rs003"), source="olx", external_id="r3", is_active=False,
                delisted_at=gone, last_seen=datetime(2026, 10, 6))
    back = add(db, olx_url("10Rs004"), source="olx", external_id="r4", is_active=False,
               delisted_at=gone, last_seen=datetime(2026, 10, 6))
    assert get(db, still).is_active is False
    verify.verify_batch(http=FakeNet({"olx:10Rs003": 410, "olx:10Rs004": 200}))
    assert get(db, still).is_active is False and events(db, still) == []
    assert get(db, back).is_active is True
    assert [(e.kind, e.reason) for e in events(db, back)] == [("returned", "reseen")]


def test_deferred_open_checks_are_done_by_the_cycle_step(db, monkeypatch):
    """Відкрита під час циклу квартира — ярус opened цього ж кроку; завдання закрито."""
    from realty.lookup import queue as jobs

    lid = add(db, olx_url("10Op001"), source="olx", external_id="op", property_id=777001,
              last_attempt=NOW - timedelta(hours=12), last_checked=NOW - timedelta(hours=12))
    job = jobs.enqueue(jobs.KIND_OPENED, jobs.opened_key(777001), 777001, state="deferred")
    try:
        from realty.liveness import service
        monkeypatch.setattr(service, "deferred_opened_jobs", lambda: {job: 777001})
    except ImportError:                    # код до E8: тест падає нижче, на поведінці
        pass
    verify.verify_batch(http=FakeNet({"olx:10Op001": 410}))
    done = jobs.get(job)
    assert done.state == "done" and jobs.result_of(done)["delisted"] == 1
    assert get(db, lid).is_active is False


def test_removed_sample_rechecks_recent_removals_and_returns_live_ones(db):
    gone = NOW - timedelta(days=3)
    lid = add(db, rieltor_url(13000003), source="lun", external_id="s1", is_active=False,
              delisted_at=gone, last_seen=gone - timedelta(days=1))
    verify.verify_batch(http=FakeNet({"rieltor:13000003": 200}))
    assert get(db, lid).is_active is True
    from liveness_kit import events
    assert [(e.kind, e.reason) for e in events(db, lid)] == [("returned", "rm_sample")]


def test_snapshot_covered_sources_match_the_snapshot_module():
    from realty import snapshot
    from realty.liveness import queue

    assert queue.SNAPSHOT_COVERED == snapshot.ENUMERABLE


def test_open_check_job_stays_deferred_unless_every_key_was_checked(db):
    """Стеля ярусу відкинула частину ключів квартири — завдання не закривається
    «done»: його після циклу виконає процес перевірки (повний результат)."""
    from types import SimpleNamespace

    from realty.liveness import apply, service
    from realty.lookup import queue as jobs

    job = jobs.enqueue(jobs.KIND_OPENED, jobs.opened_key(777002), 777002, state="deferred")
    oc = SimpleNamespace(item=SimpleNamespace(key="olx:10Pa001"), verdict=object())
    rep = apply.ApplyReport(by_property={777002: {"checked": 1}})
    closed = service._close_jobs({job: 777002}, {job: {"olx:10Pa001", "olx:10Pa002"}}, [oc], rep)
    assert closed == [] and jobs.get(job).state == "deferred"
    both = [oc, SimpleNamespace(item=SimpleNamespace(key="olx:10Pa002"), verdict=object())]
    assert service._close_jobs({job: 777002}, {job: {"olx:10Pa001", "olx:10Pa002"}},
                               both, rep) == [job]
    assert jobs.get(job).state == "done"


# --- Рецензія E8 (D52) --------------------------------------------------------------------------


def test_repeat404_streak_backs_off_after_count(db):
    """Серія набрала count (3), а ключ досі актуальний (існує чи перевірка не відповіла)
    → наступні 404-перевірки за repeat_404.after_count_hours (24, 72, 168), а не щодоби
    назавжди. На коді до виправлення ключ ішов у ярус щодоби."""
    from realty.liveness import policy as pol, queue

    lid = add(db, olx_url("10Rp001"), source="olx", external_id="rp")
    for h in (0, 24, 48, 72):                                  # 4 зараховані 404
        _check(db, lid, NOW + timedelta(hours=h), "not_found", None, code=404)

    def tier_at(hours):
        with db() as s:
            plan = queue.plan_run(s, pol.load(), now=NOW + timedelta(hours=hours))
        return _tier_of(plan, "olx:10Rp001")

    assert tier_at(96) is None, "серія 4 ≥ 3: друге значення графіка — 72 год"
    assert tier_at(143) is None
    assert tier_at(145) == "repeat404"


def test_repeat404_takes_at_most_half_of_the_hinted_cap(db):
    """Вічні 404 не витісняють зниклих із переліку: ярус repeat404 — не більше половини
    спільної стелі підказаних (DOM.RIA: 60 → 30), решта — absent. На коді до
    виправлення repeat404 забирав 40, absent — лише 20."""
    for i in range(40):
        lid = add(db, ria_url(35600000 + i), source="domria", external_id=f"r{i}")
        _check(db, lid, NOW - timedelta(hours=30), "not_found", None, code=404)
    for i in range(40):
        add(db, ria_url(35700000 + i), source="domria", external_id=f"a{i}",
            absent_since=NOW - timedelta(hours=1))
    plan = _plan(db)
    tiers = plan.tiers["dom.ria.com"]
    assert tiers.get("repeat404") == 30, tiers
    assert tiers.get("absent") == 30, tiers


def test_host_portions_must_fit_the_run_budget(tmp_path, monkeypatch):
    """Порції хоста × delay понад 80% стелі прогону, чи стеля понад ліміт кроку циклу
    мінус 5 хв — конфіг не приймається (на коді до виправлення приймався)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for f in (configfiles.ROOT / "config").glob("*.toml"):
        shutil.copy(f, cfg_dir / f.name)
    path = cfg_dir / "liveness.toml"
    text = path.read_text(encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(cfg_dir))
    path.write_text(text.replace("sweep_per_run = 450", "sweep_per_run = 600", 1),
                    encoding="utf-8")
    with pytest.raises(configfiles.ConfigError, match="80%"):
        configfiles.load("liveness")
    path.write_text(text.replace("budget_minutes = 22", "budget_minutes = 28", 1),
                    encoding="utf-8")
    with pytest.raises(configfiles.ConfigError, match="budget_minutes"):
        configfiles.load("liveness")


def test_past_the_deadline_no_existence_check_is_started(monkeypatch):
    """Стеля часу минула між запитом сторінки й перевіркою існування → «не знайдено»
    без нових запитів (картки API, ремонту), а не ще 2–3 GET після стелі."""
    from realty.liveness import engine, existence, policy as pol, queue

    cfg = pol.load()
    ticks = iter([0.0, 0.0])          # старт смуги, перевірка перед ключем; далі — 100
    monkeypatch.setattr(engine.time, "monotonic", lambda: next(ticks, 100.0))
    item = queue.WorkItem(key="domria:35800001", host="dom.ria.com", url=ria_url(35800001),
                          tier="repeat404", rows=(),
                          streak404=(NOW - timedelta(hours=48), NOW - timedelta(hours=24)))
    net = FakeNet({"domria:35800001": 404}, default=200)
    out, _st = engine.run_lane("dom.ria.com", [item], fetcher=net, cfg=cfg,
                               ctx=existence.Context(cfg=cfg, now=NOW), deadline=50.0,
                               now_fn=lambda: NOW)
    assert out[0].verdict.signature == "not_found"
    assert len(net.calls) == 1, net.calls
