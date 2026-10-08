"""Схема S4 (Блок 4, E10, D57): `db migrate` додає колонки місця й покривний індекс,
дані не змінюються, мігрована база = свіжа установка; ops.db — places_runs і
dedup_audits.suspicious_core."""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import places_kit as kit  # noqa: E402
from realty import db  # noqa: E402
from realty.models import Base  # noqa: E402

LISTING_S4 = ("district_key", "district_how", "complex_key", "complex_how", "place_area",
              "place_at", "place_sig", "row_district", "row_complex", "row_area")
PROPERTY_S4 = ("district_key", "complex_key", "place_area", "place_conflict")


def _pre_s4(engine) -> None:
    """База «як робоча після D54»: усе, крім схеми S4."""
    with engine.begin() as conn:
        conn.execute(text('DROP INDEX IF EXISTS "ix_listings_place"'))
        for col in LISTING_S4:
            conn.execute(text(f'ALTER TABLE listings DROP COLUMN "{col}"'))
        for col in PROPERTY_S4:
            conn.execute(text(f'ALTER TABLE properties DROP COLUMN "{col}"'))


def _schema(engine) -> tuple:
    insp = inspect(engine)
    cols = {t: sorted(c["name"] for c in insp.get_columns(t)) for t in ("listings", "properties")}
    return cols, sorted(db.existing_indexes(engine))


def test_s4_migration_adds_columns_and_index_without_touching_data(tmp_path):
    engine = kit.make_engine(tmp_path, "prod.db")
    kit.build(engine, n=60)
    _pre_s4(engine)
    before = db.data_fingerprint(engine)
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT id, district, complex_name, location, title, last_seen "
                                "FROM listings ORDER BY id")).all()
    plan = db.migrate(dry_run=True, bind=engine)
    assert {f"listings.{c}" for c in LISTING_S4} | {f"properties.{c}" for c in PROPERTY_S4} \
        == set(plan.columns_added)
    assert plan.indexes_created == ["ix_listings_place"]
    db.migrate(bind=engine)
    assert db.data_fingerprint(engine) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id, district, complex_name, location, title, last_seen "
                                 "FROM listings ORDER BY id")).all() == raw
        assert conn.execute(text("SELECT count(*) FROM listings WHERE district_key IS NOT NULL "
                                 "OR row_district IS NOT NULL")).scalar() == 0
    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    Base.metadata.create_all(fresh)
    assert _schema(engine) == _schema(fresh)
    assert db.integrity(engine) == ("ok", [])
    assert not db.migrate(bind=engine).changed


def test_ops_tables_for_places(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from realty import ops

    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", eng)
    monkeypatch.setattr(ops, "OpsSession", sessionmaker(bind=eng, future=True))
    ops.init_ops(force=True)
    names = set(inspect(eng).get_table_names())
    assert "places_runs" in names
    assert "suspicious_core" in {c["name"] for c in inspect(eng).get_columns("dedup_audits")}
