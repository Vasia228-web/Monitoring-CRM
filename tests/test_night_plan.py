"""План нічного диригента (E9, D53): роботи реєстру по хостах — M2, M3, догін, порядок.

На коді до E9 нічного плану немає зовсім (realty/night не існує): тест падає на
імпорті — а поведінку через публічний шлях (`cli.py night`) перевіряють test_night*.py.
Тут — правила відбору:
  * M2 — лише зняті СТАРИМ кодом (без події removed у мить зняття): legacy_404 (один
    404 до рішення власника 1) і onetime_reseen (last_seen > delisted_at, усі 2 982 на
    копії); відповідь новим підписом «знято»/404 — готово, «живе», а рядок досі знятий
    (запобіжник тримав) — питаємо знову;
  * M3 — актуальні ключі без ЖОДНОЇ відповіді новим підписом; зниклі з переліку
    першими; уже перевірені новим кодом — ні;
  * ключ під запобіжником уночі не питаємо; ключ, який уже пробували цієї ночі, — теж;
  * догін — лише коли прострочених ключів хоста понад alerts.coverage_overdue_share.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import add, clean_fuse, db, olx_url, ria_url, rieltor_url  # noqa: E402,F401

from realty import configfiles  # noqa: E402
from realty.liveness import policy  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")
NOW = datetime(2026, 10, 8, 23, 0)                    # UTC: 02:00 за Києвом 09.10


def _plan(db, *, held=(), attempted_since=None, ncfg=None, lcfg=None):
    from realty.night import plan

    with db() as s:
        return plan.build(s, lcfg or policy.load(), ncfg or configfiles.load("night"),
                          now=NOW, held=set(held), attempted_since=attempted_since)


def _tiers(p) -> dict[str, str]:
    return {i.key: i.tier for hp in p.hosts.values() for i in hp.items}


def _check(db, lid, at, signature, alive, code=200, reason="sweep"):
    from realty.models import CheckEvent

    with db() as s:
        s.add(CheckEvent(listing_id=lid, checked_at=at, code=code, alive=alive,
                         reason=reason, signature=signature))
        s.commit()


def _removed_event(db, lid, at):
    from realty.models import ListingEvent

    with db() as s:
        s.add(ListingEvent(listing_id=lid, at=at, kind="removed", reason="status_410",
                           source="olx", site_key=None))
        s.commit()


GONE = datetime(2026, 9, 1)


def test_m2_takes_only_removals_by_the_old_code(db):
    """legacy_404 — знятий одним 404 старим кодом; onetime_reseen — знятий, але знову
    бачений; знятий НОВИМ кодом (подія removed у мить зняття) — не M2."""
    l404 = add(db, olx_url("10L404"), source="olx", external_id="a", is_active=False,
               delisted_at=GONE, last_seen=datetime(2026, 8, 30))
    _check(db, l404, GONE, None, False, code=404)              # старий код: 404 → знято
    reseen = add(db, olx_url("10Rsn1"), source="olx", external_id="b", is_active=False,
                 delisted_at=GONE, last_seen=datetime(2026, 9, 20))   # і з вікна D43 теж
    quiet = add(db, olx_url("10Qt01"), source="olx", external_id="c", is_active=False,
                delisted_at=GONE, last_seen=datetime(2026, 8, 30))    # не бачили після
    newcode = add(db, olx_url("10New1"), source="olx", external_id="d", is_active=False,
                  delisted_at=datetime(2026, 10, 7, 1), last_seen=datetime(2026, 10, 8))
    _removed_event(db, newcode, datetime(2026, 10, 7, 1))
    manual = add(db, olx_url("10Man1"), source="olx", external_id="e", is_active=False,
                 manual_active=False, delisted_at=GONE, last_seen=datetime(2026, 9, 20))
    t = _tiers(_plan(db))
    assert t.get("olx:10L404") == "legacy_404"
    assert t.get("olx:10Rsn1") == "onetime_reseen"
    assert "olx:10Qt01" not in t and "olx:10Man1" not in t
    assert "olx:10New1" not in t or t["olx:10New1"] != "onetime_reseen"
    assert quiet and manual


def test_m2_answered_by_the_new_code_is_done_unless_the_return_was_held(db):
    done = add(db, olx_url("10Done1"), source="olx", external_id="a", is_active=False,
               delisted_at=GONE, last_seen=datetime(2026, 9, 20))
    _check(db, done, datetime(2026, 10, 8, 1), "status_410", False, code=410)
    gone404 = add(db, olx_url("10Done2"), source="olx", external_id="b", is_active=False,
                  delisted_at=GONE, last_seen=datetime(2026, 9, 20))
    _check(db, gone404, datetime(2026, 10, 8, 1), "not_found", None, code=404)
    held_alive = add(db, olx_url("10Held1"), source="olx", external_id="c", is_active=False,
                     delisted_at=GONE, last_seen=datetime(2026, 9, 20))
    _check(db, held_alive, datetime(2026, 10, 8, 1), "alive", True)   # запобіжник тримав
    unknown = add(db, olx_url("10Unk01"), source="olx", external_id="d", is_active=False,
                  delisted_at=GONE, last_seen=datetime(2026, 9, 20))
    _check(db, unknown, datetime(2026, 10, 8, 1), "blocked", None, code=403)
    t = _tiers(_plan(db))
    assert "olx:10Done1" not in t and "olx:10Done2" not in t
    assert t.get("olx:10Held1") == "onetime_reseen"
    assert t.get("olx:10Unk01") == "onetime_reseen"            # без відповіді — ще раз


def test_m3_is_every_active_key_without_an_answer_by_the_new_code(db):
    fresh = add(db, ria_url(34100001), source="domria", external_id="34100001")
    absent = add(db, ria_url(34100002), source="domria", external_id="34100002",
                 absent_since=datetime(2026, 10, 5))
    checked = add(db, ria_url(34100003), source="domria", external_id="34100003")
    _check(db, checked, datetime(2026, 9, 20), "alive", True)          # новим кодом — колись
    legacy_only = add(db, ria_url(34100004), source="domria", external_id="34100004")
    _check(db, legacy_only, datetime(2026, 10, 1), None, True)          # лише старий HEAD
    p = _plan(db)
    t = _tiers(p)
    assert t["domria:34100002"] == "onetime_hinted"
    assert t["domria:34100001"] == "onetime_blind"
    assert t["domria:34100004"] == "onetime_blind"
    assert "domria:34100003" not in t
    order = [i.key for i in p.hosts["dom.ria.com"].items]
    assert order.index("domria:34100002") < order.index("domria:34100001")
    assert fresh and absent


def test_held_keys_and_keys_tried_tonight_are_not_requested(db):
    """Під запобіжником — результат однаково не застосувався б; уже пробували після
    старту першого вікна ночі — «повторних запитів до ключа за ніч немає»."""
    a = add(db, ria_url(34200001), source="domria", external_id="34200001")
    b = add(db, rieltor_url(4200002), source="lun", external_id="l2")
    c = add(db, olx_url("10Tried"), source="olx", external_id="t",
            last_attempt=NOW - timedelta(minutes=30))
    p = _plan(db, held={"domria"}, attempted_since=NOW - timedelta(hours=1))
    t = _tiers(p)
    assert "domria:34200001" not in t and p.hosts["dom.ria.com"].held_keys == 1
    assert "rieltor:4200002" in t
    assert "olx:10Tried" not in t and p.hosts["olx.ua"].attempted_tonight == 1
    p2 = _plan(db, held={"lun"})                      # джерело рядка під запобіжником
    assert "rieltor:4200002" not in _tiers(p2)
    assert a and b and c


def test_lane_order_follows_the_registry_and_identity_rides_its_host(db):
    ncfg = configfiles.load("night")
    canary = add(db, olx_url("10Cnr01"), source="olx", external_id="k",
                 last_seen=NOW - timedelta(hours=1))
    _check(db, canary, datetime(2026, 10, 7), "alive", True)
    m3 = add(db, olx_url("10M3abc"), source="olx", external_id="m")
    m2 = add(db, olx_url("10M2abc"), source="olx", external_id="r", is_active=False,
             delisted_at=GONE, last_seen=datetime(2026, 9, 25))
    p = _plan(db, ncfg=ncfg)
    tiers = [i.tier for i in p.hosts["olx.ua"].items]
    assert tiers == ["canary", "onetime_reseen", "onetime_blind"]
    assert p.hosts["dom.ria.com"].identity == "domria"
    assert p.hosts["lun.ua"].identity == "lun" and p.hosts["olx.ua"].identity is None
    assert ncfg.jobs.order[0] == "canary" and ncfg.jobs.order[-1] == "identity"
    assert m3 and m2


def test_overdue_catch_up_only_when_the_host_is_behind(db):
    """Догін — лише якщо прострочених ключів хоста понад 10% (alerts.coverage_overdue_share)."""
    import dataclasses

    lcfg = policy.load()
    ncfg = configfiles.load("night")
    ncfg = dataclasses.replace(ncfg, jobs=dataclasses.replace(
        ncfg.jobs, order=("canary", "overdue")))          # лише догін — без M3
    ids = [add(db, rieltor_url(4300000 + i), source="lun", external_id=f"o{i}")
           for i in range(10)]
    for lid in ids:                                       # усі перевірені вчора — вчасно
        _check(db, lid, NOW - timedelta(days=1), "alive", True)
    p = _plan(db, ncfg=ncfg, lcfg=lcfg)
    assert p.hosts["rieltor.ua"].tiers.get("overdue", 0) == 0
    assert p.hosts["rieltor.ua"].overdue_share == 0
    late = [add(db, rieltor_url(4400000 + i), source="lun", external_id=f"p{i}")
            for i in range(3)]                            # 3 з 13 ніколи — 23% > 10%
    p = _plan(db, ncfg=ncfg, lcfg=lcfg)
    assert p.hosts["rieltor.ua"].tiers.get("overdue") == 3
    assert late


def test_night_pace_is_never_faster_than_the_full_crawl():
    """Темп нічної смуги = max(liveness delay, SOURCES.full_delay); rieltor ≥ 3,0 с."""
    from realty.config import SOURCES

    cfg = policy.load()
    assert policy.pace(cfg, "rieltor.ua", "night") >= 3.0
    for host, spec in cfg.hosts.items():
        if not spec.checkable:
            continue
        pace = policy.pace(cfg, host, "night")
        assert pace >= spec.delay
        if spec.pace_source:
            src = SOURCES[spec.pace_source]
            assert pace >= (src.full_delay or src.delay)


def test_held_job_rechecks_unapplied_returns_once_released(db):
    """«Живе», не застосоване під запобіжником (змішаний ключ лишився змішаним), — уночі
    перевіряємо знову, щойно сайт відпущено; поки тримається — ні. Ярус — held_return
    (рахується в запобіжнику, рецензія E9, D53), а не held (той — лише для «знято»)."""
    gone = add(db, ria_url(34990011), source="domria", external_id="34990011", is_active=False,
               delisted_at=datetime(2026, 9, 30), last_seen=datetime(2026, 9, 29))
    copy = add(db, ria_url(34990011, "kopiya"), source="lun", external_id="lc")
    _check(db, gone, datetime(2026, 10, 8, 1), "alive", True, reason="onetime_blind")
    _check(db, copy, datetime(2026, 10, 8, 1), "alive", True, reason="onetime_blind")
    assert _tiers(_plan(db)).get("domria:34990011") == "held_return"
    assert "domria:34990011" not in _tiers(_plan(db, held={"domria"}))


def test_job_names_in_the_config_schema_are_the_queue_tiers():
    """Назви робіт config/night.toml (схема) — ті самі яруси, що пишуться в
    check_events.reason (≤16 символів) і в причину події returned."""
    from realty.liveness import queue

    tiers = {queue.TIER_CANARY, queue.TIER_HELD, queue.TIER_SAMPLE, *queue.NIGHT_TIERS}
    assert set(configfiles.NIGHT_JOBS) - {"identity"} == tiers
    # Робота «held» пише ще й ярус held_return (незастосоване «живе», рецензія E9, D53).
    assert all(len(t) <= 16 for t in tiers | {queue.TIER_HELD_RETURN})
