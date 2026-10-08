"""`cli.py db migrate|plans` — міграція схеми під замком циклу й плани запитів (Блок 2, E4).

Процедура розгортання (інтеграційний план, «процедура»): цикл і нічні роботи
неактивні → бекап із перевіркою → `cli.py db migrate --dry-run` (план DDL, лише
читання) → `cli.py db migrate`. Команда сама:
  * бере замок циклу з обмеженим очікуванням (типово ≤10 хв, інакше відмова —
    міграцію не можна вести поруч із кроками циклу, а чекати вічно — теж);
  * знімок «до», увесь DDL і знімок «після» — в ОДНІЙ транзакції BEGIN
    IMMEDIATE на одному з'єднанні (DDL у SQLite транзакційний). Замок циклу
    не зупиняє інших записувачів: сайт, що працює під час міграції, пише
    check_events (перевірка при відкритті старого коду) і скарги — і одне
    відкриття квартири за 3–10 с побудови індексів на Fedora давало
    «розбіжність» і вказівку відновити бекап, тобто стерти справжні записи
    (рецензія Блоку 2). Тепер інші записувачі чекають кінця транзакції
    (busy_timeout: 30 с у коду, 2 с і повтор — у фонового запису сайту);
  * розбіжність «до/після» — ROLLBACK тієї самої транзакції: схема лишається
    як була, бекап не потрібен (правило власника: розбіжність — відкат, стоп,
    числа). integrity_check і foreign_key_check — після коміту, поза
    блокуванням (на HDD це секунди); їх збій — ненульовий код і вказівка
    відновити з бекапу.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def wait_cycle_lock(wait_min: float, *, poll_s: float = 15.0, lock_path=None):
    """Замок циклу з обмеженим очікуванням: None — цикл не звільнив його вчасно.

    Замок береться неблокуючи й опитується — як у дозборі identity.
    """
    from .runner import LOCK_PATH, CycleLock

    lock = CycleLock(lock_path or LOCK_PATH)
    deadline = time.monotonic() + wait_min * 60
    announced = False
    while not lock.acquire():
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        if not announced:
            print(f"цикл збору тримає замок (PID {lock.holder().get('pid')}) — "
                  f"чекаю до {wait_min:.0f} хв…", flush=True)
            announced = True
        time.sleep(min(poll_s, left))
    return lock


def _header() -> None:
    from . import db

    print(f"SQLite: {sqlite3.sqlite_version} (Python {sys.version.split()[0]})")
    print(f"база: {db.engine.url.render_as_string(hide_password=True)}")


class _Mismatch(Exception):
    """Знімки «до» й «після» в одній транзакції різні — відкотити її."""


def locked_engine(url):
    """Рушій міграції: кожна транзакція — BEGIN IMMEDIATE (блокування запису одразу).

    pysqlite сам відкриває транзакцію лише перед DML, не перед DDL, і лише
    DEFERRED — тож його логіку вимкнено (isolation_level = None) і BEGIN
    IMMEDIATE пише сам рушій (рецепт документації SQLAlchemy для SQLite).
    Ті самі PRAGMA, що й у робочого рушія (db._tune_sqlite).
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    from .db import BUSY_TIMEOUT_MS

    eng = create_engine(url, future=True, poolclass=NullPool)

    @event.listens_for(eng, "connect")
    def _connect(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None
        cur = dbapi_connection.cursor()
        try:
            cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            cur.execute("PRAGMA foreign_keys = ON")
        finally:
            cur.close()

    @event.listens_for(eng, "begin")
    def _begin(conn) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return eng


def _diff_note(key: str, before, after) -> str:
    """Що означає різниця: ріст таблиці — не те саме, що втрата рядків."""
    if before == after:
        return ""
    if key.startswith("count(") and isinstance(before, int) and isinstance(after, int):
        return f"   ← РІЗНИЦЯ ({'+' if after > before else ''}{after - before} рядків)"
    return "   ← РІЗНИЦЯ"


def migrate(*, dry_run: bool, wait_min: float) -> int:
    """0 — готово (або лише план), 1 — дані чи цілісність не збіглися, 2 — замок зайнятий."""
    from . import db, ops

    _header()
    plan = db.migrate(dry_run=True)
    print("\nПЛАН ЗМІН СХЕМИ" + (" (нічого не пишу)" if dry_run else ""))
    for line in plan.ddl or ["— схема вже відповідає моделі"]:
        print(f"  {line}")
    try:
        ops_plan = ops.pending_schema()
    except Exception as e:                              # noqa: BLE001 — лише показ
        ops_plan = [f"(не прочитано: {e})"]
    print("ops.db (застосує init_ops() будь-якого процесу — сайт, сторож, крок; не ця "
          "транзакція):")
    for line in ops_plan or ["— схема вже відповідає моделі"]:
        print(f"  {line}")
    if dry_run:
        return 0

    lock = wait_cycle_lock(wait_min)
    if lock is None:
        print(f"ВІДМОВА: цикл збору не звільнив замок за {wait_min:.0f} хв — "
              f"запустіть між циклами.")
        return 2
    eng = locked_engine(db.engine.url)
    rolled_back = False
    try:
        if plan.rebuild_listings:
            # Застаріле UNIQUE(original_url): перебудова таблиці потребує PRAGMA
            # foreign_keys=off, який у транзакції не діє, — тому окремо й першою
            # (так само її робить init_db() будь-якого процесу; кількість рядків
            # звіряється всередині). Решта — в одній транзакції нижче.
            print("перебудова listings (зняти UNIQUE(original_url)) — окремо, перед міграцією")
            db._drop_obsolete_url_unique()
        started = time.perf_counter()
        try:
            with eng.begin() as conn:                    # BEGIN IMMEDIATE
                before = db.data_fingerprint(conn)
                rep = db.migrate(bind=conn)
                after = db.data_fingerprint(conn)
                if before != after:
                    raise _Mismatch()
        except _Mismatch:
            rolled_back = True
        took = time.perf_counter() - started
        if not rolled_back:
            ops.init_ops(force=True)            # нові таблиці ops.db (web_generations, …)
            check, fk = db.integrity()
    finally:
        eng.dispose()
        lock.release()
    if rolled_back:
        print(f"\n  {'що':<28}{'до':>28}{'після':>28}")
        for key in before:
            print(f"  {key:<28}{str(before[key]):>28}{str(after.get(key)):>28}"
                  f"{_diff_note(key, before[key], after.get(key))}")
        print("УВАГА: дані в транзакції міграції змінились — ROLLBACK виконано, схема "
              "лишилась як була (бекап не потрібен). Зупинитися, показати числа власнику.")
        return 1
    print(f"\nзастосовано за {took:.1f} с (одна транзакція): створено індексів "
          f"{len(rep.indexes_created)}, видалено застарілих {len(rep.indexes_dropped)}, "
          f"нових таблиць {len(rep.tables_created)}, колонок {len(rep.columns_added)}")
    print(f"\n  {'що':<28}{'до':>28}{'після':>28}")
    for key in before:
        print(f"  {key:<28}{str(before[key]):>28}{str(after.get(key)):>28}"
              f"{_diff_note(key, before[key], after.get(key))}")
    print(f"  integrity_check: {check}")
    print(f"  foreign_key_check: {len(fk)} порушень" + (f" {fk[:5]}" if fk else ""))
    ok = check == "ok" and not fk
    print("МІГРАЦІЯ ГОТОВА" if ok else
          "УВАГА: цілісність бази після міграції не підтверджено — зупинитися, відновити "
          "з бекапу, показати числа власнику")
    return 0 if ok else 1


def _print_plans(rows: list[dict], *, verbose: bool) -> int:
    bad = [r for r in rows if r["bad"]]
    for r in rows:
        if verbose or r["bad"]:
            print(f"  [{'ПОГАНО' if r['bad'] else 'ok':6}] {r['label']}")
            for line in r["plan"]:
                print(f"            {line}")
    print(f"запитів перевірено: {len(rows)}; із проходом усієї таблиці listings: {len(bad)}")
    return len(bad)


def plans(*, preview: bool, verbose: bool) -> int:
    """EXPLAIN запитів сайту (лише читання). `preview` — на тимчасовій копії з
    індексами міграції: так на SQLite Fedora видно плани ДО `db migrate`."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from . import db
    from .config import DATA_DIR
    from .ops import _now
    from .web import plans as site_plans

    _header()
    if not preview:
        with db.SessionLocal() as s:
            bad = _print_plans(site_plans.report(s, _now()), verbose=verbose)
        return 1 if bad else 0
    # Копія через backup API у теку на ТОМУ Ж диску (не /tmp: на Fedora це може
    # бути пам'ять, а вільно ~160 МБ — D45), міграція копії, плани, видалення.
    src = Path(db.engine.url.database)
    tmp = Path(tempfile.mkdtemp(prefix="plans-preview-", dir=DATA_DIR))
    try:
        copy = tmp / "preview.db"
        a, b = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(copy)
        with b:
            a.backup(b)
        a.close()
        b.close()
        eng = create_engine(f"sqlite:///{copy}", future=True)
        rep = db.migrate(bind=eng)
        print(f"копія з індексами міграції ({', '.join(rep.indexes_created) or 'нових немає'})")
        with Session(bind=eng) as s:
            bad = _print_plans(site_plans.report(s, _now()), verbose=verbose)
        eng.dispose()
        return 1 if bad else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
