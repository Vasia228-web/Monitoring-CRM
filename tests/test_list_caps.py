"""Перелік уперся в стелю сторінок — попередження в щоденному зведенні (власник 09.10).

«Якщо перелік будь-якого джерела впирається в стелю сторінок — попередження в
щоденному зведенні, а не тихе обрізання.» До цієї зміни: повний перелік LUN, що вперся
в 240 сторінок, лише не зберігався («помилки сторінок») — тривоги не було; нічний прохід
стрічки вважав стелю кінцем; звичайний збір DOM.RIA/OLX, у якого остання дозволена
сторінка ще мала нові, мовчав. Тепер кожен такий випадок — рядок ops.list_caps, а сторож
дає попередження list-cap:<джерело>-<вид> у зведення.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import add, db  # noqa: E402,F401
from night_kit import clean_night  # noqa: E402,F401
from test_night_feed import FakeFeed, _lun_rows, _run  # noqa: E402
from test_snapshot_absent import _fake_lun, _Pages  # noqa: E402
from seller_kit import lun_obj  # noqa: E402

from realty import config, configfiles, ops, snapshot, watchdog  # noqa: E402
from realty.sources import REGISTRY  # noqa: E402

NOW = datetime(2026, 10, 9, 8, 40)


@pytest.fixture
def caps(tmp_path, monkeypatch):
    """Окрема ops.db: лише рядки цього тесту."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.init_ops(force=True)

    def rows():
        with ops.ops_session() as s:
            return [(r.source, r.kind, r.cap) for r in s.scalars(select(ops.ListCap))]
    return rows


def test_full_list_cut_by_the_cap_is_recorded(caps, monkeypatch):
    monkeypatch.setitem(config.SOURCES, "lun", replace(config.SOURCES["lun"], full_pages=3))
    _fake_lun(monkeypatch)
    snap = snapshot.capture("lun", fetcher=_Pages(lambda n: f"PAGE{n}"))
    assert snap.complete is False
    assert caps() == [("lun", "full", 3)]


def test_fresh_collection_of_a_source_without_recency_is_not_a_cap(caps, monkeypatch):
    """LUN без сортування за датою: стеля звичайного збору — норма (глибше дочитують
    повний перелік і нічний прохід)."""
    _fake_lun(monkeypatch)
    src = REGISTRY["lun"](fetcher=_Pages(lambda n: f"PAGE{n}"), mode="fresh")
    list(src.run())
    assert caps() == []


def _fake_domria(monkeypatch, new_on_every_page: bool):
    cls = REGISTRY["domria"]
    monkeypatch.setattr(cls, "_search_page", lambda self, page, limit=None: [page * 10 + 1])
    monkeypatch.setattr(cls, "_card", lambda self, rid: {"id": rid})
    monkeypatch.setattr(cls, "_parse", lambda self, d: {
        "external_id": str(d["id"]), "original_url": f"https://dom.ria.com/x-{d['id']}.html",
        "price": 50000, "currency": "USD"})
    known = set() if new_on_every_page else {str(p * 10 + 1) for p in range(2, 100)}
    return cls(mode="fresh", known_ids=known)


def test_fresh_collection_with_new_listings_on_the_last_page_is_recorded(caps, monkeypatch):
    """DOM.RIA (найновіші першими): усі 8 сторінок мали нові — нове могло лишитись за
    стелею; записано з кількістю нових на останній сторінці."""
    src = _fake_domria(monkeypatch, new_on_every_page=True)
    list(src.run())
    assert caps() == [("domria", "fresh", src.cfg.max_pages)]
    with ops.ops_session() as s:
        assert s.scalars(select(ops.ListCap.detail)).one().endswith(": 1")


def test_fresh_collection_that_stopped_on_a_page_without_new_is_not_a_cap(caps, monkeypatch):
    src = _fake_domria(monkeypatch, new_on_every_page=False)
    list(src.run())
    assert src.stats["pages"] < src.cfg.max_pages
    assert caps() == []


def test_night_feed_pass_that_hits_its_page_cap_is_recorded(db, caps):
    """Нічний прохід стрічки: стеля max_pages, а сторінки ще є — прохід завершено, але
    не тихо (на коді до зміни — просто «finished»)."""
    _lun_rows(db, 6)
    fake = FakeFeed({p: [lun_obj(p)] for p in range(1, 6)})
    feed_caps = dict(configfiles.load("night").feed.max_pages, lun=2)
    rep = _run(db, "lun", fake, min_missing_active=1, max_pages=feed_caps)
    assert rep.get("cap_hit") is True
    assert caps() == [("lun", "feed", 2)]


def test_watchdog_warns_in_the_digest_and_forgets_after_the_window(caps):
    with ops.ops_session() as s:
        s.add(ops.ListCap(at=NOW - timedelta(hours=3), source="lun", kind="full", cap=320,
                          detail="сторінки ще не скінчились"))
        s.add(ops.ListCap(at=NOW - timedelta(hours=2), source="olx", kind="fresh", cap=5,
                          detail="нових оголошень на останній сторінці: 12"))
        s.add(ops.ListCap(at=NOW - timedelta(hours=1), source="olx", kind="fresh", cap=5,
                          detail="нових оголошень на останній сторінці: 7"))
    alerts = {a.key: a for a in watchdog.check_list_caps(NOW)}
    assert set(alerts) == {"list-cap:lun-full", "list-cap:olx-fresh"}
    levels = configfiles.load("alerts").levels
    assert all(watchdog.level_of(k, levels) == "warning" for k in alerts)
    assert "320" in alerts["list-cap:lun-full"].text
    assert "2 рази" in alerts["list-cap:olx-fresh"].text and ": 7" in alerts["list-cap:olx-fresh"].text
    later = NOW + timedelta(hours=configfiles.load("alerts").list_cap.window_hours + 1)
    assert watchdog.check_list_caps(later) == []
