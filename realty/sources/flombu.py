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
from .. import identity
from ..fetcher import FetchError
from ..models import Condition, MarketType
from ..normalize import (
    classify_condition, classify_market, in_ivano_frankivsk, parse_area, parse_date, parse_price,
    parse_rooms,
)
from ..places import extract as place_extract
from ..seller import evidence as seller_evidence
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


def _seller(attributes: dict) -> dict | None:
    cfg = seller_evidence.config()
    if cfg is None:
        return None
    return seller_evidence.from_feed_item(attributes, cfg.flombu)


def _city_by_coords() -> bool:
    """config/places/rules.toml sources.flombu_city_by_coords (зламаний конфіг — як до E10)."""
    from .. import configfiles

    try:
        return configfiles.load("places/rules").sources.flombu_city_by_coords
    except configfiles.ConfigError as e:
        log.error("config/places/rules.toml не читається — flombu: місто за текстом: %s", e)
        return False


def _village(locality: str | None) -> str | None:
    """Населений пункт, якщо це не саме місто (село громади — як мітка села LUN)."""
    if not locality or not isinstance(locality, str):
        return None
    return None if in_ivano_frankivsk(locality) else locality.strip() or None


def _locality_ok(locality: str) -> bool:
    """Населений пункт flombu — саме місто або назва з довідника районів (район, село
    громади, «поза громадою»). Прямокутник міста ширший за громаду: його східний край
    — за ~1,5 км від центру Тисмениці, північна смуга — села інших громад (рецензія
    E10), тож при явному населеному пункті координат замало."""
    if in_ivano_frankivsk(locality):
        return True
    from ..places import directory

    d = directory.current()
    if d is None:
        return False
    kind, key = d.match_district(locality)
    return kind == "district" or (kind == "ignore" and key == "city")


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

    def parse_page(self, data: dict) -> tuple[list[dict], int, int | None]:
        """Записи однієї сторінки JSON:API, скільки на ній записів і скільки сторінок
        каже сайт (meta.pages) — для нічного проходу стрічки (E11, D60): той самий розбір
        (`_parse`, гео-відбір), що й у збору, без запису."""
        items = data.get("data") or []
        geo = {inc["id"]: inc.get("attributes", {}) for inc in (data.get("included") or [])
               if inc.get("type") == "estateRecordLocation"}
        pages = (data.get("meta") or {}).get("pages")
        recs = []
        for item in items:
            try:
                rec = self._parse(item, geo)
            except Exception as e:                     # noqa: BLE001 — запис, не сторінка
                log.debug("flombu: запис %s не розібрався: %s", item.get("id"), e)
                continue
            if rec:
                recs.append(rec)
        return recs, len(items), pages if isinstance(pages, int) else None

    def iter_listings(self) -> Iterator[dict]:
        total = None                      # meta.pages — скільки сторінок каже сам сайт
        page = None
        for page in range(max(1, self.start_page), self.cfg.max_pages + 1):
            if self.stop_requested:
                break
            self.begin_page(page)
            try:
                data = self.fetcher.get_json(API, self._params(page))
            except FetchError as e:
                self.give_up(f"сторінка {page} не завантажилась", e)
                break
            items = data.get("data") or []
            # Гео лежить окремо, в `included`; зв'язуємо за id відношення.
            geo = {
                inc["id"]: inc.get("attributes", {})
                for inc in (data.get("included") or [])
                if inc.get("type") == "estateRecordLocation"
            }
            pages = (data.get("meta") or {}).get("pages")
            if isinstance(pages, int):
                total = pages
            log.info("flombu: сторінка %d — %d записів (усього стор.: %s)",
                     page, len(items), pages)
            if not items:
                if total is not None and page <= total:
                    self.enum_incomplete(f"порожня сторінка {page} з {total}")
                break
            for item in items:
                try:
                    rec = self._parse(item, geo)
                except Exception as e:
                    log.debug("flombu: запис %s не розібрався: %s", item.get("id"), e)
                    self.enum_incomplete(f"запис {item.get('id')} не розібрався")
                    continue
                if rec:
                    yield rec
            self.end_page()
        else:
            # Стеля сторінок: повний, лише якщо сайт сам сказав, що сторінок не більше.
            if page is not None and (total is None or page < total):
                self.cap_reached(self.cfg.max_pages, "сайт каже сторінок: "
                                 f"{total if total is not None else 'невідомо скільки'}")

    def _parse(self, item: dict, geo: dict) -> dict:
        a = item.get("attributes") or {}
        kind = (a.get("type2HumanVal") or "").lower()
        if "квартир" not in kind:  # цікавлять лише квартири
            return {}

        # Зв'язок з локацією: API віддає relationships['location'] (тип
        # estateRecordLocation), а код до E10 шукав ключ 'estateRecordLocation' — координат
        # і населеного пункту не було в жодному з 27 оголошень (Етап 0, живий JSON:
        # «ключ зв'язку location»). Старий ключ лишається запасним.
        rels = item.get("relationships") or {}
        rel = ((rels.get("location") or rels.get("estateRecordLocation") or {})
               .get("data") or {})
        g = geo.get(rel.get("id"), {}) if isinstance(rel, dict) and rel else {}
        address = g.get("originalAddress") or a.get("addressToStreet") or ""
        text = address or a.get("addressLocalityHumanVal")
        lat, lon = g.get("latitude"), g.get("longitude")
        # Місто чи ні — за координатами (той самий BBOX, що й у LUN; внутрішнє рішення
        # D47 п. 8, D57: збір flombu 27 → ~400 квартир). Без координат — за текстом, як і
        # досі. config/places/rules.toml sources.flombu_city_by_coords = false — як до
        # E10 (текст), а скільки пройшло б за координатами, — у stats.would_add_geo.
        by_coords = getattr(self, "_by_coords", None)
        if by_coords is None:
            by_coords = self._by_coords = _city_by_coords()
        if by_coords:
            inside = in_ivano_frankivsk(text, lat, lon)
            locality = g.get("locality") if isinstance(g.get("locality"), str) else ""
            if inside and locality.strip() and not _locality_ok(locality.strip()):
                # Точка в прямокутнику, але населений пункт — інший (Тисмениця, село
                # іншої громади): не місто.
                self.stats["skipped_locality"] = self.stats.get("skipped_locality", 0) + 1
                return {}
        else:
            inside = in_ivano_frankivsk(text)
            if not inside and lat is not None and in_ivano_frankivsk(text, lat, lon):
                self.stats["would_add_geo"] = self.stats.get("would_add_geo", 0) + 1
        if not inside:
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
            # Район flombu не дає (sublocality1 порожнє в 12 з 12, Етап 0); населений
            # пункт — у place_raw, а в district — лише село (не саме місто: «Івано-
            # Франківськ» районом не є; D57).
            "district": g.get("sublocality1") or _village(g.get("locality")) or None,
            "place_raw": place_extract.from_flombu_location(g) or None,
            # Докази типу продавця (Блок 3, E11, D60): ownerType і комісія агента з білого
            # списку config/seller.toml [flombu]; ownerPhoneId (похідний від телефону) — ні.
            "seller_evidence": _seller(a),
            "published_at": parse_date(a.get("publishedAtHumanVal")),
            "market_type": classify_market(title, accents),
            "condition": classify_condition(title, accents),
            "description": None,
            "identity": identity.from_flombu(a, g),
            "raw": {"id": item.get("id"), "ownerType": a.get("ownerType"),
                    "accents": a.get("tileEstateAccentAttrs")},
        }
