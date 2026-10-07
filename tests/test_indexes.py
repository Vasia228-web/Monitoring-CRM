"""Міграція схеми й індекси Блоку 2 (крок E4, D50).

Чому окремий тест. `create_all` не додає індексів до наявної таблиці — саме тому
в робочій базі бракувало 8 індексів моделі, а на свіжій установці 6 із них були
(і ix_listings_quality_status сповільнював список: 0,4 → 36 мс). Тепер індекси
наявних таблиць доводить до моделі лише `db.migrate()` / `cli.py db migrate`:
явно, під замком циклу, зі звіркою даних до/після.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import db  # noqa: E402
from realty.models import Base, Condition, Listing, MarketType, PriceEvent, Property  # noqa: E402

NOW = datetime(2026, 10, 7, 8, 0, 0)
NEW = {"ix_listings_in_progress", "ix_listings_keeper", "ix_listings_last_checked",
       "ix_listings_last_seen", "ix_listings_visible"}
OBSOLETE = {"ix_listings_quality_status", "ix_listings_is_active", "ix_listings_manual_active",
            "ix_listings_views", "ix_listings_last_attempt", "ix_listings_last_alive_at"}


def _names(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='listings' "
            "AND sql IS NOT NULL"))}


def _fill(engine) -> None:
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        for pid in range(1, 6):
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=50.0))
        s.flush()
        for i in range(1, 21):
            s.add(Listing(id=i, source="olx" if i % 2 else "domria", external_id=str(i),
                          original_url=f"https://example.test/{i}", price_usd=40_000.0 + i,
                          rooms=1 + i % 3, market_type=MarketType.SECONDARY,
                          condition=Condition.RENOVATED, quality_status="ok",
                          property_id=1 + i % 5, last_seen=NOW - timedelta(hours=i)))
        s.flush()
        s.add(PriceEvent(listing_id=1, source="olx", price=40_001, price_usd=40_001.0,
                         observed_at=NOW))
        s.commit()


@pytest.fixture
def production_like(tmp_path):
    """База «як робоча до E4»: таблиці моделі, але без п'яти нових індексів
    (і без шести застарілих — їх там і не було)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'prod.db'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name in NEW:
            conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    _fill(engine)
    return engine


def test_migrate_creates_the_planned_indexes_and_is_idempotent(production_like):
    engine = production_like
    assert not (NEW & _names(engine))
    plan = db.migrate(dry_run=True, bind=engine)
    assert set(plan.indexes_created) == NEW and not plan.indexes_dropped
    assert not (NEW & _names(engine)), "dry-run нічого не пише"
    rep = db.migrate(bind=engine)
    assert set(rep.indexes_created) == NEW
    assert NEW <= _names(engine) and not (OBSOLETE & _names(engine))
    again = db.migrate(bind=engine)
    assert not again.changed and again.ddl == []


def test_migrated_production_db_and_fresh_install_have_the_same_indexes(tmp_path,
                                                                        production_like):
    """Свіжа установка (create_all) і мігрована робоча база — однакові індекси.

    До E4 вони розходились на 8 індексів (на свіжій — зайві, у робочій — бракує).
    """
    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    Base.metadata.create_all(fresh)
    db.migrate(bind=production_like)
    assert _names(production_like) == _names(fresh)
    assert not (OBSOLETE & _names(fresh))


