"""Зведення ловить свої помилки саме (D41): самоперевірка, сторож, вибірка, кнопки власника."""
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from realty import dedup, dedup_audit, dedup_sample, ops, watchdog
from realty.models import (
    Base, Condition, DedupDecision, Listing, MarketType, Property, PropertyRedirect,
)
from realty.web import sessions
from realty.web.app import app

NOW = datetime(2026, 9, 22)
OWNER, OWNER_PW = "vasia", "пароль власника 1"
FRIEND, FRIEND_PW = "druh", "druh-pass-2"
SITE = "https://mojkvartiry.test"


def _l(i, pid, **kw):
    base = dict(source="domria", external_id=str(i), original_url=f"https://d/{i}",
                price=65000, currency="USD", price_usd=65000.0, rooms=2, area_total=55.0,
                floor=5, floors_total=9, location="вул. Незалежності, 146",
                market_type=MarketType.SECONDARY, condition=Condition.RENOVATED,
                first_seen=NOW - timedelta(days=5), last_seen=NOW, quality_status="ok",
                property_id=pid)
    base.update(kw)
    return Listing(**base)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(ops_engine)
    with Session() as s:
        for pid in (1, 2, 3):
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=55.0,
                           first_seen=NOW, last_seen=NOW))
        s.flush()
        s.add_all([
            # Квартира 1: корпуси 34к9 і 34к7 — злито дві різні.
            _l(1, 1, identity={"korpus": "34 корпус 9"}),
            _l(2, 1, identity={"korpus": "34/7"}),
            _l(3, 1),
            # Квартира 2: чиста.
            _l(4, 2, location="вул. Стуса, 30", identity={"flat": "ria:aa"}),
            _l(5, 2, location="вул. Стуса, 30", source="lun"),
            # Квартира 3: той самий id DIM.RIA, що в квартирі 2, — пропущений дубль.
            _l(6, 3, location="вул. Стуса, 30", identity={"flat": "ria:aa"}),
        ])
        s.commit()
    return Session


def test_audit_finds_contradictions_and_missed_duplicates(db):
    with db() as s:
        res = dedup_audit.audit(s)
    assert res["suspicious"] == 1 and res["queue"][0]["property_id"] == 1
    assert res["queue"][0]["kinds"] == ["korpus"]
    assert res["missed"] == 1
    assert res["missed_list"][0] == {"kind": "ria_flat", "key": "ria:aa", "properties": [2, 3]}


def test_owner_decisions_silence_the_audit(db):
    with db() as s:
        s.add(DedupDecision(kind="same", left=[1, 2], right=[]))
        s.add(DedupDecision(kind="different", left=[4], right=[6]))
        s.commit()
        res = dedup_audit.audit(s)
    assert res["suspicious"] == 0 and res["missed"] == 0


def test_watchdog_alerts_only_on_a_sharp_rise(db):
    for n in (100, 104, 98, 101):
        dedup_audit.record({"properties": 3000, "suspicious": n, "missed": 2, "by_kind": {},
                            "queue": [], "missed_list": []}, ())
    assert watchdog.check_dedup(NOW) == []
    dedup_audit.record({"properties": 3000, "suspicious": 180, "missed": 2,
                        "by_kind": {"korpus": 90}, "queue": [], "missed_list": []}, ())
    alerts = watchdog.check_dedup(NOW)
    assert [a.key for a in alerts] == ["dedup-suspicious"]
    assert "180" in alerts[0].text and "різні корпуси" in alerts[0].text


def test_weekly_sample_verdicts(db):
    with db() as s:
        res = dedup_sample.run(seed=1, record=True, session=s)
    by = {d["property_id"]: d for d in res["details"]}
    assert by[1]["verdict"] == "several" and "корпуси" in by[1]["why"]
    # Квартира 2: DIM.RIA з id і LUN без прямого доказу — неясно, а не «одна».
    assert by[2]["verdict"] == "unclear"
    assert res["n"] == 2 and res["error_share"] == 0.5
    assert dedup_sample.recent(1)[0].several == 1


def test_description_is_no_proof_in_new_builds():
    text = "продається квартира у новому житловому комплексі " * 3
    a = dedup.Shape(1, "domria", "u1", 1, 40.0, 3, "x", frozenset(), None, 1.0, primary=True)
    b = dedup.Shape(2, "olx", "u2", 1, 40.0, 3, None, frozenset(), None, 1.0, primary=True)
    texts = {1: dedup_sample._norm(text), 2: dedup_sample._norm(text)}
    assert dedup_sample.judge([a, b], texts)[0] == "unclear"
    a2, b2 = dedup.replace(a, primary=False), dedup.replace(b, primary=False)
    assert dedup_sample.judge([a2, b2], texts)[0] == "one"


