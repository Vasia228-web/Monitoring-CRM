"""Схема S3 Блоку 1 через db.migrate (E8, D52): лише доповнення, дані ті самі.

listing_events (FK на listings і check_events), listings.absent_since (частковий
індекс), source_removed_at, probe_url, докази Блоків 3/4 (seller_evidence,
seller_profile з індексом, seller_evidence_at, place_raw), check_events.signature.
На коді до E8 жодного з цих полів не було.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty import db  # noqa: E402
from realty.models import Base  # noqa: E402

S3_COLUMNS = ("absent_since", "source_removed_at", "probe_url", "seller_evidence",
              "seller_profile", "seller_evidence_at", "place_raw")


def _pre_s3(path):
    """База зі схемою до E8: таблиці моделі без S3 і з рядками."""
    eng = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE listing_events"))
        for ix in ("ix_listings_absent_since", "ix_listings_seller_profile"):
            conn.execute(text(f'DROP INDEX "{ix}"'))
        for col in S3_COLUMNS:
            conn.execute(text(f"ALTER TABLE listings DROP COLUMN {col}"))
        conn.execute(text("ALTER TABLE check_events DROP COLUMN signature"))
        conn.execute(text(
            "INSERT INTO listings (id, source, original_url, external_id, currency, is_active, "
            "views, check_failures, in_progress, quality_status, price_estimated, "
            "detail_enriched, llm_extracted, first_seen, last_seen, price_usd, market_type, "
            "condition) VALUES "
            "(1, 'olx', 'https://www.olx.ua/d/uk/obyavlenie/x-ID10Mig.html', '10Mig', 'USD', "
            "1, 0, 0, 0, 'ok', 0, 0, 0, '2026-09-01', '2026-10-01', 50000, 'UNKNOWN', "
            "'UNKNOWN')"))
        conn.execute(text("INSERT INTO check_events (listing_id, checked_at, code, alive, reason)"
                          " VALUES (1, '2026-10-01', 200, 1, 'sweep')"))
    return eng


def test_migrate_adds_s3_without_touching_data(tmp_path):
    eng = _pre_s3(tmp_path / "old.db")
    plan = db.migrate(dry_run=True, bind=eng)
    assert "listing_events" in plan.tables_created
    assert {f"listings.{c}" for c in S3_COLUMNS} | {"check_events.signature"} \
        <= set(plan.columns_added)
    assert {"ix_listings_absent_since", "ix_listings_seller_profile"} <= set(plan.indexes_created)
    before = db.data_fingerprint(eng)
    db.migrate(bind=eng)
    assert db.data_fingerprint(eng) == before
    check, fk = db.integrity(eng)
    assert check == "ok" and fk == []
    with eng.connect() as conn:
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = "
                                "'ix_listings_absent_since'")).scalar()
        assert "WHERE absent_since IS NOT NULL" in sql, "частковий індекс (конфлікт 1)"
        fks = {(r[2], r[3]) for r in conn.execute(text("PRAGMA foreign_key_list(listing_events)"))}
        assert fks == {("listings", "listing_id"), ("check_events", "check_event_id")}
    assert db.migrate(dry_run=True, bind=eng).changed is False, "повтор — порожній план"


def test_fresh_install_and_migrated_base_have_the_same_s3(tmp_path):
    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    Base.metadata.create_all(fresh)
    old = _pre_s3(tmp_path / "old.db")
    db.migrate(bind=old)

    def shape(eng):
        with eng.connect() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(listings)"))}
            ev = {r[1] for r in conn.execute(text("PRAGMA table_info(listing_events)"))}
            ix = {r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"))}
        return cols, ev, ix

    assert shape(fresh) == shape(old)
