"""Наскільки ціна OLX у базі розходиться з ціною на сторінці оголошення.

Картка у видачі показує гривневий еквівалент (перерахований самим OLX), а
сторінка оголошення — ціну, яку виставив продавець, здебільшого в доларах.
Ми беремо гривні з картки й конвертуємо своїм курсом — тобто повертаємо назад
через другий курс. Питання: наскільки це розходиться з оригіналом.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from sqlalchemy import select

from realty.db import SessionLocal
from realty.fetcher import BrowserFetcher
from realty.models import Listing
from realty.normalize import parse_price, to_usd

N = 60
OUT = Path(__file__).with_name("_olx_price.json")


def main() -> None:
    with SessionLocal() as s:
        rows = list(s.scalars(
            select(Listing).where(
                Listing.source == "olx", Listing.is_active.is_(True),
                Listing.quality_status == "ok", Listing.price_usd.isnot(None))
            .order_by(Listing.id % 4093).limit(N)).all())

    browser = BrowserFetcher(delay=2.0, label="probe")
    out = []
    try:
        for i, row in enumerate(rows, 1):
            rec = {"id": row.id, "url": row.original_url,
                   "db_price": row.price, "db_currency": row.currency,
                   "db_usd": row.price_usd, "page_price": None,
                   "page_currency": None, "page_usd": None, "error": None}
            try:
                html = browser.render(row.original_url, wait_selector="body")
                el = BeautifulSoup(html, "lxml").select_one(
                    '[data-testid="ad-price-container"]')
                text = el.get_text(" ", strip=True) if el else ""
                rec["page_text"] = text[:70]
                price, cur = parse_price(text)
                rec["page_price"], rec["page_currency"] = price, cur
                rec["page_usd"] = to_usd(price, cur)
            except Exception as e:                              # noqa: BLE001
                rec["error"] = f"{type(e).__name__}"[:60]
            out.append(rec)
            if i % 10 == 0:
                print(f"  {i}/{len(rows)}")
    finally:
        browser.close()

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    ok = [r for r in out if r["page_usd"] and r["db_usd"]]
    diffs = [(r["db_usd"] / r["page_usd"] - 1) * 100 for r in ok]
    print(f"\nзвірено: {len(ok)} із {len(out)}")
    if not diffs:
        return
    diffs.sort()
    import statistics as st
    print(f"  медіана відхилення: {st.median(diffs):+.2f}%")
    print(f"  у межах ±1%:        {sum(1 for d in diffs if abs(d) <= 1)}")
    print(f"  відхилення >5%:     {sum(1 for d in diffs if abs(d) > 5)}")
    print(f"  відхилення >10%:    {sum(1 for d in diffs if abs(d) > 10)}")
    print(f"  найгірші: {[round(d, 1) for d in diffs[:3]]} … "
          f"{[round(d, 1) for d in diffs[-3:]]}")
    print(f"\n  валюта на сторінці: "
          f"{ {c: sum(1 for r in ok if r['page_currency'] == c) for c in {r['page_currency'] for r in ok}} }")
    print("\n  найбільші розходження:")
    worst = sorted(ok, key=lambda r: -abs(r["db_usd"] / r["page_usd"] - 1))[:6]
    for r in worst:
        d = (r["db_usd"] / r["page_usd"] - 1) * 100
        print(f"    {d:+7.1f}%  база=${r['db_usd']:>9,.0f}  сторінка={r['page_text']}")


if __name__ == "__main__":
    main()
