"""`cli.py links reindex|selftest|parse` — ключ listings.site_key (крок E6, D51).

  * `reindex [--dry-run]` — разове заповнення ключа ЛИШЕ там, де він NULL
    (нове порожнє поле; непорожнього не перезаписує). Під замком циклу
    (очікування ≤ --wait-min, інакше відмова, код 2), пакетами ≤ `reindex.batch_rows`
    (config/links.toml, ≤200) рядків на транзакцію BEGIN IMMEDIATE; у кожній
    транзакції — відбиток усіх інших колонок рядків пакета до й після UPDATE
    (розбіжність — ROLLBACK, стоп, код 1). Друкує кількості: без ключа до/після,
    заповнено, не розібрано (id).
  * `reindex --fix-mismatched [--dry-run]` — після правки config/links.toml: ще й
    переписує ключі, що НЕ збігаються з links.site_key(original_url) (слухачі
    ORM перераховують ключ лише при зміні адреси, тож старі рядки самі не
    виправляться). Та сама механіка; UPDATE — лише якщо ключ у рядку той, що
    був у плані (`site_key IS :old`). Друкує ключі за сімейством до/після і
    розбіжності до/після (має бути 0).
  * `selftest` — лише читання: кожна збережена адреса розбирається й знаходить
    свій рядок за ключем (частка за джерелом), ключ у базі = ключ з адреси,
    ключі в кількох квартирах, групи id OLX, однакові без урахування регістру.
  * `parse ТЕКСТ` — налагодження розбору: сімейство, ключ, канонічна адреса.

Нові рядки ключ отримують самі — слухачі ORM (`realty/models.py`).
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import text

from . import links



# --- Підключення ----------------------------------------------------------------------------


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """sqlite3 лише для читання. Для бази в режимі WAL потрібен файл -shm; якщо
    його немає (копія), а -wal порожній чи відсутній — immutable (вмісту WAL
    немає, тож нічого не губиться). Інакше — помилка, а не тихе читання без WAL."""
    p = Path(path).resolve()
    if not p.exists():
        raise SystemExit(f"бази немає: {p}")
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.OperationalError:
        wal = Path(f"{p}-wal")
        if wal.exists() and wal.stat().st_size:
            raise
        conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn


def db_path(arg: str | None) -> Path:
    """--db або файл бази з DB_URL (лише SQLite)."""
    if arg:
        return Path(arg).resolve()
    from .db import engine

    if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        raise SystemExit("потрібна база SQLite (DB_URL) або --db")
    return Path(engine.url.database).resolve()


def write_engine(path: Path):
    """Рушій запису: кожна транзакція — BEGIN IMMEDIATE, ті самі PRAGMA, що й у
    робочого рушія (dbmigrate.locked_engine)."""
    from .dbmigrate import locked_engine

    return locked_engine(f"sqlite:///{path}")


# --- Відбиток рядків ------------------------------------------------------------------------


def listing_columns(conn) -> list[str]:
    return [r[1] for r in conn.execute(text('PRAGMA table_info("listings")'))]


def rows_digest(conn, ids: list[int], columns: list[str]) -> str:
    """sha256 значень `columns` рядків `ids` (у порядку id) — доказ, що UPDATE
    не зачепив інших колонок."""
    h = hashlib.sha256()
    cols = ", ".join(f'"{c}"' for c in columns)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ", ".join(str(int(i)) for i in chunk)
        for row in conn.execute(text(f"SELECT {cols} FROM listings WHERE id IN ({marks}) "
                                     f"ORDER BY id")):
            h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def table_digest(conn, exclude: set[str]) -> str:
    """sha256 усіх колонок listings, крім `exclude`, для всієї таблиці."""
    cols = [c for c in listing_columns(conn) if c not in exclude]
    ids = [r[0] for r in conn.execute(text("SELECT id FROM listings ORDER BY id"))]
    return rows_digest(conn, ids, cols)


# --- reindex --------------------------------------------------------------------------------


class _BatchMismatch(Exception):
    pass


def _plan(path: Path, fix_mismatched: bool = False
          ) -> tuple[int, list[tuple[int, str, str | None, str | None]], list[tuple[int, str]]]:
    """(рядків усього, [(id, джерело, ключ у рядку, новий ключ)] до запису,
    [(id, джерело)] нерозібрані без ключа).

    Без `fix_mismatched` — лише рядки з NULL; з ним — ще й ті, де ключ у рядку
    не дорівнює ключу з адреси (новий ключ може бути й None — адреса більше не
    оголошення)."""
    conn = open_readonly(path)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("listings")')}
        if "site_key" not in cols:
            raise SystemExit("колонки listings.site_key ще немає — спершу `cli.py db migrate`")
        total = conn.execute("SELECT count(*) FROM listings").fetchone()[0]
        where = "" if fix_mismatched else " WHERE site_key IS NULL"
        todo, unparsed = [], []
        for lid, source, url, stored in conn.execute(
                f"SELECT id, source, original_url, site_key FROM listings{where} ORDER BY id"):
            key = links.site_key(url)
            if key is None and stored is None:
                unparsed.append((lid, source))
            elif key != stored:
                todo.append((lid, source, stored, key))
        return total, todo, unparsed
    finally:
        conn.close()


def _null_count(path: Path) -> int:
    conn = open_readonly(path)
    try:
        return conn.execute("SELECT count(*) FROM listings WHERE site_key IS NULL").fetchone()[0]
    finally:
        conn.close()


def _family_counts(path: Path) -> Counter:
    """Ключі в базі за сімейством (частина до «:»)."""
    conn = open_readonly(path)
    try:
        return Counter(dict(conn.execute(
            "SELECT substr(site_key, 1, instr(site_key, ':') - 1), count(*) FROM listings "
            "WHERE site_key IS NOT NULL GROUP BY 1")))
    finally:
        conn.close()


def _mismatch_count(path: Path) -> int:
    """Непорожні ключі, що не дорівнюють ключу з адреси (як у selftest)."""
    conn = open_readonly(path)
    try:
        return sum(1 for url, stored in conn.execute(
            "SELECT original_url, site_key FROM listings WHERE site_key IS NOT NULL")
            if links.site_key(url) != stored)
    finally:
        conn.close()


def reindex(*, db: str | None = None, dry_run: bool = False, wait_min: float = 10.0,
            batch: int | None = None, fix_mismatched: bool = False, out=print) -> int:
    """0 — готово (чи лише план), 1 — розбіжність (стоп), 2 — замок зайнятий.

    `batch` — для тестів (менше пакетів); інакше `reindex.batch_rows` з config/links.toml.
    """
    from . import db as dbmod
    from .dbmigrate import wait_cycle_lock

    path = db_path(db)
    limit = links.config().reindex.batch_rows
    batch = max(1, min(int(batch if batch is not None else limit), limit))
    out(f"база: {path}")
    started = time.perf_counter()
    total, todo, unparsed = _plan(path, fix_mismatched)
    fill = [t for t in todo if t[2] is None]
    fix = [t for t in todo if t[2] is not None]
    null_before = _null_count(path)
    out(f"рядків: {total}; без ключа (site_key IS NULL): {null_before}; "
        f"заповнимо: {len(fill)}; не розібрано: {len(unparsed)} "
        f"(розбір {time.perf_counter() - started:.2f} с)")
    for src, n in sorted(Counter(src for _, src, _, _ in fill).items()):
        out(f"  {src:<8} {n}")
    if unparsed:
        out("  нерозібрані id (лишаться NULL): "
            + ", ".join(str(i) for i, _ in unparsed[:20]) + (" …" if len(unparsed) > 20 else ""))
    fam_before = None
    if fix_mismatched:
        fam_before = _family_counts(path)
        out(f"ключ у базі ≠ ключ з адреси: {len(fix)} — перепишемо "
            f"(з них стане NULL: {sum(1 for t in fix if t[3] is None)})")
        for src, n in sorted(Counter(src for _, src, _, _ in fix).items()):
            out(f"  {src:<8} {n}")
    if dry_run:
        out("--dry-run: нічого не записано")
        return 0
    if not todo:
        out("заповнювати нічого" if not fix_mismatched else "заповнювати й виправляти нічого")
        return 0

    lock = wait_cycle_lock(wait_min)
    if lock is None:
        out(f"ВІДМОВА: цикл збору не звільнив замок за {wait_min:.0f} хв — запустіть між циклами.")
        return 2
    eng = write_engine(path)
    filled = txns = 0
    max_txn_s = 0.0
    stopped = None
    try:
        with eng.connect() as conn:
            before = dbmod.data_fingerprint(conn)
            other = [c for c in listing_columns(conn) if c != "site_key"]
        t0 = time.perf_counter()
        for start in range(0, len(todo), batch):
            chunk = todo[start:start + batch]
            ids = [lid for lid, _, _, _ in chunk]
            t_txn = time.perf_counter()
            try:
                with eng.begin() as conn:                         # BEGIN IMMEDIATE
                    d_before = rows_digest(conn, ids, other)
                    # `IS :old`: NULL — лише порожнє (FILL_ONLY); інакше — лише якщо
                    # ключ у рядку той самий, що в плані (ніхто не змінив тим часом).
                    res = conn.execute(
                        text("UPDATE listings SET site_key = :k WHERE id = :id AND site_key IS :old"),
                        [{"k": key, "id": lid, "old": old} for lid, _, old, key in chunk])
                    if rows_digest(conn, ids, other) != d_before:
                        raise _BatchMismatch()
                    filled += res.rowcount or 0
            except _BatchMismatch:
                stopped = (ids[0], ids[-1])
                break
            txns += 1
            max_txn_s = max(max_txn_s, time.perf_counter() - t_txn)
        took = time.perf_counter() - t0
        with eng.connect() as conn:
            after = dbmod.data_fingerprint(conn)
            check, fk = dbmod.integrity(conn)
    finally:
        eng.dispose()
        lock.release()
    null_after = _null_count(path)
    what = "заповнено й виправлено" if fix_mismatched else "заповнено"
    out(f"\n{what}: {filled} за {took:.2f} с; транзакцій: {txns} (≤{batch} рядків, "
        f"найдовша {max_txn_s * 1000:.0f} мс)")
    out(f"без ключа: {null_before} → {null_after} (нерозібраних {len(unparsed)})")
    mismatch_after = 0
    if fix_mismatched:
        fam_after = _family_counts(path)
        mismatch_after = _mismatch_count(path)
        out(f"ключ у базі ≠ ключ з адреси: {len(fix)} → {mismatch_after}")
        out(f"  {'сімейство':<12}{'ключів до':>12}{'після':>10}")
        for fam in sorted(set(fam_before) | set(fam_after)):
            out(f"  {fam:<12}{fam_before.get(fam, 0):>12}{fam_after.get(fam, 0):>10}")
    out(f"\n  {'що':<28}{'до':>28}{'після':>28}")
    for key in before:
        mark = "" if before[key] == after.get(key) else "   ← РІЗНИЦЯ"
        out(f"  {key:<28}{str(before[key]):>28}{str(after.get(key)):>28}{mark}")
    out(f"  integrity_check: {check}; foreign_key_check: {len(fk)} порушень")
    if stopped:
        out(f"УВАГА: у пакеті id {stopped[0]}…{stopped[1]} змінилось щось, крім site_key — "
            f"ROLLBACK цього пакета, зупинено. Показати числа власнику.")
        return 1
    listings_same = all(before[k] == after.get(k) for k in before if "listings" in k)
    emptied = sum(1 for t in fix if t[3] is None)
    ok = (listings_same and check == "ok" and not fk and mismatch_after == 0
          and null_after <= len(unparsed) + emptied)
    if not ok:
        out("УВАГА: числа не збіглись — зупинитися, показати власнику.")
    else:
        out("ГОТОВО")
    return 0 if ok else 1


# --- selftest -------------------------------------------------------------------------------


def selftest(*, db: str | None = None) -> dict:
    """Лише читання. Див. докстрінг модуля."""
    path = db_path(db)
    conn = open_readonly(path)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("listings")')}
        has_col = "site_key" in cols
        rows = conn.execute(
            "SELECT id, source, external_id, original_url, property_id"
            + (", site_key" if has_col else ", NULL") + " FROM listings ORDER BY id").fetchall()
        redirects = dict(conn.execute("SELECT old_id, new_id FROM property_redirects"))
    finally:
        conn.close()

    def resolve(pid):
        seen = set()
        while pid in redirects and pid not in seen:
            seen.add(pid)
            pid = redirects[pid]
        return pid

    t0 = time.perf_counter()
    computed = {lid: links.site_key(url) for lid, _, _, url, _, _ in rows}
    parse_s = time.perf_counter() - t0
    by_key: dict[str, list[int]] = defaultdict(list)
    for lid, _, _, _, _, stored in rows:
        key = stored if has_col and stored is not None else computed[lid]
        if key:
            by_key[key].append(lid)
    per = defaultdict(Counter)
    unparsed, mismatched = [], []
    for lid, source, ext, url, pid, stored in rows:
        st = per[source]
        st["rows"] += 1
        key = computed[lid]
        if key is None:
            unparsed.append(lid)
            if has_col and stored is not None:
                mismatched.append(lid)                   # ключ є, а адреса — не оголошення
            continue
        st["parsed"] += 1
        if lid in by_key.get(key, ()):
            st["maps_back"] += 1
        if has_col:
            if stored == key:
                st["stored_equal"] += 1
            elif stored is None:
                st["stored_null"] += 1
            else:
                mismatched.append(lid)
        fam, ident = key.split(":", 1)
        if fam == source:
            st["own_site"] += 1
            if ident == str(ext):
                st["id_equals_external_id"] += 1
        else:
            st[f"via:{fam}"] += 1
    pid_of = {lid: pid for lid, _, _, _, pid, _ in rows}
    multi_rows = {k: v for k, v in by_key.items() if len(v) > 1}
    multi_props = {k for k, v in by_key.items()
                   if len({resolve(pid_of[i]) for i in v if pid_of[i] is not None}) > 1}
    olx_ci = defaultdict(set)
    for k in by_key:
        if k.startswith("olx:"):
            olx_ci[k.lower()].add(k)
    return {
        "db": str(path), "rows": len(rows), "has_site_key_column": has_col,
        "parse_seconds": round(parse_s, 3),
        "per_source": {s: dict(c) for s, c in sorted(per.items())},
        "unparsed": len(unparsed), "unparsed_ids": unparsed[:20],
        "stored_mismatch": len(mismatched), "stored_mismatch_ids": mismatched[:20],
        "keys": len(by_key), "keys_with_several_rows": len(multi_rows),
        "rows_in_shared_keys": sum(len(v) for v in multi_rows.values()),
        "keys_in_several_properties": len(multi_props),
        "olx_case_insensitive_collision_groups": sum(1 for v in olx_ci.values() if len(v) > 1),
    }


def render_selftest(rep: dict) -> str:
    lines = [f"база: {rep['db']}", f"рядків: {rep['rows']}; розбір {rep['parse_seconds']} с"]
    lines.append(f"  {'джерело':<9}{'рядків':>8}{'розібр.':>9}{'свій рядок':>24}"
                 f"{'ключ=у базі':>13}{'id=external_id':>16}  через сайти")
    for src, st in rep["per_source"].items():
        n = st["rows"]
        back_n = st.get("maps_back", 0)
        # Частка — униз до 0,1%, а не округлення: 7 999 із 8 001 — «99.9%», не «100.0%»
        # (контрольна точка E6 — рівно 100%). Неповна — ще й «≠».
        pct = math.floor(1000 * back_n / n) / 10 if n else 0.0
        via = ", ".join(f"{k[4:]} {v}" for k, v in sorted(st.items()) if k.startswith("via:"))
        own = st.get("own_site", 0)
        own_txt = f"{st.get('id_equals_external_id', 0)}/{own}" if own else "—"
        back = f"{back_n}/{n} ({pct:.1f}%)" + (" ≠" if back_n < n else "")
        lines.append(f"  {src:<9}{n:>8}{st.get('parsed', 0):>9}{back:>24}"
                     f"{st.get('stored_equal', 0):>13}{own_txt:>16}  {via}")
    lines += [
        f"не розібрано: {rep['unparsed']}" + (f" (id {rep['unparsed_ids']})" if rep['unparsed'] else ""),
        f"ключ у базі ≠ ключ з адреси: {rep['stored_mismatch']}"
        + (f" (id {rep['stored_mismatch_ids']})" if rep['stored_mismatch'] else ""),
        f"різних ключів: {rep['keys']}; ключів із кількома рядками: {rep['keys_with_several_rows']} "
        f"({rep['rows_in_shared_keys']} рядків)",
        f"ключів, що ведуть у кілька квартир: {rep['keys_in_several_properties']}",
        f"груп id OLX, однакових без урахування регістру: "
        f"{rep['olx_case_insensitive_collision_groups']}",
    ]
    if not rep["has_site_key_column"]:
        lines.append("колонки site_key ще немає — ключі обчислено з адрес (`cli.py db migrate`)")
    return "\n".join(lines)


def describe(text_in: str) -> str:
    """`cli.py links parse`: що розпізнано — без query, піддоменів і сирого тексту."""
    r = links.parse(text_in)
    if not r.ok:
        return f"не посилання на оголошення: {r.reason}" + (f" ({r.family})" if r.family else "")
    parts = [f"сімейство: {r.family}", f"id: {r.id}", f"ключ: {r.key or '—'}", f"через: {r.via}"]
    if r.candidates and r.key is None:
        parts.append("варіанти: " + ", ".join(r.candidates))
    if r.case_lost:
        parts.append("регістр id OLX втрачено")
    parts.append(f"канонічна: {r.canonical_url or '—'}")
    parts.append(f"перевірка: {r.fetch_url or '—'}")
    return "\n".join(parts)
