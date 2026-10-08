"""Спільний нічний прохід стрічки LUN і flombu (E11, D60): докази лише туди, де порожньо.

Інтеграція, конфлікт 4: один прохід стрічки за ніч замість трьох; захоплює identity,
place_raw і seller_evidence; пише після кожної сторінки й продовжує з місця зупинки;
не збір — нових оголошень, цін, подій ціни, last_seen і стану актуальності не чіпає.
На коді до E11 (realty/night/feed.py немає) уночі йшов `scrape --no-detail` —
звичайний збір стрічки, що вставляв нові оголошення й ставив last_seen.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import add, db, get  # noqa: E402,F401
from night_kit import clean_night, scope_of  # noqa: E402,F401
from seller_kit import NAME, PHONE, flombu_item, lun_obj  # noqa: E402

from realty import configfiles  # noqa: E402
from realty.models import Listing, PriceEvent  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_night")
NOW = datetime(2026, 10, 8, 23, 30)
SEEN = datetime(2026, 10, 1)


def _cfgs(**feed):
    ncfg = configfiles.load("night")
    if feed:
        ncfg = dataclasses.replace(ncfg, feed=dataclasses.replace(ncfg.feed, **feed))
    return ncfg, configfiles.load("seller")


def _lun_page(objs) -> str:
    payload = "".join(json.dumps(o, ensure_ascii=False) for o in objs) or "[]"
    return f'<script>self.__next_f.push([1,{json.dumps(payload, ensure_ascii=False)}])</script>'


class FakeFeed:
    """Фетчер стрічки: сторінка → відповідь; журнал запитів."""

    def __init__(self, pages: dict[int, list], *, flombu_pages: int | None = None) -> None:
        self.pages = pages
        self.flombu_pages = flombu_pages
        self.log: list[str] = []

    def get(self, url, params=None, headers=None, delay=None):
        self.log.append(url)
        page = int(url.split("page=")[1]) if "page=" in url else 1
        return _lun_page(self.pages.get(page, []))

    def get_json(self, url, params=None, delay=None):
        page = int(params["page"])
        self.log.append(f"{url}#{page}")
        items = self.pages.get(page, [])
        return {"data": items, "included": [], "meta": {"pages": self.flombu_pages}}

    def close(self):
        pass


class Gate:
    """Ворота смуги: зупиняються після `allow` сторінок (дедлайн чи блокування)."""

    pace = 1.6

    def __init__(self, feed: FakeFeed, allow: int = 10 ** 6) -> None:
        self.feed, self.allow = feed, allow

    def stopped(self, margin: float = 0.0) -> bool:
        return len(self.feed.log) >= self.allow


def _run(db, source, fake, *, allow=10 ** 6, now=NOW, **feed):
    from realty.night import feed as night_feed

    ncfg, scfg = _cfgs(**feed)
    return night_feed.run(source, 0.0, Gate(fake, allow), ncfg=ncfg, scfg=scfg,
                          scope=scope_of(db), now_fn=lambda: now, fetcher=fake)


def _lun_rows(db, n, start=0):
    return [add(db, f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{13300000 + i}/",
                source="lun", external_id=str(4720690000 + i)) for i in range(start, start + n)]


def test_feed_pass_writes_only_where_empty_and_never_collects(db):
    keep = add(db, "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13300001/",
               source="lun", external_id="4720690001", identity={"flat": "lun:old"},
               seller_evidence={"lun_contact_type": "owner"})
    fresh = add(db, "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13300002/",
                source="lun", external_id="4720690002")
    before = {lid: get(db, lid) for lid in (keep, fresh)}
    with db() as s:
        prices = s.scalar(select(func.count()).select_from(PriceEvent))
    fake = FakeFeed({1: [lun_obj(1), lun_obj(2), lun_obj(3)]})     # 3 — нового рядка немає
    rep = _run(db, "lun", fake, min_missing_active=1)
    assert rep["status"] == "finished" and rep["pages"] == 2 and rep["matched"] == 2
    a, b = get(db, keep), get(db, fresh)
    assert a.identity == {"flat": "lun:old"}                       # не переписано
    assert a.seller_evidence["lun_contact_type"] == "owner"        # не переписано
    assert a.seller_evidence["lun_active_offers"] == 14            # нові ключі — так
    assert b.identity and b.seller_evidence["lun_checked_at"]
    assert b.place_raw["lun_geo_checked_at"]
    for lid, row in ((keep, a), (fresh, b)):
        old = before[lid]
        assert (row.last_seen, row.price_usd, row.is_active, row.title, row.description) == \
            (old.last_seen, old.price_usd, old.is_active, old.title, old.description)
    with db() as s:
        assert s.scalar(select(func.count()).select_from(Listing)) == 2     # не вставляє
        assert s.scalar(select(func.count()).select_from(PriceEvent)) == prices
    text = json.dumps([a.seller_evidence, b.seller_evidence, b.identity, b.place_raw],
                      ensure_ascii=False)
    assert NAME not in text and "067" not in text and PHONE not in text


def test_feed_pass_writes_after_each_page_and_resumes_where_it_stopped(db):
    from realty.night import evidence, feed as night_feed

    _lun_rows(db, 6, 1)
    pages = {1: [lun_obj(1), lun_obj(2)], 2: [lun_obj(3), lun_obj(4)], 3: [lun_obj(5), lun_obj(6)]}
    fake = FakeFeed(pages)
    rep = _run(db, "lun", fake, allow=1, min_missing_active=1)      # дедлайн після сторінки 1
    assert rep["status"] == "stopped" and rep["pages"] == 1
    st = evidence.state_get(night_feed.state_name("lun"))
    assert st["next_page"] == 2 and not st.get("finished_at")
    with db() as s:                                                 # сторінка 1 — уже в базі
        got = {r.external_id: bool(r.seller_evidence) for r in s.scalars(select(Listing))}
    assert got["4720690001"] and got["4720690002"] and not got["4720690003"]
    fake2 = FakeFeed(pages)
    rep2 = _run(db, "lun", fake2, min_missing_active=1, now=NOW + timedelta(hours=3))
    assert rep2["first_page"] == 2 and rep2["status"] == "finished"
    assert all("page=1" not in u and u.endswith(("page=2", "page=3", "page=4")) for u in fake2.log)
    with db() as s:
        assert all(r.seller_evidence for r in s.scalars(select(Listing)))
    assert evidence.state_get(night_feed.state_name("lun"))["finished_at"]


def test_feed_pass_runs_only_when_needed_and_cools_down(db):
    from realty.night import evidence, feed as night_feed

    _lun_rows(db, 3, 1)
    rep = _run(db, "lun", FakeFeed({1: []}), min_missing_active=50)
    assert rep["status"] == "skipped" and "не потрібно" in rep["why"]
    rep = _run(db, "lun", FakeFeed({1: [lun_obj(1)]}), min_missing_active=1)
    assert rep["status"] == "finished" and rep["gain"] == 1        # 1 < low_gain 200
    later = NOW + timedelta(days=3)
    rep = _run(db, "lun", FakeFeed({1: []}), min_missing_active=1, now=later)
    assert rep["status"] == "skipped" and "через" in rep["why"]    # малий виграш — тиждень
    st = evidence.state_get(night_feed.state_name("lun"))
    st["gain"] = 500                                                # великий — доба
    evidence.state_put(night_feed.state_name("lun"), st)
    rep = _run(db, "lun", FakeFeed({1: []}), min_missing_active=1,
               now=NOW + timedelta(hours=25))
    assert rep["status"] == "finished"


def test_flombu_feed_pass_takes_owner_type_but_not_the_phone_id(db):
    for i in (1, 2, 3):
        add(db, f"https://flombu.com/uk/estate_deal_sales/{116000 + i}", source="flombu",
            external_id=str(116000 + i))
    fake = FakeFeed({1: [flombu_item(1), flombu_item(2)], 2: [flombu_item(3)]}, flombu_pages=2)
    rep = _run(db, "flombu", fake, min_missing_active=1)
    assert rep["status"] == "finished" and rep["pages"] == 2 and len(fake.log) == 2
    with db() as s:
        rows = s.scalars(select(Listing)).all()
    assert all(r.seller_evidence["flombu_owner_type"] == "agent" for r in rows)
    assert all("a1b2c3d4e5" not in json.dumps(r.seller_evidence) for r in rows)
    assert all(r.seller_profile is None for r in rows)
