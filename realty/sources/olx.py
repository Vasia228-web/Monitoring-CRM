"""OLX (olx.ua) — рендер сторінки результатів у справжньому Chromium.

HTTP-клієнти OLX не пропускає (CloudFront віддає 403), а звичайний браузер
відкриває публічну сторінку без проблем. Тому це єдине джерело з
`needs_browser=True`.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..fetcher import FetchError
from ..models import Condition, MarketType
from ..normalize import (
    classify_condition, classify_market, in_ivano_frankivsk, parse_area, parse_date, parse_price,
    parse_rooms,
)
from .base import BaseSource

log = logging.getLogger(__name__)

BASE = "https://www.olx.ua"
LIST_URL = BASE + "/uk/nedvizhimost/kvartiry/prodazha-kvartir/ivano-frankovsk/"
# Типова видача OLX перемішана: угорі просувані оголошення. Для щоденного
# прогону це не годиться, тому явно просимо сортування за датою.
RECENT_FIRST = "search%5Border%5D=created_at%3Adesc"
CARD_SEL = '[data-cy="l-card"]'
# Площа стоїть окремим параметром картки у вузлі, текст якого — рівно «85 м²».
# Прив'язуємось до форми тексту, а не до згенерованого класу (css-13vv2xi).
_AREA_NODE = re.compile(r"^\d[\d\s.,]*\s*м²$")


# --- Сторінка деталей ---------------------------------------------------------
# Картка у стрічці не має кімнат приблизно в чверті оголошень, зате сторінка
# оголошення віддає ті самі поля списком «Ключ: Значення». Розбираємо їх
# детерміновано — це дешевше й надійніше за LLM-фолбек.
DETAIL = {
    "params": '[data-testid="ad-parameters-container"]',
    "price": '[data-testid="ad-price-container"]',
    "title": '[data-testid="offer_title"]',
    "description": '[data-testid="ad_description"]',
    "posted": '[data-testid="ad-posted-at"]',
}
_PARAM_RE = re.compile(r"^([^:]{2,40}?)\s*:\s*(.+)$")
# Значення параметра «Ремонт» — повний перелік із фільтра пошуку OLX.
# Це власне поле сайту, тому воно має пріоритет над розбором опису: жодне
# з цих формулювань не збігається з текстовими шаблонами («Житловий стан»,
# «Під чистову обробку» — не «чорнова обробка»).
_REPAIR_BY_PARAM = {
    "авторський проект": Condition.RENOVATED,
    "євроремонт": Condition.RENOVATED,
    "косметичний ремонт": Condition.RENOVATED,
    "житловий стан": Condition.RENOVATED,
    "після будівельників": Condition.NEEDS_REPAIR,
    "під чистову обробку": Condition.NEEDS_REPAIR,
    "аварійний стан": Condition.NEEDS_REPAIR,
}
_MARKET_BY_PARAM = {
    "вторинний ринок": MarketType.SECONDARY,
    "первинний ринок": MarketType.PRIMARY,
    "новобудова": MarketType.PRIMARY,       # саме це значення OLX ставить частіше
    "вторичный рынок": MarketType.SECONDARY,
    "первичный рынок": MarketType.PRIMARY,
    "новостройка": MarketType.PRIMARY,
}


def parse_detail(html: str) -> dict:
    """Витягує поля зі сторінки оголошення OLX. Повертає лише знайдене."""
    soup = BeautifulSoup(html, "lxml")

    params: dict[str, str] = {}
    box = soup.select_one(DETAIL["params"])
    if box:
        for node in box.find_all(["p", "li", "span"]):
            if node.find(["p", "li", "span"]):
                continue  # беремо лише листові вузли, щоб не злипались пари
            m = _PARAM_RE.match(node.get_text(" ", strip=True))
            if m:
                params.setdefault(m.group(1).strip().lower(), m.group(2).strip())

    def text_of(key: str) -> str | None:
        el = soup.select_one(DETAIL[key])
        return el.get_text(" ", strip=True) if el else None

    out: dict = {}
    if v := params.get("кількість кімнат"):
        out["rooms"] = parse_rooms(v)
    if v := params.get("загальна площа"):
        out["area_total"] = parse_area(v)
    for key, field in (("поверх", "floor"), ("поверховість", "floors_total")):
        if (v := params.get(key)) and v.isdigit():
            out[field] = int(v)

    if price_txt := text_of("price"):
        price, currency = parse_price(price_txt)
        if price:
            out["price"], out["currency"] = price, currency
    if posted := text_of("posted"):
        if dt := parse_date(posted.replace("Опубліковано", "").strip()):
            out["published_at"] = dt

    description = text_of("description")
    if description:
        out["description"] = description[:2000]

    if market := _MARKET_BY_PARAM.get((params.get("вид об'єкта") or "").lower()):
        out["market_type"] = market
    else:
        market = classify_market(description, " ".join(params.values()))
        if market is not MarketType.UNKNOWN:
            out["market_type"] = market

    condition = _REPAIR_BY_PARAM.get((params.get("ремонт") or "").lower())
    if condition is None:
        condition = classify_condition(
            description, text_of("title"), " ".join(params.values()),
            market=out.get("market_type"),
        )
    if condition is not Condition.UNKNOWN:
        out["condition"] = condition

    return {k: v for k, v in out.items() if v is not None}


class OlxSource(BaseSource):
    name = "olx"

    def enrich(self, rec: dict, html: str) -> dict:
        """Рівень 2: доповнює запис зі сторінки оголошення."""
        found = parse_detail(html)
        if not found:
            return rec
        filled = []
        for field, value in found.items():
            current = rec.get(field)
            if current in (None, "", MarketType.UNKNOWN, Condition.UNKNOWN):
                rec[field] = value
                filled.append(field)
        if filled:
            rec["detail_enriched"] = True
            log.debug("OLX: зі сторінки деталей додано %s", ", ".join(filled))
        return rec

    def iter_listings(self) -> Iterator[dict]:
        for page in range(max(1, self.start_page), self.cfg.max_pages + 1):
            if self.stop_requested:
                break
            self.begin_page(page)
            url = self._page_url(page)
            try:
                html = self.browser.render(url, wait_selector=CARD_SEL)
            except FetchError as e:
                log.warning("OLX: %s не відрендерилась: %s", url, e)
                break
            cards = BeautifulSoup(html, "lxml").select(CARD_SEL)
            log.info("OLX: %s — %d карток", url, len(cards))
            if not cards:
                break
            for card in cards:
                try:
                    rec = self._parse(card)
                    if rec:
                        yield rec
                except Exception as e:
                    log.debug("OLX: картка не розібралась: %s", e)
            self.end_page()

    def _page_url(self, page: int) -> str:
        params = [RECENT_FIRST] + ([f"page={page}"] if page > 1 else [])
        return f"{LIST_URL}?{'&'.join(params)}"

    @staticmethod
    def _area(card) -> float | None:
        for el in card.find_all(string=True):
            t = " ".join(str(el).split())
            if _AREA_NODE.match(t):
                return parse_area(t)
        return None

    @staticmethod
    def _txt(card, testid: str) -> str | None:
        el = card.select_one(f'[data-testid="{testid}"]')
        return el.get_text(" ", strip=True) if el else None

    def _parse(self, card) -> dict:
        a = card.select_one('a[href]')
        if not a:
            return {}
        href = urljoin(BASE, a["href"].split("?")[0])

        title = self._txt(card, "ad-card-title") or ""
        price_txt = self._txt(card, "ad-price") or ""
        loc_date = self._txt(card, "location-date") or ""
        full = card.get_text(" | ", strip=True)

        if not in_ivano_frankivsk(loc_date or full):
            self.stats["skipped_geo"] += 1
            return {}

        price, currency = parse_price(price_txt)
        area = self._area(card) or parse_area(full)
        rooms = parse_rooms(title) or parse_rooms(full)

        location, _, date_part = loc_date.partition(" - ")
        m = re.search(r"([^,]+?)\s*,?\s*$", location.strip())
        location = m.group(1).strip() if m else location.strip()

        external_id = re.search(r"-ID([A-Za-z0-9]+)\.html", href)
        return {
            "external_id": external_id.group(1) if external_id else href.rsplit("/", 1)[-1],
            "original_url": href,
            "title": title or None,
            "price": price,
            "currency": currency,
            "rooms": rooms,
            "area_total": area,
            "location": location or None,
            "published_at": parse_date(date_part),
            "market_type": classify_market(title, full),
            "condition": classify_condition(title, full),
            "description": None,
            "raw": {"price_text": price_txt, "location_date": loc_date},
        }