def test_migrate_drops_only_the_obsolete_names(tmp_path):
    """Свіжа установка до D50 мала 6 індексів моделі, що шкодять плану, — прибрати лише їх."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old_fresh.db'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name in OBSOLETE:
            col = name.removeprefix("ix_listings_")
            conn.execute(text(f'CREATE INDEX "{name}" ON listings ({col})'))
        conn.execute(text("CREATE INDEX ix_listings_custom ON listings (title)"))
    rep = db.migrate(bind=engine)
    assert set(rep.indexes_dropped) == OBSOLETE and not rep.indexes_created
    names = _names(engine)
    assert not (OBSOLETE & names) and "ix_listings_custom" in names and NEW <= names


def test_migrate_keeps_data_integrity_and_foreign_keys(production_like):
    before = db.data_fingerprint(production_like)
    db.migrate(bind=production_like)
    assert db.data_fingerprint(production_like) == before
    assert db.integrity(production_like) == ("ok", [])


def test_url_unique_rebuild_survives_expression_indexes(tmp_path):
    """Перебудова listings (застаріле UNIQUE(original_url)) поруч з індексами за виразом.

    Рефлексія SQLAlchemy пропускає індекси за виразом: старий перелік через
    inspect().get_indexes лишив би ix_listings_visible на перейменованій таблиці,
    і create_all упав би на «index … already exists».
    """
    from sqlalchemy.schema import CreateIndex

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    Base.metadata.create_all(engine)
    _fill(engine)
    with engine.begin() as conn:     # стара схема: ще й UNIQUE(original_url) + усі індекси
        conn.execute(text("PRAGMA legacy_alter_table=ON"))
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name='listings'")).scalar()
        for name in [r[1] for r in conn.execute(text('PRAGMA index_list("listings")'))
                     if r[3] == "c"]:
            conn.execute(text(f'DROP INDEX "{name}"'))
        conn.execute(text("ALTER TABLE listings RENAME TO listings_old"))
        legacy = sql.replace("UNIQUE (source, external_id)",
                             "UNIQUE (source, external_id), UNIQUE (original_url)")
        assert legacy != sql
        conn.execute(text(legacy))
        conn.execute(text("INSERT INTO listings SELECT * FROM listings_old"))
        conn.execute(text("DROP TABLE listings_old"))
        for ix in Listing.__table__.indexes:
            conn.execute(CreateIndex(ix))
    assert "ix_listings_visible" in _names(engine)
    before = db.data_fingerprint(engine)
    rep = db.migrate(bind=engine)
    assert rep.rebuild_listings
    with engine.connect() as conn:
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name='listings'")).scalar()
    assert "UNIQUE (original_url)" not in sql
    assert db.data_fingerprint(engine) == before
    assert NEW <= _names(engine)
    assert db.integrity(engine) == ("ok", [])


def test_add_missing_columns_tolerates_a_concurrent_migration(tmp_path, monkeypatch):
    """Два процеси одночасно бачать колонку відсутньою — другий ALTER не валить старт."""
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE listings DROP COLUMN viewed_at"))
    stale = db._missing_columns(engine)                # те, що «побачив» другий процес
    assert [c for _t, c, _d in stale] == ["viewed_at"]
    db._add_missing_columns(engine)                    # перший процес додав
    monkeypatch.setattr(db, "_missing_columns", lambda bind=None: stale)
    db._add_missing_columns(engine)                    # другий — «duplicate column»: не помилка


def test_cli_db_migrate_refuses_while_the_cycle_holds_the_lock(tmp_path, monkeypatch, capsys):
    """`cli.py db migrate` не йде поруч із циклом: замок зайнятий — відмова, код 2."""
    from realty import dbmigrate, runner

    lock_path = tmp_path / "cycle.lock"
    monkeypatch.setattr(runner, "LOCK_PATH", lock_path)
    holder = runner.CycleLock(lock_path)
    assert holder.acquire()
    try:
        code = dbmigrate.migrate(dry_run=False, wait_min=0.0)
    finally:
        holder.release()
    assert code == 2
    assert "ВІДМОВА" in capsys.readouterr().out


def _migrate_cli_on(engine, tmp_path, monkeypatch):
    """`cli.py db migrate` (dbmigrate.migrate) на тестовій базі й своєму замку циклу."""
    from realty import dbmigrate, ops, runner

    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / "cycle.lock")
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(ops, "init_ops", lambda force=False: None)
    return dbmigrate


def test_cli_db_migrate_holds_the_write_lock_so_a_site_write_cannot_fake_a_mismatch(
        production_like, tmp_path, monkeypatch, capsys):
    """Рецензія: під час `db migrate` сайт старого коду пише check_events (перевірка
    в GET) — а знімки до/після бралися без блокування запису, і один такий запис
    давав «відновити з бекапу» (82 720 → 82 721, код 1), тобто стерти справжні
    дані. Тепер знімки й DDL — в одній транзакції BEGIN IMMEDIATE: чужий запис
    чекає її кінця."""
    import sqlite3

    dbmigrate = _migrate_cli_on(production_like, tmp_path, monkeypatch)
    path = production_like.url.database
    real, outcome = db.migrate, []

    def migrate_while_the_site_writes(*, dry_run=False, bind=None):
        rep = real(dry_run=dry_run, bind=bind)
        if not dry_run:                               # посеред побудови індексів
            con = sqlite3.connect(path, timeout=0.2)
            try:
                con.execute("INSERT INTO check_events (listing_id, checked_at, code, alive, "
                            "reason) VALUES (1, '2026-10-07 08:00:00', 200, 1, 'opened')")
                con.commit()
                outcome.append("записано посеред міграції")
            except sqlite3.OperationalError as e:
                outcome.append(str(e))
            finally:
                con.close()
        return rep

    monkeypatch.setattr(db, "migrate", migrate_while_the_site_writes)
    code = dbmigrate.migrate(dry_run=False, wait_min=0.0)
    out = capsys.readouterr().out
    assert outcome == ["database is locked"]          # чекає (busy_timeout), а не вклинюється
    assert code == 0 and "МІГРАЦІЯ ГОТОВА" in out
    assert NEW <= _names(production_like)
    with production_like.begin() as conn:             # після коміту запис проходить
        conn.execute(text("INSERT INTO check_events (listing_id, checked_at, code, alive, "
                          "reason) VALUES (1, '2026-10-07 08:00:00', 200, 1, 'opened')"))


def test_cli_db_migrate_rolls_the_schema_back_when_the_data_moved(
        production_like, tmp_path, monkeypatch, capsys):
    """Розбіжність до/після — ROLLBACK тієї самої транзакції: індексів немає, дані
    цілі, бекап не потрібен (правило власника: розбіжність — відкат і стоп)."""
    dbmigrate = _migrate_cli_on(production_like, tmp_path, monkeypatch)
    real = db.migrate
    before = db.data_fingerprint(production_like)

    def migrate_and_lose_a_row(*, dry_run=False, bind=None):
        rep = real(dry_run=dry_run, bind=bind)
        if not dry_run and bind is not None:          # у транзакції міграції
            bind.execute(text("DELETE FROM price_events"))
        elif not dry_run:                             # міграція без спільної транзакції
            with production_like.begin() as conn:
                conn.execute(text("DELETE FROM price_events"))
        return rep

    monkeypatch.setattr(db, "migrate", migrate_and_lose_a_row)
    code = dbmigrate.migrate(dry_run=False, wait_min=0.0)
    out = capsys.readouterr().out
    assert code == 1 and "ROLLBACK" in out and "count(price_events)" in out
    assert not (NEW & _names(production_like))        # індекси теж відкочено
    assert db.data_fingerprint(production_like) == before
