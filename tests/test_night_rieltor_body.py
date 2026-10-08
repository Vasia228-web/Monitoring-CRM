"""Уночі rieltor.ua — GET замість HEAD для ключів без доказів Блоків 3/4 (E11, D60).

Інтеграція, конфлікт 4: «для rieltor у M3 замість HEAD робимо GET — та сама
класифікація за кодом», тіло — гачкам доказів (роль і агенція — Блок 3, блок ЖК —
Блок 4); окремої фази D Блоку 4 немає. Запитів стільки ж (один на ключ), вердикт той
самий, що дав би HEAD; крок циклу — і далі HEAD. На коді до E11 нічна смуга rieltor
питала лише HEAD і доказів із картки не мала.
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, ProbeResult, add, clean_fuse, db, get, rieltor_url,
)
from night_kit import FakeClock, TimedNet, clean_night, local_epoch, utc_of  # noqa: E402,F401
from seller_kit import NAME, PHONE, rieltor_html  # noqa: E402

from realty import configfiles  # noqa: E402
from realty.liveness import policy, queue  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse", "clean_night")
NOW = datetime(2026, 10, 8, 23, 0)
T0 = local_epoch(2026, 10, 9, 1, 10)


def _plan(db):
    from realty.night import plan

    with db() as s:
        return plan.build(s, policy.load(), configfiles.load("night"), now=NOW)


def _items(p, host="rieltor.ua"):
    return {i.key: i for i in p.hosts[host].items}


def test_night_plan_asks_get_only_for_keys_without_evidence(db):
    lacking = add(db, rieltor_url(13200001), source="lun", external_id="a1")
    done = add(db, rieltor_url(13200002), source="lun", external_id="a2",
               seller_evidence={"rieltor_detail_at": "2026-10-01", "rieltor_role": "Рієлтор"},
               place_raw={"rieltor_checked_at": "2026-10-01"})
    half = add(db, rieltor_url(13200003), source="lun", external_id="a3",
               seller_evidence={"rieltor_detail_at": "2026-10-01"})
    assert lacking and done and half
    p = _plan(db)
    items = _items(p)
    cap = policy.load().capture.body_hosts["rieltor.ua"]
    assert items["rieltor:13200001"].body_cap == cap
    assert items["rieltor:13200003"].body_cap == cap               # бракує блоку ЖК
    assert items["rieltor:13200002"].body_cap == 0
    assert p.hosts["rieltor.ua"].body_gets == 2
    assert p.hosts["rieltor.ua"].requests == 3                     # запитів стільки ж


def test_cycle_plan_keeps_head(db):
    add(db, rieltor_url(13200011), source="lun", external_id="b1")
    cfg = policy.load()
    with db() as s:
        plan = queue.plan_run(s, cfg)
    rieltor = [i for i in plan.items if i.host == "rieltor.ua"]
    assert rieltor and all(i.body_cap == 0 for i in rieltor)


def _item(n, body_cap):
    from realty.night.codec import item_from_json, item_to_json

    url = rieltor_url(n)
    row = queue.Row(id=n, site_key=f"rieltor:{n}", source="lun", external_id=str(n),
                    original_url=url, probe_url=None, is_active=True, manual_active=None,
                    delisted_at=None, last_seen=datetime(2026, 10, 8), last_attempt=None,
                    last_checked=None, check_failures=0, absent_since=None, viewed_at=None,
                    property_id=None)
    item = queue.WorkItem(key=f"rieltor:{n}", host="rieltor.ua", url=url, tier="onetime_blind",
                          rows=(row,), body_cap=body_cap)
    return item_from_json(json.loads(json.dumps(item_to_json(item))))   # як у файлі плану


def _run(items, answers):
    from realty.liveness import capture
    from realty.night.lane import Lane
    from realty.night import codec

    cfg = policy.load()
    spec = {"host": "rieltor.ua", "family": "rieltor", "pace": 3.0, "stop_at": T0 + 3600,
            "held_path": None, "identity": None, "max_consecutive_blocks": 5,
            "block_share": 0.10, "block_min_requests": 20,
            "items": [codec.item_to_json(i) for i in items]}
    clock = FakeClock(T0)
    net = TimedNet(FakeNet(answers))
    out = io.StringIO()
    lane = Lane(spec, out, fetcher=net.for_lane("rieltor.ua", clock), cfg=cfg,
                hooks=capture.default_hooks(cfg), clock=clock,
                now_fn=lambda: utc_of(clock.time()), snapshots={})
    summary = lane.run()
    recs = [json.loads(line) for line in out.getvalue().splitlines()]
    return summary, net, {r["i"]: r for r in recs if r["t"] == "item"}


def test_get_gives_the_same_verdict_as_head_and_evidence_from_the_body():
    cap = policy.load().capture.body_hosts["rieltor.ua"]
    items = [_item(13200021, cap), _item(13200022, cap), _item(13200023, 0),
             _item(13200024, cap), _item(13200025, cap)]
    answers = {"rieltor:13200021": (200, rieltor_html(13200021)),
               "rieltor:13200022": (410, "<html><title> - RIELTOR.UA</title>410</html>"),
               "rieltor:13200023": (200, rieltor_html(13200023)),
               # Тіло не дочитано (завелике) — вердикт однаково за кодом, як у HEAD.
               "rieltor:13200024": ProbeResult(code=200, method="GET", url="", final_url="",
                                               error="too_large"),
               # Картка іншого оголошення — доказів немає (вердикт — як у HEAD).
               "rieltor:13200025": (200, rieltor_html(13299999))}
    summary, net, recs = _run(items, answers)
    methods = [m for _h, _t, _u, m in net.log]
    assert methods == ["GET", "GET", "HEAD", "GET", "GET"]
    assert summary["requests"] == 5                                # жодного зайвого запиту
    kinds = {i: (r["verdict"]["kind"], r["verdict"]["signature"]) for i, r in recs.items()}
    assert kinds == {0: ("alive", "alive"), 1: ("removed", "status_410"),
                     2: ("alive", "alive"), 3: ("alive", "alive"), 4: ("alive", "alive")}
    cap0 = recs[0]["capture"]
    assert cap0["seller_evidence"]["rieltor_role"] == "Рієлтор"
    assert cap0["place_raw"]["rieltor_zhk"] == "ЖК Паркова Алея"
    assert not recs[1]["capture"] and not recs[2]["capture"]
    assert not recs[3]["capture"] and not recs[4]["capture"]
    line = json.dumps(recs, ensure_ascii=False)
    assert NAME not in line and "000 00 01" not in line and PHONE not in line


def test_evidence_from_the_night_get_lands_only_where_empty(db):
    """Застосування (той самий шлях, що вночі: apply_outcomes) — нові ключі доказів у
    рядки ключа, наявні — без змін; статус, last_seen і ціна — як були."""
    from realty.liveness import apply as lv_apply, capture, engine, existence
    from night_kit import scope_of

    lid = add(db, rieltor_url(13200031), source="lun", external_id="c1",
              seller_evidence={"rieltor_role": "Власник"})
    before = get(db, lid)
    cfg = policy.load()
    with db() as s:
        rows = queue.load_rows(s, where=None)
    row = next(r for r in rows if r.id == lid)
    item = queue.WorkItem(key="rieltor:13200031", host="rieltor.ua", url=rieltor_url(13200031),
                          tier="onetime_blind", rows=(row,),
                          body_cap=cfg.capture.body_hosts["rieltor.ua"])
    net = engine.CountingFetcher(FakeNet({"rieltor:13200031": (200, rieltor_html(13200031))}),
                                 engine.LaneStats(host="rieltor.ua"))
    oc, _ = engine.check_one(item, net, cfg=cfg, ctx=existence.Context(cfg=cfg, now=NOW,
                                                                       snapshots={}),
                             hooks=capture.default_hooks(cfg), delay=3.0, now_fn=lambda: NOW)
    lv_apply.apply_outcomes([oc], cfg=cfg, scope=scope_of(db))
    after = get(db, lid)
    assert after.seller_evidence["rieltor_role"] == "Власник"              # не переписано
    assert after.seller_evidence["rieltor_has_agency"] is True
    assert after.place_raw["rieltor_zhk"] == "ЖК Паркова Алея"
    assert after.is_active == before.is_active and after.last_seen == before.last_seen
    assert after.price_usd == before.price_usd


def test_a_complex_name_with_a_phone_never_reaches_the_database(db):
    """Рецензія E11 (08.10): блок ЖК картки rieltor — текст продавця; номер у ньому — назву
    не пишемо зовсім (і позначки «картку бачили» для місця теж), ні в захопленні смуги, ні
    в базі. Доказ продавця (роль) — пишеться як завжди."""
    from realty.liveness import apply as lv_apply, capture, engine, existence
    from night_kit import scope_of

    cfg = policy.load()
    cap = cfg.capture.body_hosts["rieltor.ua"]
    html = rieltor_html(13200041, zhk=f"ЖК Паркова Алея, тел. {PHONE}")
    summary, _net, recs = _run([_item(13200041, cap)], {"rieltor:13200041": (200, html)})
    got = recs[0]["capture"]
    assert got["seller_evidence"]["rieltor_role"] == "Рієлтор"
    line = json.dumps(recs, ensure_ascii=False)
    assert PHONE not in line and "000 00 01" not in line and "Паркова" not in line
    lid = add(db, rieltor_url(13200041), source="lun", external_id="ph1")
    with db() as s:
        row = next(r for r in queue.load_rows(s, where=None) if r.id == lid)
    item = queue.WorkItem(key="rieltor:13200041", host="rieltor.ua", url=rieltor_url(13200041),
                          tier="onetime_blind", rows=(row,), body_cap=cap)
    net = engine.CountingFetcher(FakeNet({"rieltor:13200041": (200, html)}),
                                 engine.LaneStats(host="rieltor.ua"))
    oc, _ = engine.check_one(item, net, cfg=cfg, ctx=existence.Context(cfg=cfg, now=NOW,
                                                                       snapshots={}),
                             hooks=capture.default_hooks(cfg), delay=3.0, now_fn=lambda: NOW)
    lv_apply.apply_outcomes([oc], cfg=cfg, scope=scope_of(db))
    after = get(db, lid)
    assert after.seller_evidence["rieltor_role"] == "Рієлтор"
    text = json.dumps([after.place_raw, after.seller_evidence], ensure_ascii=False)
    assert PHONE not in text and "Паркова" not in text
    assert not (after.place_raw or {}).get("rieltor_checked_at")


def test_every_body_host_has_an_extractor():
    from realty.liveness import capture

    assert set(policy.load().capture.body_hosts) <= set(capture.BODY_EXTRACTORS)
