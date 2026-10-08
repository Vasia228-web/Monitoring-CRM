"""Смуга рендерів OLX уночі (E11, D60): вкладки «Приватні»/«Бізнес», одна черга сторінок
деталей для Блоків 3 і 4 — після перевірок Блоку 1, у тому самому темпі й з тими самими
блокуваннями.

Інтеграція, конфлікти 3–5: Блок 1 — першим у смузі; один рендер на ключ за ніч; темп
olx.ua старт-до-старту (2,8 с) для БУДЬ-ЯКИХ запитів смуги; 5 відмов/капч поспіль —
смуга стоїть до кінця ночі й сторож шле night-blocked. Запис — лише туди, де порожньо;
мітка вкладки — лише після звірки з чипом. На коді до E11 (night/evidence.py немає)
смуга olx.ua після HEAD нічого не робила.
Браузера немає: рендер підміняє FakeRenderer (мережа заборонена conftest).
"""
from __future__ import annotations

import dataclasses
import functools
import io
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import FakeNet, add, clean_fuse, db, get, olx_url  # noqa: E402,F401
from night_kit import (  # noqa: E402,F401
    FakeClock, TimedNet, VirtualLauncher, clean_night, local_epoch, night_env, scope_of, utc_of,
)
from seller_kit import NAME, PHONE, olx_detail_html, olx_tab_html  # noqa: E402

from realty import configfiles, ops  # noqa: E402
from realty.liveness import policy, queue  # noqa: E402
from realty.models import Listing  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse", "clean_night")
T0 = local_epoch(2026, 10, 9, 1, 10)
NOW = utc_of(T0)


class FakeRenderer:
    """Рендер за адресою: функція (адреса) → (код, html); журнал (час смуги, адреса)."""

    def __init__(self, answer, clock) -> None:
        self.answer, self.clock = answer, clock
        self.log: list[tuple[float, str]] = []
        self.closed = 0

    def render(self, url):
        from realty.night.render import RenderResult

        self.log.append((self.clock.time(), url))
        code, html = self.answer(url)
        return RenderResult(code, html, url)

    def close(self, dead=False):
        self.closed += 1


def _cfgs(**render):
    ncfg = configfiles.load("night")
    if render:
        ncfg = dataclasses.replace(ncfg, olx_render=dataclasses.replace(ncfg.olx_render,
                                                                        **render))
    return ncfg, configfiles.load("seller")


def _row(lid, url, **kw):
    base = dict(id=lid, site_key=None, source="olx", external_id=str(lid), original_url=url,
                probe_url=None, is_active=True, manual_active=None, delisted_at=None,
                last_seen=datetime(2026, 10, 8), last_attempt=None, last_checked=None,
                check_failures=0, absent_since=None, viewed_at=None, property_id=None)
    base.update(kw)
    return queue.Row(**base)


def _lane(db, items, evidence_spec, answer, *, stop_after=3600, mem=5000, **render):
    """Справжня Lane смуги olx.ua: HEAD Блоку 1 (FakeNet) → рендери (FakeRenderer)."""
    from realty.liveness import capture
    from realty.night import codec, evidence
    from realty.night.lane import Lane

    ncfg, scfg = _cfgs(**render)
    cfg = policy.load()
    spec = {"host": "olx.ua", "family": "olx", "pace": policy.pace(cfg, "olx.ua", "night"),
            "stop_at": T0 + stop_after, "held_path": None, "identity": None,
            "max_consecutive_blocks": 5, "block_share": 0.10, "block_min_requests": 20,
            "evidence": evidence_spec, "items": [codec.item_to_json(i) for i in items]}
    clock = FakeClock(T0)
    net = TimedNet(FakeNet(default=200))
    renderer = FakeRenderer(answer, clock)
    fn = functools.partial(evidence.run_olx_jobs, renderer_factory=lambda rcfg: renderer,
                           scope=scope_of(db), scfg=scfg, ncfg=ncfg,
                           now_fn=lambda: utc_of(clock.time()), mem_fn=lambda: mem)
    out = io.StringIO()
    lane = Lane(spec, out, fetcher=net.for_lane("olx.ua", clock), cfg=cfg,
                hooks=capture.default_hooks(cfg), clock=clock,
                now_fn=lambda: utc_of(clock.time()), snapshots={}, evidence_fn=fn)
    summary = lane.run()
    recs = [json.loads(x) for x in out.getvalue().splitlines()]
    ev = next((r["report"] for r in recs if r["t"] == "evidence"), None)
    return summary, ev, net, renderer


def _entry(db, tok, **kw):
    lid = add(db, olx_url(tok), source=kw.pop("source", "olx"), external_id=kw.pop("ext", tok),
              **kw)
    return lid, {"key": f"olx:{tok}", "url": olx_url(tok), "ids": [lid], "private": False}


