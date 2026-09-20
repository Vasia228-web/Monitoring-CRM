"""Читання не має падати через те, що інший процес пише.

20.09.2026 сторож не зміг прочитати базу під час циклу: «database is locked».
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event, text


def _tuned(path: Path):
    from realty.db import BUSY_TIMEOUT_MS
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _r):
        cur = conn.cursor()
        cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA journal_mode = WAL")
        cur.close()

    return engine


def test_reader_is_not_blocked_by_a_writer(tmp_path):
    engine = _tuned(tmp_path / "t.db")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        c.execute(text("INSERT INTO t (v) VALUES ('було')"))

    stop = threading.Event()

    def writer():
        while not stop.is_set():
            with engine.begin() as c:
                c.execute(text("INSERT INTO t (v) VALUES ('нове')"))
            time.sleep(0.005)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        started = time.monotonic()
        for _ in range(50):                    # читаємо під безперервним записом
            with engine.connect() as c:
                assert c.execute(text("SELECT COUNT(*) FROM t")).scalar() >= 1
        assert time.monotonic() - started < 10
    finally:
        stop.set()
        t.join(timeout=5)


def test_real_database_uses_wal():
    """Робоча база має бути в WAL — інакше сторож і сайт чекають на збір."""
    from realty.db import engine
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
