"""Підключення до БД та сесії."""
from __future__ import annotations

from contextlib import contextmanager

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import DB_URL
from .models import Base

log = logging.getLogger(__name__)

engine = create_engine(DB_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    _drop_obsolete_url_unique()
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _drop_obsolete_url_unique() -> None:
    """Знімає застаріле UNIQUE(original_url) перебудовою таблиці.

    SQLite не вміє видаляти обмеження через ALTER, а обмеження помилкове:
    LUN віддає посилання на olx.ua, і той самий URL законно приходить від двох
    джерел. Дані переносяться повністю.
    """
    insp = inspect(engine)
    if not insp.has_table("listings"):
        return
    obsolete = any(
        uc.get("column_names") == ["original_url"]
        for uc in insp.get_unique_constraints("listings")
    )
    if not obsolete:
        return
    cols = [c["name"] for c in insp.get_columns("listings")]
    names = ", ".join(f'"{c}"' for c in cols)
    # Індекси в SQLite переїжджають разом із перейменованою таблицею, але їхні
    # імена лишаються глобальними, тож без цього кроку CREATE INDEX конфліктує.
    old_indexes = [ix["name"] for ix in insp.get_indexes("listings") if ix.get("name")]
    log.warning("Міграція: знімаємо UNIQUE(original_url) — перебудова таблиці")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=off"))
        conn.execute(text("ALTER TABLE listings RENAME TO listings_legacy"))
        for ix in old_indexes:
            conn.execute(text(f'DROP INDEX IF EXISTS "{ix}"'))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO listings ({names}) SELECT {names} FROM listings_legacy"
        ))
        moved = conn.execute(text("SELECT COUNT(*) FROM listings")).scalar()
        legacy = conn.execute(text("SELECT COUNT(*) FROM listings_legacy")).scalar()
        if moved != legacy:
            raise RuntimeError(
                f"перенесено {moved} із {legacy} рядків — таблицю listings_legacy лишаю"
            )
        conn.execute(text("DROP TABLE listings_legacy"))
        conn.execute(text("PRAGMA foreign_keys=on"))
    log.warning("Міграція завершена: перенесено %s рядків", moved)


def _add_missing_columns() -> None:
    """Проста міграція: доливає нові колонки в уже створену таблицю.

    Схема тут росте лише додаванням полів, тож повноцінний Alembic був би
    надмірним — але й мовчки втрачати зібрані дані не хочеться.
    """
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = col.type.compile(engine.dialect)
            default = " DEFAULT 0" if ddl.upper().startswith(("BOOL", "INT")) else ""
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}{default}"
                ))


@contextmanager
def session_scope():
    s: Session = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
