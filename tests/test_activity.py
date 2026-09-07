"""Актуальність оголошень: автоперевірка, ручна позначка, фільтри."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty.verify import (
    BROWSER_SOURCES, CHECKABLE, MAX_CONSECUTIVE_BLOCKS, classify,
)
from realty.web.app import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_only_explicit_gone_codes_delist():
    """Блокування чи збій мережі не мають вимикати живі оголошення."""
    assert classify(404) is False and classify(410) is False
    for code in (200, 301, 302):
        assert classify(code) is True
    for code in (0, 403, 429, 500, 502):
        assert classify(code) is None, f"код {code} не має бути висновком"


def test_blago_is_not_checkable():
    """blago віддає 200 і на живе, і на вигадане планування — сигналу немає."""
    assert "blago" not in CHECKABLE
    assert BROWSER_SOURCES <= CHECKABLE
    assert MAX_CONSECUTIVE_BLOCKS >= 1


def test_manual_mark_overrides_automatic(client):
    listing = client.get("/api/listings?limit=1").json()[0]
    lid = client.get("/api/listings?limit=1&status=all").json()[0]
    # беремо id через сторінку, бо /api/listings його не віддає
    import re
    lid = int(re.search(r'data-id="(\d+)"', client.get("/").text).group(1))

    try:
        r = client.post(f"/api/listings/{lid}/status", json={"active": False}).json()
        assert r["ok"] and r["active"] is False and r["manual_active"] is False
        assert r["is_active"] is True, "ручна позначка не має чіпати автоматичну"

        # фільтри бачать зміну
        ids = lambda q: [x["original_url"] for x in
                         client.get(f"/api/listings?{q}&limit=1000").json()]
        url = client.get(f"/api/listings?status=inactive&limit=1000").json()
        assert any(x["active"] is False for x in url)

        back = client.post(f"/api/listings/{lid}/status", json={"active": None}).json()
        assert back["manual_active"] is None and back["active"] is True
    finally:
        client.post(f"/api/listings/{lid}/status", json={"active": None})


def test_status_filter_narrows(client):
    all_rows = client.get("/api/listings?status=all&limit=1000").json()
    active = client.get("/api/listings?status=active&limit=1000").json()
    inactive = client.get("/api/listings?status=inactive&limit=1000").json()
    assert all(x["active"] for x in active)
    assert all(not x["active"] for x in inactive)
    assert len(active) <= len(all_rows)


def test_active_is_the_default(client):
    """Зняті з продажу не мають потрапляти у звичайний перегляд і статистику."""
    default = client.get("/api/listings?limit=50").json()
    assert all(x["active"] for x in default)


def test_bad_status_value_rejected(client):
    import re
    lid = int(re.search(r'data-id="(\d+)"', client.get("/").text).group(1))
    r = client.post(f"/api/listings/{lid}/status", json={"active": "нi"})
    assert r.status_code == 400
    assert client.post("/api/listings/999999999/status", json={"active": True}).status_code == 404


def test_sweep_marks_unseen_listings(monkeypatch, tmp_path):
    """Після повного обходу все, чого не було у видачі, — знято з продажу."""
    import realty.db as db
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from realty.models import Base, Listing
    import realty.verify as vf

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", Session)

    with db.session_scope() as s:
        for i in (1, 2, 3):
            s.add(Listing(source="domria", external_id=str(i), original_url=f"u{i}",
                          currency="USD", is_active=True))

    assert vf.sweep_after_full_run("domria", {"1", "3"}) == 1
    with Session() as s:
        gone = s.scalars(select(Listing).where(Listing.is_active.is_(False))).all()
        assert [r.external_id for r in gone] == ["2"]
        assert gone[0].delisted_at is not None
