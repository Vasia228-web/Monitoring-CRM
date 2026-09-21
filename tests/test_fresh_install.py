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


def test_tiny_sample_does_not_narrow_price_bounds(tmp_path, monkeypatch):
    """Регресія з проби на Fedora: 26 оголошень flombu дали коридор цін
    $18 900–80 900, і карантин відхилив 35% справжніх оголошень OLX."""
    db = _fresh(tmp_path, monkeypatch)
    db.init_db()
    with db.SessionLocal() as s:
        for i in range(26):
            s.add(Listing(source="flombu", external_id=str(i), original_url=f"u{i}",
                          price=30000 + 1000 * i, price_usd=30000.0 + 1000 * i,
                          price_per_sqm=700.0 + 10 * i, area_total=45.0, rooms=2,
                          location="вул. Тестова", is_active=True, quality_status="review",
                          condition=Condition.UNKNOWN, market_type=MarketType.UNKNOWN))
        s.commit()
    t = rules.load_thresholds()
    assert t.provisional
    rec = {"price": 140000, "price_usd": 140000.0, "rooms": 3, "location": "вул. Тестова",
           "original_url": "u", "area_total": 70.0, "price_per_sqm": 2000.0}
    verdict, _ = rules.validate(rec, t)
    assert verdict == "review"          # на перегляд, а не «відхилено»


def test_rejected_batch_marks_the_run_failed(tmp_path, monkeypatch):
    """Ескалація карантину: зібрано, але не записано — це не «ok»."""
    from sqlalchemy import select

    from realty import ops
    from realty.pipeline import Pipeline
    from realty.sources import REGISTRY
    from realty.sources.base import BaseSource

    db = _fresh(tmp_path, monkeypatch)
    db.init_db()
    import realty.pipeline as pl
    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "session_scope", _scope(db))
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession", sessionmaker(bind=ops_engine, expire_on_commit=False,
                                                        future=True))
    ops.OpsBase.metadata.create_all(ops_engine)

    class Junk(BaseSource):
        name = "junk"

        def iter_listings(self):
            for i in range(30):      # без кімнат і адреси — усе буде відхилено
                yield {"external_id": str(i), "original_url": f"https://x/{i}", "price": 1}

    monkeypatch.setitem(REGISTRY, "junk", Junk)
    Pipeline(sources=["junk"], use_llm=False).run()
    with ops.ops_session() as s:
        run = s.scalars(select(ops.RunRecord)).one()
    assert run.status == "failed"
    assert "ЕСКАЛАЦІЯ" in run.message and run.inserted == 0 and run.kept == 30


def test_browser_requests_are_counted_for_their_source():
    from realty.pipeline import Pipeline
    p = Pipeline(use_llm=False)
    assert p._get_browser(2.5, "olx").label == "olx"


def test_write_failure_marks_the_run_failed(tmp_path, monkeypatch):
    """Запис падає в базі — прогін «failed» з причиною і лічильником, а не «ok»."""
    from sqlalchemy import select

    from realty import ops
    from realty.pipeline import Pipeline
    from realty.sources import REGISTRY
    from realty.sources.base import BaseSource

    db = _fresh(tmp_path, monkeypatch)
    db.init_db()
    import realty.pipeline as pl
    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "session_scope", _scope(db))
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession", sessionmaker(bind=ops_engine, expire_on_commit=False,
                                                        future=True))
    ops.OpsBase.metadata.create_all(ops_engine)

    class Fine(BaseSource):
        name = "fine"

        def iter_listings(self):
            for i in range(5):
                yield {"external_id": str(i), "original_url": f"https://x/{i}", "price": 60000,
                       "rooms": 2, "location": "вул. Тестова", "area_total": 55.0}

    def broken_upsert(session, rec):
        raise RuntimeError("no such table: main.listings_legacy")

    monkeypatch.setitem(REGISTRY, "fine", Fine)
    monkeypatch.setattr(Pipeline, "_upsert", staticmethod(broken_upsert))
    Pipeline(sources=["fine"], use_llm=False).run()
    with ops.ops_session() as s:
        run = s.scalars(select(ops.RunRecord)).one()
    assert run.status == "failed"
    assert run.skipped == 5 and "не записано 5 із 5" in run.message
    assert "listings_legacy" in run.message
