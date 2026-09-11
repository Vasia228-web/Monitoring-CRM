"""Чому найдешевші «однокімнатні новобудови з ремонтом» позначені як з ремонтом.

Беремо саме ту вибірку, з якої почав користувач, і для кожного запису
показуємо: що в базі, яка ціна на сторінці, повний опис і — головне — який
саме шаблон класифікатора спрацював.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from sqlalchemy import select

from realty.db import SessionLocal
from realty.fetcher import BrowserFetcher, Fetcher
from realty.models import Condition, Listing, MarketType
from realty.normalize import _HAS_REPAIR, _NO_REPAIR, _PRIMARY, _SECONDARY

N = 12


def which(pattern, text: str) -> str:
    m = pattern.search(text or "")
    if not m:
        return "—"
    start = max(0, m.start() - 45)
    return f"«{m.group(0)}»   …{text[start:m.end() + 45]}…".replace("\n", " ")


def main() -> None:
    with SessionLocal() as s:
        rows = list(s.scalars(
            select(Listing).where(
                Listing.is_active.is_(True), Listing.quality_status == "ok",
                Listing.rooms == 1, Listing.condition == Condition.RENOVATED,
                Listing.market_type == MarketType.PRIMARY)
            .order_by(Listing.price_usd.asc()).limit(N)).all())

    f = Fetcher(delay=1.0, use_cache=False, label="probe")
    browser = None
    try:
        for i, row in enumerate(rows, 1):
            print("=" * 76)
            print(f"{i}. ${row.price_usd:,.0f}  ({row.price} {row.currency})  "
                  f"{row.area_total} м²  →  ${row.price_per_sqm:,.0f}/м²   [{row.source}]")
            print(f"   {row.original_url}")
            print(f"   llm={row.llm_extracted}  деталі={row.detail_enriched}  "
                  f"оцінка_ціни={row.price_estimated}")

            page_price, params, desc = None, "", ""
            try:
                if "olx.ua" in row.original_url:
                    browser = browser or BrowserFetcher(delay=2.2, label="probe")
                    html = browser.render(row.original_url, wait_selector="body")
                    soup = BeautifulSoup(html, "lxml")
                    el = soup.select_one('[data-testid="ad-price-container"]')
                    page_price = el.get_text(" ", strip=True) if el else None
                    d = soup.select_one('[data-testid="ad_description"]')
                    desc = d.get_text(" ", strip=True) if d else ""
                    params = " | ".join(dict.fromkeys(
                        e.get_text(" ", strip=True) for e in
                        soup.select('[data-testid="ad-parameters-container"] p')))
                elif "dom.ria" in row.original_url:
                    from realty.sources.domria import CARD
                    card = f.get_json(CARD.format(row.external_id), {"lang_id": 4})
                    page_price = f"{(card.get('priceArr') or {}).get('1')} $"
                    desc = card.get("description_uk") or card.get("description") or ""
                    params = " | ".join(t.get("tag_synonym", "")
                                        for t in (card.get("tag_uk") or []))
                else:
                    html = f.get(row.original_url, delay=3.0)
                    soup = BeautifulSoup(html, "lxml")
                    desc = soup.get_text(" ", strip=True)[:1500]
            except Exception as e:                              # noqa: BLE001
                print(f"   !! {type(e).__name__}: {e}"[:120])

            print(f"   ціна на сторінці: {page_price}")
            if params:
                print(f"   параметри: {params[:180]}")
            haystack = f"{row.title or ''} {params} {desc}"
            print(f"   СПРАЦЮВАВ «є ремонт»: {which(_HAS_REPAIR, haystack)[:190]}")
            print(f"   шаблон «без ремонту»:  {which(_NO_REPAIR, haystack)[:190]}")
            print(f"   шаблон «новобудова»:   {which(_PRIMARY, haystack)[:150]}")
            print(f"   опис: {desc[:260]}")
            print()
    finally:
        f.close()
        if browser is not None:
            browser.close()


if __name__ == "__main__":
    main()
