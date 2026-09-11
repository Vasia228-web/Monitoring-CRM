"""Критерій приймання: перші двадцять результатів зі скарги — поштучно.

Фільтр той самий, з якого почав користувач: однокімнатні, новобудова,
з ремонтом, за зростанням ціни. Для кожного відкриваємо сторінку джерела й
дивимось, чи відповідає воно всім трьом умовам фільтра.
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
from realty.fetcher import BrowserFetcher, Fetcher
from realty.models import Condition, Listing, MarketType
from realty.web.queries import listing_query

OUT = Path(__file__).with_name("_top20.json")
NO_REPAIR = re.compile(r"без\s+ремонт|потребує\s+ремонт|чорнов|сирец|під\s+ремонт|"
                       r"стартов\w+\s+стан", re.I)
HAS_REPAIR = re.compile(r"ремонт:\s*(?:євро|авторськ|дизайнерськ|космет)|"
                        r"з\s+ремонтом|євроремонт|зроблен\w*\s+\w*\s*ремонт|"
                        r"після\s+ремонт|житлов\w+\s+стан", re.I)
UNBUILT = re.compile(r"переуступк|здача\s+|введення\s+в\s+експлуатац\w*[:\s]*20(2[6-9]|[3-9]\d)",
                     re.I)


def main() -> None:
    stmt = listing_query(rooms="1", market="primary", condition="renovated",
                         sort="price_asc")
    with SessionLocal() as s:
        rows = list(s.scalars(stmt.limit(20)).all())
    print(f"перевіряю {len(rows)} записів\n")

    http = Fetcher(delay=1.0, use_cache=False, label="probe")
    browser = None
    out = []
    try:
        for i, row in enumerate(rows, 1):
            rec = {"n": i, "id": row.id, "source": row.source,
                   "price": row.price_usd, "ppsqm": row.price_per_sqm,
                   "url": row.original_url, "verdict": None, "evidence": ""}
            try:
                if "olx.ua" in row.original_url:
                    browser = browser or BrowserFetcher(delay=2.0, label="probe")
                    soup = BeautifulSoup(
                        browser.render(row.original_url, wait_selector="body"), "lxml")
                    params = " | ".join(dict.fromkeys(
                        e.get_text(" ", strip=True) for e in
                        soup.select('[data-testid="ad-parameters-container"] p')))
                    d = soup.select_one('[data-testid="ad_description"]')
                    text = f"{params} {d.get_text(' ', strip=True) if d else ''}"
                elif "dom.ria" in row.original_url:
                    from realty.sources.domria import CARD
                    card = http.get_json(CARD.format(row.external_id), {"lang_id": 4})
                    tags = " | ".join(t.get("tag_synonym", "")
                                      for t in (card.get("tag_uk") or []))
                    text = f"{tags} {card.get('description_uk') or ''}"
                else:
                    text = BeautifulSoup(http.get(row.original_url, delay=3.0),
                                         "lxml").get_text(" ", strip=True)[:2500]
            except Exception as e:                              # noqa: BLE001
                rec["verdict"] = f"не відкрилось: {type(e).__name__}"
                out.append(rec)
                print(f"{i:>3}. {rec['verdict']}  {row.original_url[:70]}")
                continue

            if NO_REPAIR.search(text):
                rec["verdict"] = "НЕ ВІДПОВІДАЄ: сказано «без ремонту»"
            elif UNBUILT.search(text):
                rec["verdict"] = "НЕ ВІДПОВІДАЄ: будинок ще не зданий"
            elif HAS_REPAIR.search(text):
                rec["verdict"] = "відповідає"
            else:
                rec["verdict"] = "ремонт не згаданий прямо"
            m = HAS_REPAIR.search(text) or NO_REPAIR.search(text) or UNBUILT.search(text)
            rec["evidence"] = (m.group(0) if m else text[:70]).replace("\n", " ")
            out.append(rec)
            print(f"{i:>3}. ${row.price_usd:>8,.0f}  {row.source:<7}"
                  f"{rec['verdict']:<38}{rec['evidence'][:40]}")
    finally:
        http.close()
        if browser is not None:
            browser.close()

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    from collections import Counter
    print("\n" + "=" * 60)
    for k, n in Counter(r["verdict"].split(":")[0] for r in out).most_common():
        print(f"  {k:<40} {n}")


if __name__ == "__main__":
    main()
