"""Скарги на дані та перевірка оголошення при відкритті картки."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from realty import verify
from realty.models import Base, DataReport, Listing
from realty.web.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _checkable_property() -> int:
    """Об'єкт, чиї оголошення взагалі можна перевірити (не blago)."""
    from sqlalchemy import select as sa_select

    from realty.db import SessionLocal
    from realty.verify import is_checkable

    with SessionLocal() as s:
        for row in s.scalars(
                sa_select(Listing).where(Listing.property_id.isnot(None),
                                         Listing.is_active.is_(True)).limit(200)):
            if is_checkable(row.original_url):
                return row.property_id
    raise AssertionError("у базі немає об'єкта, який можна перевірити")


def _some_listing(client) -> int:
    import re

    return int(re.search(r'data-id="(\d+)"', client.get("/").text).group(1))


# --- скарга -------------------------------------------------------------------

def test_one_click_is_enough(client):
    """Без форми й без діалогу: людина вже витратила увагу, помітивши помилку."""
    lid = _some_listing(client)
    r = client.post(f"/api/listings/{lid}/report", json={}).json()
    assert r["ok"] is True
    assert r["field"] is None
    assert r["label"] == "щось не збігається"


def test_field_can_be_named_but_is_optional(client):
    lid = _some_listing(client)
    for field, label in (("condition", "стан"), ("price", "ціна"),
                         ("gone", "оголошення вже немає")):
        r = client.post(f"/api/listings/{lid}/report", json={"field": field}).json()
        assert r["ok"] and r["label"] == label


def test_unknown_field_is_refused(client):
    lid = _some_listing(client)
    assert client.post(f"/api/listings/{lid}/report",
                       json={"field": "абищо"}).status_code == 400


def test_missing_listing_is_refused(client):
    assert client.post("/api/listings/99999999/report", json={}).status_code == 404


def test_report_keeps_a_snapshot_of_what_was_shown():
    """Дані потім зміняться — без знімка буде незрозуміло, на що скаржились."""
    from realty.models import Condition, MarketType

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        row = Listing(source="olx", external_id="1", original_url="https://x",
                      price_usd=50_000.0, rooms=2, area_total=60.0,
                      condition=Condition.RENOVATED, market_type=MarketType.PRIMARY)
        s.add(row)
        s.flush()
        s.add(DataReport(listing_id=row.id, field="condition",
                         snapshot={"condition": row.condition.value,
                                   "price_usd": row.price_usd}))
        s.commit()
        saved = s.scalars(select(DataReport)).one()
        assert saved.snapshot["condition"] == "renovated"
        assert saved.snapshot["price_usd"] == 50_000.0


def test_button_is_on_the_property_page(client):
    import re

    from sqlalchemy import select as sa_select

    from realty.db import SessionLocal
    from realty.models import Property

    with SessionLocal() as s:
        pid = s.scalars(sa_select(Property.id).limit(1)).first()
    html = client.get(f"/property/{pid}", params={"verify": "0"}).text
    assert 'class="flag"' in html
    assert "Щось не збігається" in html


# --- пріоритет ----------------------------------------------------------------

def test_reported_listings_jump_the_verification_queue(tmp_path, monkeypatch):
    """Мертве посилання дратує найбільше там, куди дивляться.

    Сліпий обхід дійде до нього через тижні, тому скарга має піднімати запис
    у черзі одразу.
    """
    from contextlib import contextmanager

    engine = create_engine(f"sqlite:///{tmp_path/'r.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(verify, "session_scope", scope)
    now = verify._now()
    with Session() as s:
        plain = Listing(source="olx", external_id="1", last_attempt=now,
                        original_url="https://olx.ua/d/uk/obyavlenie/a-IDa.html",
                        published_at=now - timedelta(days=400))
        flagged = Listing(source="olx", external_id="2", last_attempt=now,
                          original_url="https://olx.ua/d/uk/obyavlenie/b-IDb.html",
                          published_at=now - timedelta(days=1))
        s.add_all([plain, flagged])
        s.flush()
        s.add(DataReport(listing_id=flagged.id, created_at=now))
        s.commit()
        order = [c.listing_id for c in verify.collect(s)["olx.ua"]]
        assert order[0] == flagged.id


def test_opening_a_card_checks_that_exact_listing(client, monkeypatch):
    """Один запит у момент, коли він справді потрібен."""
    from realty.web import analytics_routes

    called = {}

    def fake(limit=None, ids=None, reason="sweep", **kw):
        called["ids"] = ids
        called["reason"] = reason
        return {"checked": len(ids or []), "delisted": 0, "alive": 0,
                "unknown": 0, "requests": 0, "restored": 0,
                "by_source": {}, "by_host": {}, "blocked": 0, "blocked_sources": []}

    monkeypatch.setattr("realty.verify.verify_batch", fake)
    assert client.get(f"/property/{_checkable_property()}").status_code == 200
    assert called.get("reason") == "opened"
    assert called.get("ids")


def test_a_failing_source_does_not_break_the_page(client, monkeypatch):
    """Сторінка не має падати через те, що сайт-джерело не відповів."""
    def boom(**kw):
        raise RuntimeError("мережа впала")

    monkeypatch.setattr("realty.verify.verify_batch", boom)
    assert client.get(f"/property/{_checkable_property()}").status_code == 200
