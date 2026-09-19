"""Прибирання слідів, які тести залишили в робочій базі (одноразово).

До 19.09.2026 веб-тести працювали зі справжньою `data/realty.db` і писали в неї:
  * «скарги» — чотири записи на одне оголошення в межах однієї секунди
    (поля ∅ / стан / ціна / знято);
  * «перегляди» карток — а від них залежить черга перевірки актуальності.

Причину закрито (`tests/conftest.py` дає тестам копію бази). Цей скрипт
прибирає вже накопичене.

    python scripts/cleanup_test_artifacts.py            # лише показати
    python scripts/cleanup_test_artifacts.py --apply    # прибрати

Запобіжники:
  * `--apply` не працює без успішного бекапу за останню годину;
  * видаляються лише скарги з відбитком тесту; поодинокі — лишаються;
  * перегляди скидаються лише тим оголошенням, чий час перегляду збігається
    з прогоном тестів (± 3 хв, з поправкою на місцевий час);
  * оголошення, ціни та їхня історія не зачіпаються взагалі.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.config import DB_URL  # noqa: E402

TEST_FIELDS = {"", "condition", "price", "gone"}
MIN_GROUP = 3           # менше — схоже на людину, лишаємо
WINDOW = timedelta(minutes=3)
FRESH_BACKUP_HOURS = 1


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def find_test_reports(con) -> tuple[list[int], list[int]]:
    groups: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    for row in con.execute("SELECT id, listing_id, created_at, field FROM data_reports"):
        groups[(row["listing_id"], row["created_at"][:19])].append(row)
    test, kept = [], []
    for rows in groups.values():
        fields = {r["field"] or "" for r in rows}
        if len(rows) >= MIN_GROUP and fields <= TEST_FIELDS:
            test += [r["id"] for r in rows]
        else:
            kept += [r["id"] for r in rows]
    return sorted(test), sorted(kept)


def find_test_views(con, stamps: list[datetime]) -> list[sqlite3.Row]:
    """Оголошення, чий час перегляду припадає на прогін тестів.

    `viewed_at` пишеться місцевим часом, а скарги — UTC, тому звіряємось і з
    самим часом, і зі зсувом на місцевий пояс.
    """
    offset = datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)
    windows = [s for s in stamps] + [s + offset for s in stamps]
    out = []
    for row in con.execute("SELECT id, views, viewed_at FROM listings WHERE views > 0"):
        if not row["viewed_at"]:
            continue
        seen = _parse(row["viewed_at"])
        if any(abs(seen - w) <= WINDOW for w in windows):
            out.append(row)
    return out


def fresh_backup_exists(hours: int = FRESH_BACKUP_HOURS) -> tuple[bool, str]:
    from realty import backup

    last = backup.last_success_at()
    if last is None:
        return False, "успішного бекапу ще не було"
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - last).total_seconds() / 3600
    return age <= hours, f"останній успішний бекап {age:.1f} год тому"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="справді прибрати")
    args = ap.parse_args()

    path = DB_URL.removeprefix("sqlite:///")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row

    test_reports, kept_reports = find_test_reports(con)
    stamps = [_parse(r["created_at"]) for r in con.execute(
        "SELECT created_at FROM data_reports WHERE id IN (%s)"
        % ",".join("?" * len(test_reports)), test_reports)] if test_reports else []
    views = find_test_views(con, stamps)

    print(f"база: {path}")
    print(f"скарги:    {len(test_reports)} від тестів, {len(kept_reports)} лишається")
    print(f"перегляди: {len(views)} оголошень, {sum(r['views'] for r in views)} переглядів "
          f"скидається в нуль")
    total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"оголошення й історія цін: не зачіпаються ({total} записів)")

    if not args.apply:
        print("\nце лише показ; щоб прибрати — --apply")
        return 0

    ok, why = fresh_backup_exists()
    if not ok:
        print(f"\nВІДМОВА: {why}. Спершу `python cli.py backup`.")
        return 1
    print(f"\n{why} — продовжую")

    with con:
        con.execute("DELETE FROM data_reports WHERE id IN (%s)"
                    % ",".join("?" * len(test_reports)), test_reports)
        con.executemany("UPDATE listings SET views = 0, viewed_at = NULL WHERE id = ?",
                        [(r["id"],) for r in views])
    print(f"прибрано скарг: {len(test_reports)}; скинуто переглядів у {len(views)} оголошень")
    print(f"лишилось скарг: {con.execute('SELECT COUNT(*) FROM data_reports').fetchone()[0]}")
    print(f"лишилось оголошень із переглядами: "
          f"{con.execute('SELECT COUNT(*) FROM listings WHERE views > 0').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
