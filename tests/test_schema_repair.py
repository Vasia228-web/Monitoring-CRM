"""Ремонт битих зовнішніх ключів: відтворюємо саме ту історію, що сталась."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty import schema_repair


def _legacy_db(path: Path) -> None:
    """Як було насправді: price_events → listings, потім міграція перейменувала
    listings на listings_legacy (SQLite переписав посилання), створила нову
    listings, перенесла дані й видалила legacy."""
    c = sqlite3.connect(path, isolation_level=None)
    c.executescript("""
        CREATE TABLE listings (id INTEGER PRIMARY KEY, url TEXT UNIQUE, price REAL);
        CREATE TABLE price_events (id INTEGER NOT NULL, listing_id INTEGER NOT NULL,
            price FLOAT, observed_at DATETIME NOT NULL, PRIMARY KEY (id),
            FOREIGN KEY(listing_id) REFERENCES listings (id));
        CREATE INDEX ix_pe_listing ON price_events (listing_id, observed_at);
        INSERT INTO listings VALUES (1, 'a', 100), (2, 'b', 200);
        INSERT INTO price_events VALUES (1, 1, 100, '2026-09-01'), (2, 2, 200, '2026-09-02'),
                                        (3, 1, 90, '2026-09-10');
        ALTER TABLE listings RENAME TO listings_legacy;
        CREATE TABLE listings (id INTEGER PRIMARY KEY, url TEXT, price REAL);
        INSERT INTO listings SELECT * FROM listings_legacy;
        DROP TABLE listings_legacy;
    """)
    c.close()


def test_reproduces_the_real_breakage(tmp_path):
    db = tmp_path / "r.db"
    _legacy_db(db)
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys = ON")
    try:
        c.execute("INSERT INTO price_events VALUES (4, 1, 80, '2026-09-20')")
        raise AssertionError("запис мав упасти")
    except sqlite3.OperationalError as e:
        assert "listings_legacy" in str(e)
    c.close()


def test_repair_keeps_every_row_and_makes_writes_work(tmp_path):
    db = tmp_path / "r.db"
    _legacy_db(db)
    rep = schema_repair.repair(db)
    assert rep.ok and rep.applied, rep.problems
    assert [(d.table, d.missing, d.target) for d in rep.dangling] == \
        [("price_events", "listings_legacy", "listings")]
    assert rep.before == rep.after                    # кожен рядок кожної таблиці
    assert rep.integrity == "ok" and rep.fk_violations == []
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("INSERT INTO price_events VALUES (4, 1, 80, '2026-09-20')")   # тепер пише
    c.commit()
    idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_pe_listing" in idx                     # індекси відтворено
    assert schema_repair.find_dangling(c) == []
    c.close()


def test_dry_run_changes_nothing(tmp_path):
    db = tmp_path / "r.db"
    _legacy_db(db)
    before = db.read_bytes()
    rep = schema_repair.repair(db, apply=False)
    assert rep.ok and not rep.applied
    assert schema_repair.find_dangling(sqlite3.connect(db))   # досі бите
    assert db.read_bytes() == before


def test_orphans_block_the_repair(tmp_path):
    """Сироти (подія ціни без оголошення) — не ремонтуємо мовчки, а зупиняємось."""
    db = tmp_path / "r.db"
    _legacy_db(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO price_events VALUES (9, 999, 1, '2026-09-20')")
    c.commit(); c.close()
    rep = schema_repair.repair(db)
    assert not rep.applied
    assert any("foreign_key_check" in p for p in rep.problems)
    assert schema_repair.find_dangling(sqlite3.connect(db))   # відкочено повністю


def test_unknown_target_is_not_guessed(tmp_path):
    db = tmp_path / "r.db"
    c = sqlite3.connect(db, isolation_level=None)
    c.executescript("""
        CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER REFERENCES ghost(id));
        INSERT INTO a VALUES (1, 1);
    """)
    c.close()
    rep = schema_repair.repair(db)
    assert not rep.applied and "невідомо" in rep.problems[0]
