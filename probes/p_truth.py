"""Які поля джерела віддають САМІ — щоб було з чим звіряти.

Перевірка «поле за полем» має спиратись на те, що заявляє сам сайт, а не на
повторний прогін нашого ж парсера: інакше ми звіряли б парсер із самим собою.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from realty.db import SessionLocal
from realty.fetcher import BrowserFetcher, Fetcher
from realty.models import Listing


def sample(source: str) -> Listing:
    with SessionLocal() as s:
        return s.scalars(
            select(Listing).where(Listing.source == source, Listing.is_active.is_(True))
            .order_by(Listing.price_usd.asc()).limit(1)).first()


def main() -> None:
    f = Fetcher(delay=1.2, use_cache=False, label="probe")
    try:
        # --- DOM.RIA: картка з API ---
        row = sample("domria")
        from realty.sources.domria import CARD
        card = f.get_json(CARD.format(row.external_id), {"lang_id": 4})
        print(f"=== DOM.RIA {row.original_url[-40:]}")
        print(f"  у базі: стан={row.condition.value} ринок={row.market_type.value} "
              f"кімн={row.rooms} площа={row.area_total} ціна={row.price_usd}")
        keys = [k for k in card if any(t in k.lower() for t in
                ("state", "wall", "repair", "condition", "type", "rooms", "price",
                 "square", "year", "new", "build"))]
        for k in sorted(keys)[:40]:
            v = card[k]
            if isinstance(v, (str, int, float, bool)) or v is None:
                print(f"    {k} = {str(v)[:70]}")
        print(f"    --- усього полів: {len(card)}")

        # --- rieltor (LUN) ---
        row = sample("lun")
        if "rieltor.ua" in row.original_url:
            html = f.get(row.original_url)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            print(f"\n=== rieltor {row.original_url[-40:]}")
            print(f"  у базі: стан={row.condition.value} ринок={row.market_type.value} "
                  f"кімн={row.rooms} площа={row.area_total} ціна={row.price_usd}")
            text = soup.get_text(" ", strip=True)
            print(f"    текст сторінки, перші 400 символів:\n    {text[:400]}")
    finally:
        f.close()

    # --- OLX: параметри на сторінці ---
    row = sample("olx")
    browser = BrowserFetcher(delay=2.5, label="probe")
    try:
        html = browser.render(row.original_url, wait_selector="body")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        print(f"\n=== OLX {row.original_url[-40:]}")
        print(f"  у базі: стан={row.condition.value} ринок={row.market_type.value} "
              f"кімн={row.rooms} площа={row.area_total} ціна={row.price_usd}")
        params = [li.get_text(" ", strip=True)
                  for li in soup.select('[data-testid="ad-parameters-container"] p, '
                                        '[data-testid="ad-parameters-container"] li')]
        print(f"    параметри: {params[:12]}")
        price = soup.select_one('[data-testid="ad-price-container"]')
        print(f"    ціна на сторінці: {price.get_text(' ', strip=True)[:80] if price else '—'}")
    finally:
        browser.close()


if __name__ == "__main__":
    main()
