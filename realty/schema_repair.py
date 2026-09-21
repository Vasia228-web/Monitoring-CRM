"""Ремонт зовнішніх ключів, що посилаються на неіснуючі таблиці.

Звідки вони беруться. Стара міграція (`db._drop_obsolete_url_unique`)
перейменовувала `listings` на `listings_legacy`. SQLite при `ALTER TABLE …
RENAME` переписує посилання в ІНШИХ таблицях на нове ім'я — тож
`price_events.listing_id` почав посилатись на `listings_legacy`, яку міграція
потім видалила. Поки перевірку ключів вимкнено, це нікому не заважає; з
увімкненою — кожен запис події ціни падає (20–21.09.2026 так губились усі нові
оголошення й зміни цін).

Ремонт — стандартна процедура SQLite: нова таблиця з правильним описом, копія
всіх рядків, заміна старої, відтворення індексів. Усе в одній транзакції, і
вона НЕ фіксується, якщо хоч щось не збіглося:
  * кількість рядків і вміст КОЖНОЇ таблиці (відбиток усіх рядків) до і після;
  * `integrity_check` = ok;
  * `foreign_key_check` порожній.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Dangling:
    table: str
    column: str
    missing: str
    target: str | None      # на що замінити; None — вгадувати не беремося


@dataclass
class RepairReport:
    dangling: list[Dangling] = field(default_factory=list)
    before: dict = field(default_factory=dict)      # {таблиця: (рядків, відбиток)}
    after: dict = field(default_factory=dict)
    integrity: str = ""
    fk_violations: list = field(default_factory=list)
    applied: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def tables(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]


def find_dangling(con) -> list[Dangling]:
    existing = set(tables(con))
    out = []
    for t in sorted(existing):
        for fk in con.execute(f'PRAGMA foreign_key_list("{t}")'):
            ref, col = fk[2], fk[3]
            if ref in existing:
                continue
            # Відомий випадок: «X_legacy» → «X», якщо X існує. Інших назв не
            # вгадуємо: краще зупинитись, ніж пришити ключ не туди.
            base = ref[: -len("_legacy")] if ref.endswith("_legacy") else None
            out.append(Dangling(t, col, ref, base if base in existing else None))
    return out


def fingerprint(con, table: str) -> tuple[int, str]:
    """Кількість рядків і SHA-256 від УСІХ значень усіх рядків у порядку rowid."""
    h = hashlib.sha256()
    n = 0
    for row in con.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
        h.update(repr(row).encode("utf-8"))
        h.update(b"\n")
        n += 1
    return n, h.hexdigest()


def fingerprints(con) -> dict[str, tuple[int, str]]:
    return {t: fingerprint(con, t) for t in tables(con)}


def repair(path: Path, apply: bool = True) -> RepairReport:
    """Лагодить биті зовнішні ключі. `apply=False` — усе те саме, але відкат."""
    rep = RepairReport()
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("PRAGMA foreign_keys = OFF")     # на час перебудови — як радить SQLite
        rep.dangling = find_dangling(con)
        if not rep.dangling:
            rep.integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            return rep
        unknown = [d for d in rep.dangling if d.target is None]
        if unknown:
            rep.problems.append("невідомо, на що замінити: " + ", ".join(
                f"{d.table}.{d.column}→{d.missing}" for d in unknown))
            return rep

        rep.before = fingerprints(con)
        con.execute("BEGIN IMMEDIATE")
        try:
            for table in sorted({d.table for d in rep.dangling}):
                _rebuild(con, table, [d for d in rep.dangling if d.table == table])
            rep.after = fingerprints(con)
            rep.integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            rep.fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
            left = find_dangling(con)
            if rep.after != rep.before:
                changed = [t for t in rep.before if rep.before[t] != rep.after.get(t)]
                rep.problems.append(f"вміст змінився: {changed}")
            if rep.integrity != "ok":
                rep.problems.append(f"integrity_check: {rep.integrity}")
            if rep.fk_violations:
                rep.problems.append(f"foreign_key_check: {len(rep.fk_violations)} порушень")
            if left:
                rep.problems.append(f"лишились биті ключі: {left}")
        except Exception as e:
            rep.problems.append(f"{type(e).__name__}: {e}")
        if apply and not rep.problems:
            con.execute("COMMIT")
            rep.applied = True
        else:
            con.execute("ROLLBACK")
    finally:
        con.close()
    return rep


def _rebuild(con, table: str, fixes: list[Dangling]) -> None:
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone()[0]
    indexes = [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,))]
    new_ddl = ddl
    for d in fixes:
        new_ddl, n = re.subn(rf'REFERENCES\s+"?{re.escape(d.missing)}"?',
                             f'REFERENCES "{d.target}"', new_ddl)
        if not n:
            raise RuntimeError(f"не знайшов посилання на {d.missing} в описі {table}")
    temp = f"{table}__repair"
    new_ddl = re.sub(rf'^CREATE TABLE\s+"?{re.escape(table)}"?', f'CREATE TABLE "{temp}"',
                     new_ddl, count=1)
    cols = ", ".join(f'"{r[1]}"' for r in con.execute(f'PRAGMA table_info("{table}")'))
    con.execute(new_ddl)
    con.execute(f'INSERT INTO "{temp}" ({cols}) SELECT {cols} FROM "{table}"')
    con.execute(f'DROP TABLE "{table}"')
    # legacy_alter_table=ON: не переписувати посилання в інших таблицях — саме
    # автоматичне переписування й створило цю проблему.
    con.execute("PRAGMA legacy_alter_table = ON")
    con.execute(f'ALTER TABLE "{temp}" RENAME TO "{table}"')
    con.execute("PRAGMA legacy_alter_table = OFF")
    for sql in indexes:
        con.execute(sql)