def _detail_answer(url):
    if "obyavlenie" in url and "ID10Pr" in url:
        return 200, olx_detail_html(chip="Приватна особа", profile="/uk/list/user/abcPR1/")
    if "obyavlenie" in url and "ID10Gone" in url:
        return 410, None
    if "obyavlenie" in url:
        return 200, olx_detail_html(chip="Бізнес", deal="Переуступка", zhk="ЖК Тестовий",
                                    profile="/uk/list/user/bizBZ2/")
    if "private_business" in url and "private" in url:
        return 200, olx_tab_html("Приватні", ["10Pr001", "10Tb0x1"])
    return 200, olx_tab_html("Бізнес", ["10Bz001"])


# --- Смуга: порядок, темп, запис --------------------------------------------------------------


def test_block1_first_then_tabs_then_details_at_one_pace(db):
    a, ea = _entry(db, "10Bz001")
    b, eb = _entry(db, "10Pr001", source="lun", ext="lunpr1")      # копія LUN — теж ключ olx
    c, ec = _entry(db, "10Bz002", seller_evidence={"olx_chip": "business"},
                   place_raw={"olx_zhk": "Своє"})
    items = [queue.WorkItem(key=f"olx:10Hd{i:03d}", host="olx.ua", url=olx_url(f"10Hd{i:03d}"),
                            tier="onetime_blind", rows=(_row(900 + i, olx_url(f"10Hd{i:03d}")),))
             for i in range(3)]
    spec = {"olx_tabs": {"private": {"due": True, "max_pages": 3}}, "olx_detail": [ea, eb, ec]}
    summary, ev, net, rnd = _lane(db, items, spec, _detail_answer)
    heads = [t for _h, t, _u, _m in net.log]
    renders = [t for t, _u in rnd.log]
    assert len(heads) == 3 and max(heads) < min(renders)           # Блок 1 — першим
    times = sorted(heads + renders)
    pace = policy.pace(policy.load(), "olx.ua", "night")
    assert min(b - a_ for a_, b in zip(times, times[1:])) >= pace - 1e-6
    urls = [u for _t, u in rnd.log]
    assert "private_business" in urls[0] and len(urls) == 4
    assert "ID10Pr001" in urls[1]                    # знайдений у «Приватних» — першим у черзі
    assert ev["renders"] == 4 and ev["detail"]["written"] == 3 and rnd.closed == 1
    assert summary["evidence_requests"] == 4 and summary["stopped"] is None
    ra, rb, rc = get(db, a), get(db, b), get(db, c)
    assert ra.seller_evidence["olx_chip"] == "business" and ra.place_raw["olx_zhk"] == "ЖК Тестовий"
    assert rb.seller_evidence["olx_chip"] == "private" and rb.seller_profile.startswith("olx:u:")
    assert rc.place_raw["olx_zhk"] == "Своє"                                  # не переписано
    assert rc.seller_evidence["olx_chip"] == "business" and rc.seller_evidence["olx_detail_at"]
    for row in (ra, rb, rc):
        assert row.is_active and row.last_seen == datetime(2026, 10, 1)
        text = json.dumps([row.seller_evidence, row.place_raw, row.seller_profile],
                          ensure_ascii=False)
        assert NAME not in text and PHONE not in text and "abcPR1" not in text
    with ops.ops_session() as s:
        members = {r.site_key: r.tab for r in s.scalars(select(ops.OlxTabSeen))}
    assert members == {"olx:10Pr001": "private", "olx:10Tb0x1": "private"}
    # Мітки вкладки в доказах ще немає — лише після звірки з чипом.
    assert "olx_tab" not in rb.seller_evidence


def test_tab_parameter_not_honoured_writes_no_membership(db):
    _a, ea = _entry(db, "10Bz001")
    answer = lambda url: (200, olx_tab_html(None, ["10Bz001", "10Bz009"])) \
        if "private_business" in url else _detail_answer(url)              # noqa: E731
    spec = {"olx_tabs": {"private": {"due": True, "max_pages": 3}}, "olx_detail": [ea]}
    _s, ev, _n, rnd = _lane(db, [], spec, answer)
    assert ev["tabs"]["param_failed"] is True and len(rnd.log) == 2   # 1 вкладка + 1 деталь
    with ops.ops_session() as s:
        assert s.scalars(select(ops.OlxTabSeen)).all() == []


def test_business_partitions_that_ignore_the_filter_stop_the_sweep(db):
    from realty.night import evidence

    ncfg, scfg = _cfgs()
    parts = evidence.partitions(scfg)
    answer = lambda url: (200, olx_tab_html("Бізнес", ["10Bz001", "10Bz002"]))   # noqa: E731
    spec = {"olx_tabs": {"business": {"due": True, "partitions": parts[:5],
                                      "max_pages": 25, "new_sweep": True}}}
    _s, ev, _n, rnd = _lane(db, [], spec, answer)
    assert len(rnd.log) == 2 and ev["tabs"]["business"]["filter_ignored"]
    st = evidence.state_get(evidence.OLX_TABS_STATE)["business"]
    assert st["filter_ignored_at"] and not st.get("finished_at")


