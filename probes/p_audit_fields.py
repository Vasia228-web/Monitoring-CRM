"""Блок 2.1: звірка полів у базі з тим, що заявляє сам сайт-джерело.

Звіряємо не з повторним прогоном нашого ж парсера — це було б порівняння
парсера з самим собою. Звіряємо з тим, що сайт публікує як власні дані:
теги DOM.RIA, параметри картки OLX, характеристики rieltor.

Вибірка навмисно з перекосом у дешеві однокімнатні новобудови з ремонтом —
саме там користувач бачить проблему.

Запуск: .venv/bin/python -u probes/p_audit_fields.py [скільки]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from sqlalchemy import select

from realty.db import SessionLocal
from realty.fetcher import BrowserFetcher, Fetcher
from realty.models import Condition, Listing, MarketType
from realty.sources.domria import CARD

OUT = Path(__file__).with_name("_audit_fields.json")

# --- істина від DOM.RIA -------------------------------------------------------

_TAG_RENOVATED = re.compile(r"^з\s|євроремонт|після\s+ремонт|дизайнерськ", re.I)
_TAG_NEEDS = re.compile(r"без\s+ремонт|без\s+оздобл|потребу\w*\s+ремонт|чорнов|"
                        r"стартов|сирец|сирц|під\s+ремонт", re.I)


def domria_truth(card: dict) -> dict:
    tags = [t.get("tag_synonym", "") for t in (card.get("tag_uk") or [])]
    tag_text = " | ".join(tags)
    condition = None
    for tag in tags:
        if _TAG_NEEDS.search(tag):
            condition = Condition.NEEDS_REPAIR
            break
        if "ремонт" in tag.lower() and _TAG_RENOVATED.search(tag):
            condition = Condition.RENOVATED
    prices = card.get("priceArr") or {}
    usd = prices.get("1")
    if isinstance(usd, str):
        usd = float(re.sub(r"[^\d.]", "", usd.replace(" ", "")) or 0) or None
    return {
        "condition": condition.value if condition else None,
        "condition_evidence": tag_text,
        "newbuild_id": card.get("user_newbuild_id") or card.get("newbuild_id"),
        "newbuild_name": card.get("user_newbuild_name_uk"),
        "realty_sale_type": card.get("realty_sale_type"),
        "type": card.get("type"),
        "rooms": card.get("rooms_count"),
        "area": card.get("total_square_meters"),
        "price_usd": usd,
        "price_type": card.get("price_type_uk") or card.get("price_type"),
        "description": (card.get("description_uk") or card.get("description") or "")[:900],
    }


# --- істина від OLX -----------------------------------------------------------

_OLX_ROOMS = re.compile(r"(\d+)\s*кімнат", re.I)
_OLX_AREA = re.compile(r"([\d\s.,]+)\s*м²", re.I)


def olx_truth(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    params = [el.get_text(" ", strip=True) for el in
              soup.select('[data-testid="ad-parameters-container"] p, '
                          '[data-testid="ad-parameters-container"] li, '
                          '[data-testid="ad-parameters-container"] div')]
    params = [p for p in params if p and len(p) < 90]
    joined = " | ".join(dict.fromkeys(params))
    price_el = soup.select_one('[data-testid="ad-price-container"]')
    desc_el = soup.select_one('[data-testid="ad_description"]')
    condition = None
    low = joined.lower()
    if re.search(r"без\s+ремонт|потребує\s+ремонт|чорнов|сирец|під\s+ремонт", low):
        condition = Condition.NEEDS_REPAIR
    elif re.search(r"з\s+ремонтом|євроремонт|дизайнерськ|житлов\w+\s+стан", low):
        condition = Condition.RENOVATED
    market = None
    if re.search(r"первинн\w+\s+ринок|новобудов", low):
        market = MarketType.PRIMARY
    elif re.search(r"вторинн\w+\s+ринок", low):
        market = MarketType.SECONDARY
    rooms = _OLX_ROOMS.search(joined)
    area = _OLX_AREA.search(joined)
    return {
        "condition": condition.value if condition else None,
        "condition_evidence": joined[:260],
        "market": market.value if market else None,
        "rooms": int(rooms.group(1)) if rooms else None,
        "area": float(area.group(1).replace(" ", "").replace(",", ".")) if area else None,
        "price_text": price_el.get_text(" ", strip=True)[:60] if price_el else None,
        "description": (desc_el.get_text(" ", strip=True) if desc_el else "")[:900],
    }


# --- істина від rieltor -------------------------------------------------------

def rieltor_truth(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    low = text.lower()
    condition = None
    if re.search(r"без\s+ремонт|потребує\s+ремонт|чорнов|сирец|під\s+ремонт", low):
        condition = Condition.NEEDS_REPAIR
    elif re.search(r"з\s+ремонтом|євроремонт|дизайнерськ|після\s+ремонт", low):
        condition = Condition.RENOVATED
    rooms = re.search(r"(\d+)[-\s]*кімнатн", low)
    area = re.search(r"(\d+[.,]?\d*)\s*м²", text)
    return {
        "condition": condition.value if condition else None,
        "condition_evidence": text[:260],
        "rooms": int(rooms.group(1)) if rooms else None,
        "area": float(area.group(1).replace(",", ".")) if area else None,
        "description": text[:900],
    }


# --- вибірка ------------------------------------------------------------------

def pick(limit: int) -> list[Listing]:
    """Перекіс у бік того, на що скаржиться користувач, плюс випадковий хвіст."""
    with SessionLocal() as s:
        complained = list(s.scalars(
            select(Listing).where(
                Listing.is_active.is_(True), Listing.quality_status == "ok",
                Listing.rooms == 1, Listing.condition == Condition.RENOVATED,
                Listing.market_type == MarketType.PRIMARY)
            .order_by(Listing.price_usd.asc()).limit(40)).all())
        cheap_primary = list(s.scalars(
            select(Listing).where(
                Listing.is_active.is_(True), Listing.quality_status == "ok",
                Listing.market_type == MarketType.PRIMARY)
            .order_by(Listing.price_usd.asc()).limit(50)).all())
        random_tail = list(s.scalars(
            select(Listing).where(
                Listing.is_active.is_(True), Listing.quality_status == "ok")
            .order_by(Listing.id % 7919).limit(limit)).all())
    seen, out = set(), []
    for row in complained + cheap_primary + random_tail:
        if row.id in seen:
            continue
        seen.add(row.id)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def main() -> None:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    rows = pick(want)
    by_source = Counter(r.source for r in rows)
    print(f"вибірка: {len(rows)} — {dict(by_source)}\n")

    f = Fetcher(delay=1.0, use_cache=False, label="audit")
    browser = None
    results = []
    try:
        for i, row in enumerate(rows, 1):
            rec = {"id": row.id, "source": row.source, "url": row.original_url,
                   "db": {"condition": row.condition.value,
                          "market": row.market_type.value, "rooms": row.rooms,
                          "area": row.area_total, "price_usd": row.price_usd,
                          "llm": bool(row.llm_extracted)},
                   "truth": None, "alive": None, "error": None}
            try:
                if row.source == "domria":
                    rec["alive"] = True
                    rec["truth"] = domria_truth(
                        f.get_json(CARD.format(row.external_id), {"lang_id": 4}))
                elif "olx.ua" in row.original_url:
                    browser = browser or BrowserFetcher(delay=2.2, label="audit")
                    code = browser.probe(row.original_url, delay=2.2)
                    rec["alive"] = code < 400
                    if rec["alive"]:
                        rec["truth"] = olx_truth(
                            browser.render(row.original_url, wait_selector="body"))
                elif "rieltor.ua" in row.original_url:
                    code = f.probe(row.original_url, delay=3.0)
                    rec["alive"] = code < 400
                    if rec["alive"]:
                        rec["truth"] = rieltor_truth(f.get(row.original_url, delay=3.0))
                else:
                    rec["error"] = "джерело не звіряється"
            except Exception as e:                              # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {e}"[:140]
            results.append(rec)
            if i % 10 == 0 or i == len(rows):
                print(f"  {i}/{len(rows)}")
    finally:
        f.close()
        if browser is not None:
            browser.close()

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\nзбережено: {OUT}")


if __name__ == "__main__":
    main()
