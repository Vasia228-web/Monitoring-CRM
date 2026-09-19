"""Свіжа установка: перший збір на порожній базі не має падати й не має
приймати записи без перевірки цін."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from realty.models import Base, Condition, Listing, MarketType
from realty.quality import housekeeping, rules
from realty.quality.staging import QualityGate


def _fresh(tmp_path, monkeypatch):
    import realty.db as db
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, future=True))
    monkeypatch.setattr(db, "init_db", lambda: Base.metadata.create_all(engine))
    monkeypatch.setattr(rules, "THRESHOLDS_FILE", tmp_path / "thresholds.json")
    return db


def test_gate_starts_on_an_empty_database(tmp_path, monkeypatch):
    """Регресія: `no such table: listings` при першому зборі на Fedora."""
    _fresh(tmp_path, monkeypatch)
    gate = QualityGate()
    assert gate.thresholds.provisional
    assert not (tmp_path / "thresholds.json").exists()     # пороги з порожнечі не пишемо


def test_without_thresholds_records_wait_for_review(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    t = rules.load_thresholds()
    rec = {"price": 60000, "price_usd": 60000.0, "rooms": 2, "location": "вул. Тестова",
           "original_url": "u", "area_total": 55.0, "price_per_sqm": 1090.0}
    verdict, reasons = rules.validate(rec, t)
    assert verdict == "review"
    assert "пороги ще не пораховані" in reasons[-1]


def test_llm_conflict_survives_revalidation(tmp_path, monkeypatch):
    db = _fresh(tmp_path, monkeypatch)
    db.init_db()
    monkeypatch.setattr(housekeeping, "session_scope", _scope(db))
    good = rules.Thresholds(price_usd=rules.Band(0, 1e9, 0, 1e9),
                            price_per_sqm=rules.Band(0, 1e9, 0, 1e9),
                            area_total=rules.Band(0, 1e9, 0, 1e9), sample_size=5000)
    with db.SessionLocal() as s:
        s.add(Listing(source="olx", external_id="1", original_url="u", price=60000,
                      price_usd=60000.0, rooms=2, location="вул. Тестова", area_total=55.0,
                      condition=Condition.UNKNOWN, market_type=MarketType.UNKNOWN,
                      quality_status="review",
                      quality_reason="LLM розійшовся з парсером: кімнат: парсер 2, LLM 5"))
        s.commit()
    housekeeping.revalidate(thresholds=good)
    with db.SessionLocal() as s:
        row = s.query(Listing).one()
        assert row.quality_status == "review"
        assert "LLM розійшовся" in row.quality_reason


def _scope(db):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        s = db.SessionLocal()
        try:
            yield s
            s.commit()
        finally:
            s.close()
    return scope