def test_five_captchas_in_a_row_stop_the_lane_and_the_watchdog_alerts(db):
    from realty import watchdog

    entries = [_entry(db, f"10Cp{i:03d}")[1] for i in range(8)]
    captcha = "<html><body><div>Please solve the captcha</div></body></html>"
    summary, ev, _n, rnd = _lane(db, [], {"olx_detail": entries}, lambda url: (200, captcha))
    assert len(rnd.log) == 5 and ev["stopped"] == "blocks" and ev["captcha"] == 5
    assert summary["stopped"] == "blocks" and summary["evidence_blocked"] == 5
    with db() as s:
        assert all(not r.seller_evidence for r in s.scalars(select(Listing)))
    ops.init_ops()
    with ops.ops_session() as s:
        s.add(ops.NightRun(status="partial", window="01:10", night_date="2026-10-09",
                           started_at=utc_of(time.time()),
                           lanes=json.dumps({"olx.ua": {k: v for k, v in summary.items()
                                                        if k != "t"}})))
    alerts = watchdog.check_night(utc_of(time.time()))
    assert any(a.key == "night-blocked:olx.ua" for a in alerts)


def test_no_render_starts_within_the_deadline_margin_or_with_low_memory(db):
    entries = [_entry(db, f"10Dl{i:03d}")[1] for i in range(4)]
    _s, ev, _n, rnd = _lane(db, [], {"olx_detail": entries}, _detail_answer, stop_after=60)
    assert rnd.log == [] and ev["stopped"] == "deadline"            # 60 с < запасу 75 с
    _s, ev, _n, rnd = _lane(db, [], {"olx_detail": entries}, _detail_answer, mem=100)
    assert rnd.log == [] and ev["stopped"] == "memory" and ev["mem_available_mb"] == 100
    _s, ev, _n, rnd = _lane(db, [], {"olx_detail": entries}, _detail_answer,
                            stop_after=75 + 2.8 + 1)
    assert len(rnd.log) == 2 and ev["stopped"] == "deadline"


def test_render_cap_per_window(db):
    entries = [_entry(db, f"10Cap{i:02d}")[1] for i in range(6)]
    _s, ev, _n, rnd = _lane(db, [], {"olx_detail": entries}, _detail_answer,
                            max_per_window=3, detail_per_window=3, private_tab_max_pages=0)
    assert len(rnd.log) == 3 and ev["stopped"] == "cap"


def test_failed_render_writes_nothing_and_is_not_retried_tonight(db):
    from realty.night import evidence

    gone, eg = _entry(db, "10Gone1")
    _s, ev, _n, _r = _lane(db, [], {"olx_detail": [eg]}, _detail_answer)
    assert ev["detail"]["failed"] == 1 and ev["detail"]["codes"] == {"410": 1}
    row = get(db, gone)
    assert not row.seller_evidence and row.is_active                # 410 рендера не знімає
    ncfg, scfg = _cfgs()
    with db() as s:
        queue_, counts = evidence.olx_detail_queue(s, policy.load(), ncfg, scfg, now=NOW,
                                                   night_start=NOW - timedelta(hours=1))
    assert queue_ == [] and counts["failed_recently"] == 1


# --- План: черга сторінок деталей ---------------------------------------------------------------


def test_detail_queue_order_and_exclusions(db):
    from realty.night import evidence

    night_start = NOW - timedelta(hours=1)
    p_lid = add(db, olx_url("10Q0001"), source="lun", external_id="q1",
                last_seen=datetime(2026, 10, 2))                             # приватний член
    own = add(db, olx_url("10Q0002"), source="olx", external_id="10Q0002",
              last_seen=datetime(2026, 10, 3))
    copy = add(db, olx_url("10Q0002"), source="lun", external_id="q2c",
               seller_evidence={"olx_detail_at": "2026-10-01"})           # копія з доказом
    lun_only = add(db, olx_url("10Q0003"), source="lun", external_id="q3",
                   last_seen=datetime(2026, 10, 7))
    add(db, olx_url("10Q0004"), source="olx", external_id="10Q0004",
        seller_evidence={"olx_detail_at": "2026-10-01"})                  # уже є
    add(db, olx_url("10Q0005"), source="olx", external_id="10Q0005")       # у Блоці 1 вікна
    add(db, olx_url("10Q0006"), source="olx", external_id="10Q0006",
        last_attempt=night_start + timedelta(minutes=5))                   # пробували цієї ночі
    add(db, olx_url("10Q0007"), source="olx", external_id="10Q0007", is_active=False)
    ops.init_ops()
    with ops.ops_session() as s:
        s.add(ops.OlxTabSeen(site_key="olx:10Q0001", tab="private", first_seen_at=NOW,
                             last_seen_at=NOW, conflicts=0))
    ncfg, scfg = _cfgs()
    with db() as s:
        q, counts = evidence.olx_detail_queue(s, policy.load(), ncfg, scfg, now=NOW,
                                              night_start=night_start, exclude={"olx:10Q0005"})
    assert [e["key"] for e in q] == ["olx:10Q0001", "olx:10Q0002", "olx:10Q0003"]
    assert q[0]["private"] and sorted(q[1]["ids"]) == sorted([own, copy])
    assert q[0]["ids"] == [p_lid] and q[2]["ids"] == [lun_only]
    assert counts["in_block1"] == 1 and counts["tried_tonight"] == 1 and counts["missing"] == 5