def test_new_decision_switches_off_the_one_it_contradicts(db):
    with db() as s:
        old = dedup.decide(s, "same", [1, 2], [3])
        dedup.decide(s, "different", [2], [1, 3])
        s.commit()
        assert s.get(DedupDecision, old.id).active is False
        assert s.scalar(select(DedupDecision).where(DedupDecision.active.is_(True))).kind == "different"


# --- кнопки на сторінці квартири ------------------------------------------------------

@pytest.fixture
def web(db, tmp_path, monkeypatch):
    import realty.web.analytics_routes as routes
    import realty.web.dedup_routes as dr

    @contextmanager
    def scope():
        s = db()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(dr, "session_scope", scope)
    monkeypatch.setattr(routes, "SessionLocal", db)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    monkeypatch.setenv("FRIEND_USER", FRIEND)
    monkeypatch.setenv("FRIEND_PASSWORD", FRIEND_PW)
    return db


def _as(user, pw):
    c = TestClient(app, base_url=SITE, client=("127.0.0.1", 50000), follow_redirects=False)
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"},
               headers={"CF-Connecting-IP": "203.0.113.10", "Accept": "text/html"})
    assert r.status_code == 303
    return c


def test_owner_splits_off_a_listing_and_rebuild_keeps_it(web):
    c = _as(OWNER, OWNER_PW)
    r = c.post("/api/dedup/split", json={"property_id": 1, "listing_ids": [2]},
               headers={"Origin": SITE})
    assert r.status_code == 200 and r.json()["ok"]
    new = r.json()["new_property_id"]
    assert new > 3
    with web() as s:
        pid = {l.id: l.property_id for l in s.scalars(select(Listing))}
        assert pid[2] == new and pid[1] == pid[3] == 1
        assert {l.last_seen for l in s.scalars(select(Listing))} == {NOW}   # не «бачили»
        dedup.rebuild(s, rules=())
        s.commit()
        pid2 = {l.id: l.property_id for l in s.scalars(select(Listing))}
    assert pid2[2] != pid2[1]                                   # перебудова не скасувала


def test_owner_merges_two_flats_and_the_old_link_redirects(web):
    c = _as(OWNER, OWNER_PW)
    r = c.post("/api/dedup/merge", json={"property_id": 2, "other": f"{SITE}/property/3"},
               headers={"Origin": SITE})
    assert r.json() == {"ok": True, "property_id": 2}
    with web() as s:
        assert s.get(Property, 3) is None and s.get(PropertyRedirect, 3).new_id == 2
        assert s.get(Listing, 6).property_id == 2
        dec = s.scalar(select(DedupDecision))
        assert dec.kind == "same" and set(dec.left) | set(dec.right) == {4, 5, 6}


def test_owner_can_undo_a_decision(web):
    c = _as(OWNER, OWNER_PW)
    c.post("/api/dedup/split", json={"property_id": 1, "listing_ids": [2]}, headers={"Origin": SITE})
    with web() as s:
        dec_id = s.scalar(select(DedupDecision.id))
    r = c.post(f"/api/dedup/decisions/{dec_id}/undo", headers={"Origin": SITE})
    assert r.json()["ok"]
    with web() as s:
        assert s.get(DedupDecision, dec_id).active is False


def test_friend_cannot_touch_dedup_and_does_not_see_the_buttons(web):
    f = _as(FRIEND, FRIEND_PW)
    r = f.post("/api/dedup/split", json={"property_id": 1, "listing_ids": [2]},
               headers={"Origin": SITE, "Accept": "application/json"})
    assert r.status_code == 403
    with web() as s:
        assert s.get(Listing, 2).property_id == 1


def test_foreign_site_cannot_split(web):
    c = _as(OWNER, OWNER_PW)
    r = c.post("/api/dedup/split", json={"property_id": 1, "listing_ids": [2]},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_status_api_shows_the_queue(web):
    with web() as s:
        dedup_audit.record(dedup_audit.audit(s), ())
    d = _as(OWNER, OWNER_PW).get("/api/status/dedup").json()
    assert d["last"]["suspicious"] == 1 and d["last"]["queue"][0]["property_id"] == 1
    assert d["rules"] == [] and d["kinds"]["korpus"] == "різні корпуси"


def test_buttons_are_on_the_page_for_the_owner_only(web, monkeypatch):
    import realty.web.analytics_routes as routes
    from realty.analytics import cache
    cache.invalidate()
    owner = _as(OWNER, OWNER_PW).get("/property/1?verify=0", headers={"Accept": "text/html"})
    friend = _as(FRIEND, FRIEND_PW).get("/property/1?verify=0", headers={"Accept": "text/html"})
    cache.invalidate()
    assert owner.status_code == 200, owner.text[:300]
    assert "Виправити зведення" in owner.text and 'id="dd-split"' in owner.text
    assert friend.status_code == 200 and "Виправити зведення" not in friend.text
