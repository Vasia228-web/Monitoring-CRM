"""Що насправді каже картка про тип угоди й чи буває ціна «від».

Два питання з ТЗ, які закриваються однією вибіркою:

  * «новобудова» в тексті не завжди означає первинний ринок — квартиру в
    новому будинку часто перепродують. Що саме OLX пише в полі «Тип угоди» і
    як це співвідноситься з тим, що ми проставили;
  * забудовники публікують ціну «від» — найдешевшу квартиру в будинку, а не ту,
    що в оголошенні. Чи трапляється таке в тексті ціни.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from sqlalchemy import select

from realty.db import SessionLocal
from realty.fetcher import BrowserFetcher
from realty.models import Listing

N = 70
OUT = Path(__file__).with_name("_deal_type.json")
FROM_PRICE = re.compile(r"\bвід\b|\bот\b|\bfrom\b", re.I)


def main() -> None:
    with SessionLocal() as s:
        rows = list(s.scalars(
            select(Listing).where(
                Listing.source == "olx", Listing.is_active.is_(True))
            .order_by(Listing.id % 3571).limit(N)).all())

    browser = BrowserFetcher(delay=2.0, label="probe")
    out = []
    try:
        for i, row in enumerate(rows, 1):
            rec = {"id": row.id, "url": row.original_url,
                   "db_market": row.market_type.value,
                   "db_condition": row.condition.value,
                   "deal": None, "year": None, "price_text": None, "error": None}
            try:
                soup = BeautifulSoup(
                    browser.render(row.original_url, wait_selector="body"), "lxml")
                params = {}
                for el in soup.select('[data-testid="ad-parameters-container"] p'):
                    text = el.get_text(" ", strip=True)
                    if ":" in text:
                        k, _, v = text.partition(":")
                        params[k.strip().lower()] = v.strip()
                rec["deal"] = params.get("тип угоди")
                rec["year"] = params.get("рік введення в експлуатацію")
                rec["object"] = params.get("вид об'єкта")
                el = soup.select_one('[data-testid="ad-price-container"]')
                rec["price_text"] = el.get_text(" ", strip=True)[:60] if el else None
            except Exception as e:                              # noqa: BLE001
                rec["error"] = type(e).__name__
            out.append(rec)
            if i % 10 == 0:
                print(f"  {i}/{len(rows)}")
    finally:
        browser.close()

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    ok = [r for r in out if not r["error"]]

    print(f"\nзвірено {len(ok)} із {len(out)}\n")
    print("ТИП УГОДИ × що в нас проставлено:")
    pairs = Counter((r.get("deal") or "—", r["db_market"]) for r in ok)
    for (deal, market), n in pairs.most_common(14):
        print(f"  {deal:<26} → {market:<10} {n}")

    print("\nВИД ОБ'ЄКТА × тип угоди:")
    for (obj, deal), n in Counter(
            (r.get("object") or "—", r.get("deal") or "—") for r in ok).most_common(10):
        print(f"  {obj:<14} {deal:<26} {n}")

    froms = [r for r in ok if r["price_text"] and FROM_PRICE.search(r["price_text"])]
    print(f"\nЦІНА «ВІД» у тексті ціни: {len(froms)} із {len(ok)}")
    for r in froms[:5]:
        print(f"    {r['price_text']}")

    years = [r for r in ok if r.get("year")]
    print(f"\nРІК ВВЕДЕННЯ вказано: {len(years)} із {len(ok)}")
    print("  значення:", Counter(re.sub(r"\D", "", r["year"]) for r in years).most_common(8))


if __name__ == "__main__":
    main()
