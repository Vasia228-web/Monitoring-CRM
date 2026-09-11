"""Крок 3: скільки коштує перелічити все опубліковане по кожному джерелу.

Головне питання — чи можна взяти більшу сторінку. Якщо API DOM.RIA приймає
limit=100 замість 20, перелік 8.6 тисяч оголошень коштує 87 запитів замість
433, і різниця снапшотів стає дешевою настільки, що її можна робити щодня.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.config import DOMRIA_CITY_ID, DOMRIA_STATE_ID
from realty.fetcher import Fetcher
from realty.sources.domria import SEARCH

BASE = {"category": 1, "realty_type": 2, "operation_type": 1,
        "state_id": DOMRIA_STATE_ID, "city_id": DOMRIA_CITY_ID}


def main() -> None:
    f = Fetcher(delay=1.2, use_cache=False, label="probe")
    try:
        print("DOM.RIA: скільки ідентифікаторів віддає одна сторінка\n")
        print(f"  {'limit':>6}{'віддано':>10}{'усього в видачі':>18}")
        for limit in (20, 50, 100, 200, 500):
            try:
                data = f.get_json(SEARCH, {**BASE, "page": 0, "limit": limit})
            except Exception as e:                          # noqa: BLE001
                print(f"  {limit:>6}   {type(e).__name__}")
                continue
            items = data.get("items") or []
            print(f"  {limit:>6}{len(items):>10}{data.get('count', '?'):>18}")
            time.sleep(1.0)

        # Чи справді сторінки не перетинаються при більшому limit.
        best = 100
        a = f.get_json(SEARCH, {**BASE, "page": 0, "limit": best}).get("items") or []
        time.sleep(1.0)
        b = f.get_json(SEARCH, {**BASE, "page": 1, "limit": best}).get("items") or []
        print(f"\n  сторінки 0 і 1 при limit={best}: {len(a)} і {len(b)} ід., "
              f"перетин {len(set(a) & set(b))}")
    finally:
        f.close()


if __name__ == "__main__":
    main()