def test_tabs_plan_private_once_a_night_business_weekly(db):
    from realty.night import evidence

    ncfg, scfg = _cfgs()
    night_start = NOW - timedelta(hours=1)
    p = evidence.olx_tabs_plan(ncfg, scfg, now=NOW, night_start=night_start, active_keys=2000)
    assert p["private"]["due"] and p["business"]["due"] and p["business"]["new_sweep"]
    assert len(p["business"]["partitions"]) == len(scfg.olx_tabs.rooms) * len(
        scfg.olx_tabs.price_bands_usd)
    evidence.state_put(evidence.OLX_TABS_STATE, {
        "private_at": (NOW - timedelta(minutes=30)).isoformat(),
        "business": {"started_at": (NOW - timedelta(days=1)).isoformat(),
                     "done": ["one:0-40000"]}})
    p = evidence.olx_tabs_plan(ncfg, scfg, now=NOW, night_start=night_start, active_keys=2000)
    assert not p["private"]["due"]
    assert p["business"]["due"] and not p["business"]["new_sweep"]
    assert ["one", 0, 40000] not in p["business"]["partitions"]
    evidence.state_put(evidence.OLX_TABS_STATE, {"business": {
        "started_at": (NOW - timedelta(days=2)).isoformat(),
        "finished_at": (NOW - timedelta(days=1)).isoformat()}})
    p = evidence.olx_tabs_plan(ncfg, scfg, now=NOW, night_start=night_start, active_keys=2000)
    assert not p["business"]["due"]


# --- Мітки вкладок: лише після звірки з чипом --------------------------------------------------


def _members(db, n, *, agree, tab="private"):
    ops.init_ops()
    ids = []
    with ops.ops_session() as s:
        for i in range(n):
            key = f"10Lb{i:03d}"
            s.add(ops.OlxTabSeen(site_key=f"olx:{key}", tab=tab, first_seen_at=NOW,
                                 last_seen_at=NOW, conflicts=0))
    for i in range(n):
        chip = tab if i < agree else ("business" if tab == "private" else "private")
        ids.append(add(db, olx_url(f"10Lb{i:03d}"), source="olx", external_id=f"lb{i}",
                       seller_evidence={"olx_chip": chip}))
    return ids


def test_tab_labels_wait_for_agreement_with_the_chip(db):
    from realty.night import evidence

    scfg = configfiles.load("seller")
    ids = _members(db, 40, agree=40)
    rep = evidence.apply_tab_labels(scope_of(db), scfg, now=NOW)
    assert rep["status"] == "not_enough" and rep["applied"] == 0       # 40 < 50
    assert all("olx_tab" not in get(db, i).seller_evidence for i in ids)


def test_tab_labels_are_refused_when_tab_and_chip_disagree(db):
    from realty.night import evidence

    scfg = configfiles.load("seller")
    ids = _members(db, 60, agree=55)                                    # 91,7% < 98%
    rep = evidence.apply_tab_labels(scope_of(db), scfg, now=NOW)
    assert rep["status"] == "disagree" and rep["applied"] == 0
    assert all("olx_tab" not in get(db, i).seller_evidence for i in ids)


def test_tab_labels_are_written_only_where_empty_once_calibrated(db):
    from realty.night import evidence

    scfg = configfiles.load("seller")
    ids = _members(db, 60, agree=60)
    with db() as s:
        row = s.get(Listing, ids[0])
        row.seller_evidence = {**row.seller_evidence, "olx_tab": "business"}
        s.commit()
    rep = evidence.apply_tab_labels(scope_of(db), scfg, now=NOW)
    assert rep["status"] == "calibrated" and rep["applied"] == 59
    assert get(db, ids[0]).seller_evidence["olx_tab"] == "business"        # не переписано
    assert all(get(db, i).seller_evidence["olx_tab"] == "private" for i in ids[1:])
    assert get(db, ids[1]).seller_evidence["olx_tab_at"] == NOW.date().isoformat()
