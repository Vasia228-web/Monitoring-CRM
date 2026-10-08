"""Актуальність оголошень: автоперевірка, ручна позначка, фільтри."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty.verify import HOSTS, MAX_CONSECUTIVE_BLOCKS, classify, host_key
from realty.web.app import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_only_explicit_gone_codes_delist():
    """Блокування чи збій мережі не мають вимикати живі оголошення.

    Змінено в E8 (D52): один 404 — «не знайдено», не «знято» (рішення власника 1, D46).
    """
    assert classify(410) is False
    assert classify(404) is None
    for code in (200, 301, 302):
        assert classify(code) is True
    for code in (0, 403, 429, 500, 502):
        assert classify(code) is None, f"код {code} не має бути висновком"


def test_blago_is_not_checkable():
    """blago віддає 200 і на живе, і на вигадане планування — сигналу немає."""
    assert "blagodeveloper.com" not in HOSTS
    assert MAX_CONSECUTIVE_BLOCKS >= 1


def test_queues_are_keyed_by_host_not_by_source():
    """LUN агрегує OLX, тож його посилання ведуть на чужий сайт.

    Якби черги нарізались за назвою джерела, `lun` і `olx` били б в olx.ua
    удвічі частіше, ніж передбачає пауза — тобто рівно з тим ризиком
    блокування, якого ми уникаємо.
    """
    lun_to_olx = "https://www.olx.ua/d/uk/obyavlenie/kvartira-IDxxxx.html"
    own_olx = "https://olx.ua/d/uk/obyavlenie/insha-IDyyyy.html"
    assert host_key(lun_to_olx) == host_key(own_olx) == "olx.ua"
    assert host_key("https://rieltor.ua/ivano-frankovsk/flats-sale/view/1/") == "rieltor.ua"


def _first_listing_id(client) -> int:
    import re

    return int(re.search(r'data-id="(\d+)"', client.get("/").text).group(1))


def test_manual_mark_overrides_automatic(client):
    """Ручна позначка життєвого циклу лишилась як API поверх автоперевірки.

    UI-фільтр «Актуальність» прибрано, але саме поле нікуди не поділось: його
    читає шар контролю якості, рахуючи пороги лише по живих оголошеннях.
    """
    lid = _first_listing_id(client)
    try:
        r = client.post(f"/api/listings/{lid}/status", json={"active": False}).json()
        assert r["ok"] and r["active"] is False and r["manual_active"] is False
        assert r["is_active"] is True, "ручна позначка не має чіпати автоматичну"

        # Зняте з продажу зникає зі списку — без жодного фільтра.
        shown = client.get("/api/listings?limit=1000").json()
        assert all(x.get("active", True) for x in shown)

        back = client.post(f"/api/listings/{lid}/status", json={"active": None}).json()
        assert back["manual_active"] is None and back["active"] is True
    finally:
        client.post(f"/api/listings/{lid}/status", json={"active": None})


def test_removed_filters_are_gone_from_api_and_ui(client):
    """Критерій приймання: фільтрів «Якість» і «Актуальні» немає ніде."""
    page = client.get("/").text
    assert 'name="status"' not in page and 'name="quality"' not in page

    # Невідомі параметри просто ігноруються, а не змінюють вибірку.
    base = client.get("/api/listings?limit=100").json()
    with_old = client.get("/api/listings?limit=100&status=inactive&quality=rejected").json()
    assert [r["original_url"] for r in base] == [r["original_url"] for r in with_old]


def test_only_live_and_clean_records_are_shown(client):
    """Зняті з продажу й такі, що не пройшли контроль, у видачу не потрапляють."""
    rows = client.get("/api/listings?limit=200").json()
    assert rows and all(r.get("active", True) for r in rows)


def test_bad_status_value_rejected(client):
    lid = _first_listing_id(client)
    r = client.post(f"/api/listings/{lid}/status", json={"active": "нi"})
    assert r.status_code == 400
    assert client.post("/api/listings/999999999/status", json={"active": True}).status_code == 404


def test_full_run_never_delists_by_absence(monkeypatch, tmp_path):
    """Повний обхід, що не побачив оголошення, нікого не знімає (заміна в E8, D52).

    Досі тут стояв test_sweep_marks_unseen_listings: `sweep_after_full_run` знімав
    за відсутністю у видачі — це суперечить правилу явного сигналу (рішення власника
    1, D46). Функцію прибрано; повний прогін зі штучним джерелом, яке не віддало 2
    з 3 актуальних оголошень, лишає всі три актуальними.
    """
    import realty.db as db
    import realty.pipeline as pl
    import realty.verify as vf
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from realty.models import Base, Listing
    from realty.sources.base import BaseSource

    assert not hasattr(vf, "sweep_after_full_run")
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", Session)

    from contextlib import contextmanager

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(pl, "session_scope", scope)
    monkeypatch.setattr(pl, "init_db", lambda: None)
    with scope() as s:
        for i in (1, 2, 3):
            s.add(Listing(source="fake", external_id=str(i),
                          original_url=f"https://flombu.com/uk/estate_deal_sales/{i}",
                          currency="USD", is_active=True, quality_status="ok"))

    class Fake(BaseSource):
        name = "fake"

        def iter_listings(self):
            yield {"source": "fake", "external_id": "1", "price": 50000, "currency": "USD",
                   "original_url": "https://flombu.com/uk/estate_deal_sales/1",
                   "rooms": 1, "area_total": 40.0}

    monkeypatch.setitem(pl.REGISTRY, "fake", Fake)
    from realty.quality.staging import QualityGate

    class Pass(QualityGate):
        def screen(self, session, batch):
            return batch

    report = pl.Pipeline(sources=["fake"], use_llm=False, mode="full", gate=Pass()).run()
    assert report.delisted == 0
    with Session() as s:
        rows = s.scalars(select(Listing)).all()
        assert len(rows) == 3
        assert all(r.is_active for r in rows), "відсутність у видачі не знімає"
        assert all(r.delisted_at is None for r in rows)
