"""Дії власника видно на НАСТУПНОМУ ж відкритті сторінки (промт 11, Блок 2).

Блок 2 прибирає з шляху запиту повторні розрахунки: частини «Аналітики» й криві
виживання тепер живуть у пам'яті знімка (D49), а далі (E5) з'являться кеші
списків. Умова власника: «кеш оновлюється після циклу, а мої дії („взяти в
обробку“, розблокування, кнопки склеювання) видно одразу». Тут — повний шлях
через сайт на синтетичній базі: дія власника → наступний GET показує результат.
"""
from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty import ops  # noqa: E402
from realty.analytics import cache, objects, segments  # noqa: E402
from realty.models import Base, Condition, DataReport, Listing, MarketType, Property  # noqa: E402
from realty.web import sessions  # noqa: E402
from realty.web.app import app  # noqa: E402

NOW = datetime(2026, 10, 7, 8, 0, 0)
OWNER, OWNER_PW = "vasia", "пароль власника 1"
FRIEND, FRIEND_PW = "druh", "druh-pass-2"
SITE = "https://mojkvartiry.test"


def _l(i, pid, **kw):
    base = dict(source="domria", external_id=str(i), original_url=f"https://example.test/{i}",
                price=50_000 + i, currency="USD", price_usd=50_000.0 + i, rooms=2,
                area_total=50.0, price_per_sqm=1000.0 + i, floor=5, floors_total=9,
                location="вул. Стуса, 30", market_type=MarketType.SECONDARY,
                condition=Condition.RENOVATED, published_at=NOW - timedelta(days=40),
                first_seen=NOW - timedelta(days=30), last_seen=NOW, quality_status="ok",
                property_id=pid, is_active=True)
    base.update(kw)
    return Listing(id=i, **base)


@pytest.fixture
def site(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        for pid in range(1, 41):
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=50.0,
                           price_usd_min=50_000.0, price_per_sqm=1000.0,
                           first_seen=NOW - timedelta(days=30), last_seen=NOW))
        s.flush()
        # Квартира 1 — три оголошення (є що розділити); 2…40 — по одному.
        s.add_all([_l(1, 1), _l(2, 1, source="olx"), _l(3, 1, source="lun")]
                  + [_l(10 + pid, pid) for pid in range(2, 41)])
        s.commit()

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    import realty.web.analytics_routes as routes
    import realty.web.app as appmod
    import realty.web.dedup_routes as dr
    import realty.web.status as status_mod
    for mod in (routes, appmod, status_mod):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(dr, "session_scope", scope)
    from realty import backup, dedup_audit, dedup_sample  # noqa: F401 — таблиці ops
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(ops_engine)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    for name, value in (("AUTH_USER", OWNER), ("AUTH_PASSWORD", OWNER_PW),
                        ("FRIEND_USER", FRIEND), ("FRIEND_PASSWORD", FRIEND_PW)):
        monkeypatch.setenv(name, value)
    for mod in (segments, objects):
        monkeypatch.setattr(mod, "_now", lambda: NOW)
    cache.invalidate()
    c = TestClient(app, base_url=SITE, client=("127.0.0.1", 50000), follow_redirects=False)
    r = c.post("/login", data={"username": OWNER, "password": OWNER_PW, "next": "/"},
               headers={"CF-Connecting-IP": "203.0.113.90", "Accept": "text/html"})
    assert r.status_code == 303
    yield c, Session
    cache.invalidate()


def _post(c, url, payload=None):
    r = c.post(url, json=payload or {}, headers={"Origin": SITE})
    assert r.status_code == 200, (url, r.status_code, r.text[:200])
    return r.json()


def _badge(html: str) -> int:
    return int(re.search(r'В обробці<span class="count">(\d+)</span>', html).group(1))


def test_take_in_work_shows_on_the_next_load(site):
    c, _ = site
    before = c.get("/processing").text
    assert 'data-id="20"' not in before and _badge(before) == 0
    _post(c, "/api/properties/10/processing", {"in_progress": True})
    after = c.get("/processing").text
    assert 'data-id="20"' in after and _badge(after) == 1
    _post(c, "/api/properties/10/processing", {"in_progress": False})
    assert 'data-id="20"' not in c.get("/processing").text


def test_manual_inactive_mark_removes_the_row_on_the_next_load(site):
    c, _ = site
    assert 'data-id="25"' in c.get("/?per_page=200").text
    _post(c, "/api/listings/25/status", {"active": False})
    assert 'data-id="25"' not in c.get("/?per_page=200").text
    _post(c, "/api/listings/25/status", {"active": None})
    assert 'data-id="25"' in c.get("/?per_page=200").text


def test_report_flag_shows_on_the_next_load(site):
    c, Session = site
    assert c.get("/api/status/reports").json()["total"] == 0
    _post(c, "/api/listings/30/report", {"field": "price"})
    items = c.get("/api/status/reports").json()["items"]
    assert [(i["listing_id"], i["field"]) for i in items] == [(30, "price")]
    with Session() as s:
        assert s.scalar(select(DataReport.listing_id)) == 30


def test_unblock_shows_on_the_next_load(site):
    c, _ = site
    for _ in range(sessions.MAX_FAILURES):
        sessions.register_failure("198.51.100.7", None, None, "x")
    assert [b["ip"] for b in c.get("/api/auth/blocks").json()["blocks"]] == ["198.51.100.7"]
    _post(c, "/api/auth/blocks/198.51.100.7/unblock")
    assert c.get("/api/auth/blocks").json()["blocks"] == []


def test_split_merge_and_undo_show_on_the_next_load(site):
    c, _ = site
    # Сторінки вже відкривали — знімок і криві в пам'яті.
    assert c.get("/property/1?verify=0").status_code == 200
    assert c.get("/analytics").status_code == 200
    new = _post(c, "/api/dedup/split", {"property_id": 1, "listing_ids": [3]})["new_property_id"]
    page = c.get(f"/property/{new}?verify=0")
    assert page.status_code == 200, "нова квартира після «розділити» — 404"

    def links(html):
        return {int(i) for i in re.findall(r'https://example\.test/(\d+)"', html)}
    assert links(page.text) == {3}
    assert links(c.get("/property/1?verify=0").text) == {1, 2}
    # «Злити» — стара адреса веде на ту, що лишилась, і там усі оголошення.
    kept = _post(c, "/api/dedup/merge", {"property_id": 1, "other": str(new)})["property_id"]
    gone = new if kept == 1 else 1
    r = c.get(f"/property/{gone}?verify=0")
    assert r.status_code == 302 and r.headers["location"].startswith(f"/property/{kept}")
    merged = c.get(f"/property/{kept}?verify=0").text
    assert links(merged) == {1, 2, 3}
    # «Скасувати рішення» — його більше немає на сторінці квартири.
    undo = re.findall(r'data-undo="(\d+)"', merged)
    assert undo, "рішення власника не видно на сторінці"
    _post(c, f"/api/dedup/decisions/{undo[0]}/undo")
    assert f'data-undo="{undo[0]}"' not in c.get(f"/property/{kept}?verify=0").text
