"""flombu (flombu.com) — публічний JSON:API списку оголошень.

Фільтрація йде параметрами `filter[...]` — без цього префікса сервер віддає
всю Україну. Але навіть із прямокутником міста flombu повертає всю область,
тому остаточний гео-відбір робимо на своєму боці за координатами й адресою.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

from bs4 import BeautifulSoup

from ..config import BBOX, CITY_UK
from ..fetcher import FetchError
from ..models import Condition, MarketType
from ..normalize import (
    classify_condition, classify_market, in_ivano_frankivsk, parse_area, parse_date, parse_price,
    parse_rooms,
)
from .base import BaseSource

log = logging.getLogger(__name__)

API = "https://flombu.com/uk/estate_deal_sales.json"
ITEM_URL = "https://flombu.com/uk/estate_deal_sales/{}"

# Стрічка flombu не містить опису, тож стан і ринок із неї не видно. Опис є
# лише на HTML-сторінці оголошення — у блоці одразу після заголовка «Опис»
# (у JSON-версії сторінки цього поля немає, перевірено).
_HEADING = re.compile(r"^\s*Опис\s*$")


def parse_detail(html: str) -> dict:
    """Дістає опис зі сторінки оголошення flombu."""
    soup = BeautifulSoup(html, "lxml")
    heading = soup.find(string=_HEADING)
    if heading is None:
        return {}
    body = heading.parent.find_next_sibling()
    if body is None:
        return {}
    description = body.get_text(" ", strip=True)
    if len(description) < 30:
        return {}

    out: dict = {"description": description[:2000]}
    market = classify_market(description)
    if market is not MarketType.UNKNOWN:
        out["market_type"] = market
    condition = classify_condition(description, market=market)
    if condition is not Condition.UNKNOWN:
        out["condition"] = condition
    return out


class FlombuSource(BaseSource):
    name = "flombu"

    def enrich(self, rec: dict, html: str) -> dict:
        """Рівень 2: доповнює запис описом зі сторінки оголошення."""
        found = parse_detail(html)
        if not found:
            return rec
        filled = False
        for field, value in found.items():
            if rec.get(field) in (None, "", MarketType.UNKNOWN, Condition.UNKNOWN):
                rec[field] = value
                filled = True
        if filled:
            rec["detail_enriched"] = True
        return rec

    def _params(self, page: int) -> dict:
        return {
            "filter[south]": BBOX["south"], "filter[north]": BBOX["north"],
            "filter[west]": BBOX["west"], "filter[east]": BBOX["east"],
            "filter[bounds_label]": CITY_UK, "filter[bounds_country]": "ua",
            "filter[deal_type]": "estate_deal_sale",
            "filter[estate_type]": "flat",
            "page": page,
        }

    def iter_listings(self) -> Iterator[dict]:
        for page in range(max(1, self.start_page), self.cfg.max_pages + 1):
            if self.stop_requested:
                break
            self.begin_page(page)
            try:
                data = self.fetcher.get_json(API, self._params(page))
            except FetchError as e:
                log.warning("flombu: сторінка %d не завантажилась: %s", page, e)
                break
            items = data.get("data") or []
            # Гео лежить окремо, в `included`; зв'язуємо за id відношення.
            geo = {
                inc["id"]: inc.get("attributes", {})
                for inc in (data.get("included") or [])
                if inc.get("type") == "estateRecordLocation"
            }
            log.info("flombu: сторінка %d — %d записів (усього стор.: %s)",
                     page, len(items), (data.get("meta") or {}).get("pages"))
            if not items:
                break
            for item in items:
                try:
                    rec = self._parse(item, geo)
                    if rec:
                        yield rec
                except Exception as e:
                    log.debug("flombu: запис %s не розібрався: %s", item.get("id"), e)
            self.end_page()

    def _parse(self, item: dict, geo: dict) -> dict:
        a = item.get("attributes") or {}
        kind = (a.get("type2HumanVal") or "").lower()
        if "квартир" not in kind:  # цікавлять лише квартири
            return {}

        rel = ((item.get("relationships") or {}).get("estateRecordLocation") or {}).get("data") or {}
        g = geo.get(rel.get("id"), {}) if rel else {}
        address = g.get("originalAddress") or a.get("addressToStreet") or ""
        if not in_ivano_frankivsk(address or a.get("addressLocalityHumanVal"),
                                  g.get("latitude"), g.get("longitude")):
            self.stats["skipped_geo"] += 1
            return {}

        price = a.get("price")
        currency = a.get("priceCurrency") or "USD"
        if price is None:
            price, currency = parse_price(a.get("priceHumanVal"))

        accents = " ".join(a.get("tileEstateAccentAttrs") or [])
        title = a.get("title") or ""
        area = parse_area(re.sub(r"<[^>]+>", "", a.get("estateSizeHumanVal") or ""))

        return {
            "external_id": str(item.get("id")),
            "original_url": ITEM_URL.format(item.get("id")),
            "title": title or None,
            "price": price,
            "currency": currency,
            "rooms": parse_rooms(accents) or parse_rooms(title),
            "area_total": area,
            "location": a.get("addressToStreet") or g.get("route") or None,
            "district": g.get("sublocality1") or g.get("locality") or None,
            "published_at": parse_date(a.get("publishedAtHumanVal")),
            "market_type": classify_market(title, accents),
            "condition": classify_condition(title, accents),
            "description": None,
            "raw": {"id": item.get("id"), "ownerType": a.get("ownerType"),
                    "accents": a.get("tileEstateAccentAttrs")},
        }
