"""DIM.RIA (dom.ria.com) — JSON-ендпоінти пошуку та картки об'єкта.

Перевірено на етапі верифікації:
  пошук  GET /node/searchEngine/v2/  -> {"count": N, "items": [realty_id, ...]}
  картка GET /realty/data/{id}?lang_id=4 -> повний JSON оголошення
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator

from ..config import DOMRIA_CITY_ID, DOMRIA_STATE_ID
from ..fetcher import FetchError
from ..models import Condition, MarketType
from ..normalize import classify_condition, classify_market, parse_date
from .base import BaseSource

log = logging.getLogger(__name__)

SEARCH = "https://dom.ria.com/node/searchEngine/v2/"
CARD = "https://dom.ria.com/realty/data/{}"
PAGE_SIZE = 20

# Примітка: поле `realty_sale_type` перевірялося на вибірці — воно НЕ розділяє
# первинку й вторинку однозначно, тому ринок визначаємо за тегами й описом.

# RIA дублює ціну в трьох валютах: ключ 1 — USD, 2 — EUR, 3 — UAH.
_CURRENCY_BY_ID = {1: "USD", 2: "EUR", 3: "UAH"}
_USD_KEY = "1"

# --- Сторінка оголошення ------------------------------------------------------
# JSON-ендпоінт віддає `characteristics_values` порожнім, а HTML-сторінка несе
# ті самі характеристики вже з людськими назвами — у `window.__INITIAL_STATE__`.
# Саме там лежить пряме поле «стан квартири», якого в API немає.
_STATE_MARKER = "__INITIAL_STATE__"
_CONDITION_LABEL = "стан квартири:"
# Значення поля «стан квартири», перевірені на вибірці сторінок.
_CONDITION_MAP = {
    "дизайнерський ремонт": Condition.RENOVATED,
    "євроремонт": Condition.RENOVATED,
    "косметичний ремонт": Condition.RENOVATED,
    "хороший": Condition.RENOVATED,
    "задовільний": Condition.RENOVATED,
    "потребує ремонту/ без ремонту / ремонт не завершений": Condition.NEEDS_REPAIR,
    "аварійний": Condition.NEEDS_REPAIR,
}
_MARKET_MAP = {
    "вторинна нерухомість": MarketType.SECONDARY,
    "первинна нерухомість": MarketType.PRIMARY,
    "новобудова": MarketType.PRIMARY,
}
# Мітку ринку RIA ставить не завжди, але рік будівництва й серію будинку —
# майже завжди; для решти оголошень ринок визначаємо з них.
_BUILT_RE = re.compile(r"Побудовано\s*(\d{4})", re.I)
_SERIES_LABEL = "тип будинку (серія):"


def extract_state(html: str) -> dict:
    """Дістає `window.__INITIAL_STATE__` зі сторінки оголошення."""
    i = html.find(_STATE_MARKER)
    if i < 0:
        return {}
    start = html.find("{", i)
    if start < 0:
        return {}
    depth, in_str, esc = 0, False, False
    for j in range(start, len(html)):
        ch = html[j]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:j + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def parse_detail(html: str) -> dict:
    """Витягує стан і ринок зі сторінки оголошення DIM.RIA."""
    realty = ((extract_state(html).get("listing") or {}).get("data") or {}).get("realty") or {}
    if not realty:
        return {}

    out: dict = {}

    # Стан квартири — окремий підпис у групі «Характеристика приміщення».
    # Групу перевіряємо навмисно: сусідня «Зовнішня обробка: без оздоблення»
    # стосується фасаду, і без розрізнення груп зіпсувала б класифікацію.
    for group in realty.get("secondaryParams") or []:
        for item in group.get("items") or []:
            label = (item.get("label") or "").strip()
            if not label.lower().startswith(_CONDITION_LABEL):
                continue
            value = label.split(":", 1)[1].strip().lower()
            out["condition"] = _CONDITION_MAP.get(value) or classify_condition(value)
            break

    # Тип нерухомості лежить окремим записом mainCharacteristics без charId,
    # а поруч — рік будівництва («Побудовано 2025»).
    for char in (realty.get("mainCharacteristics") or {}).get("chars") or []:
        value = char.get("value")
        if not isinstance(value, str):
            continue
        if market := _MARKET_MAP.get(value.strip().lower()):
            out["market_type"] = market
        elif m := _BUILT_RE.search(value):
            out["built_year"] = int(m.group(1))

    if "market_type" not in out:
        series = next(
            (i.get("label", "").split(":", 1)[1].strip()
             for g in realty.get("secondaryParams") or []
             for i in g.get("items") or []
             if (i.get("label") or "").lower().startswith(_SERIES_LABEL)),
            "",
        )
        market = classify_market(series, built_year=out.get("built_year"))
        if market is not MarketType.UNKNOWN:
            out["market_type"] = market

    if (desc := realty.get("description_uk") or realty.get("description")):
        out["description"] = desc[:2000]

    return {k: v for k, v in out.items()
            if v not in (None, Condition.UNKNOWN, MarketType.UNKNOWN)}


class DomRiaSource(BaseSource):
    name = "domria"

    def enrich(self, rec: dict, html: str) -> dict:
        """Рівень 2: стан і ринок зі сторінки оголошення.

        Це єдине джерело, де рівень 2 ходить не по нові поля, а по ті самі —
        просто API їх не віддає, а HTML-сторінка віддає.
        """
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

    def _search_page(self, page: int) -> list[int]:
        data = self.fetcher.get_json(SEARCH, {
            "category": 1,            # житло
            "realty_type": 2,         # квартира
            "operation_type": 1,      # продаж
            "state_id": DOMRIA_STATE_ID,
            "city_id": DOMRIA_CITY_ID,
            "page": page,
            "limit": PAGE_SIZE,
        })
        return list(data.get("items") or [])

    def _card(self, realty_id: int) -> dict:
        return self.fetcher.get_json(CARD.format(realty_id), {"lang_id": 4})

    def iter_listings(self) -> Iterator[dict]:
        for page in range(self.start_page, self.cfg.max_pages):
            if self.stop_requested:
                break
            self.begin_page(page)
            try:
                ids = self._search_page(page)
            except FetchError as e:
                log.warning("DIM.RIA: сторінка %d не завантажилась: %s", page, e)
                break
            if not ids:
                break
            log.info("DIM.RIA: сторінка %d, %d оголошень", page, len(ids))
            for rid in ids:
                try:
                    yield self._parse(self._card(rid))
                except FetchError as e:
                    log.debug("DIM.RIA: картка %s недоступна: %s", rid, e)
                except Exception as e:
                    log.warning("DIM.RIA: картка %s не розібралась: %s", rid, e)
            self.end_page()

    def _parse(self, d: dict) -> dict:
        # Гео-фільтр: покладаємось на city_id самого джерела — він точний.
        if d.get("city_id") != DOMRIA_CITY_ID:
            self.stats["skipped_geo"] += 1
            return {}

        desc = d.get("description_uk") or d.get("description") or ""
        tags = " ".join(t.get("tag_synonym", "") for t in (d.get("tag_uk") or []))
        utp = " ".join(d.get("secondaryUtp") or [])
        complex_name = d.get("user_newbuild_name_uk") or d.get("user_newbuild_name")

        street = d.get("street_name_uk") or d.get("street_name") or ""
        house = d.get("building_number_str") or ""
        district = d.get("district_name_uk") or d.get("district_name")
        location = ", ".join(p for p in (street, house) if p) or district

        market = classify_market(desc, tags, utp, complex_name=complex_name)
        if d.get("is_developer") or d.get("type") == "nb":
            market = MarketType.PRIMARY

        currency = _CURRENCY_BY_ID.get(d.get("currency_type_id"), "USD")
        # Увага: `price_item` — це ціна за м² у валюті оголошення, а не в USD.
        # Доларову шкалу беремо з `priceItemArr["1"]`, інакше гривневі
        # оголошення дають абсурдні «$/м²».
        usd_per_sqm = (d.get("priceItemArr") or {}).get(_USD_KEY)
        if not usd_per_sqm and currency == "USD":
            usd_per_sqm = d.get("price_item")

        return {
            "external_id": str(d.get("realty_id")),
            "original_url": "https://dom.ria.com/uk/" + str(d.get("beautiful_url", "")),
            "title": d.get("advert_title") or f"{d.get('rooms_count')}-кім. квартира, {street}",
            "price": d.get("price_total") or d.get("price"),
            "currency": currency,
            "price_per_sqm": usd_per_sqm,
            "rooms": d.get("rooms_count"),
            "area_total": d.get("total_square_meters"),
            "location": location,
            "district": district,
            "floor": d.get("floor"),
            "floors_total": d.get("floors_count"),
            "published_at": parse_date(d.get("publishing_date")),
            "market_type": market,
            "condition": classify_condition(tags, desc, utp, market=market),
            "description": desc[:2000] or None,
            "complex_name": complex_name,
            "raw": {k: d.get(k) for k in (
                "realty_id", "price", "currency_type", "rooms_count", "total_square_meters",
                "realty_sale_type", "type", "latitude", "longitude", "publishing_date",
                "price_item", "priceItemArr",
            )},
        }
