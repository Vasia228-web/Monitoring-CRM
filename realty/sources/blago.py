"""blagodeveloper (blagodeveloper.com) — забудовник, статичний HTML.

Особливість джерела: у картках планувань немає ціни за об'єкт — забудовник
публікує лише «від N грн/м²» на рівні ЖК. Тому загальна ціна рахується як
площа × ставка і позначається прапорцем `price_estimated`.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import urljoin, unquote

from bs4 import BeautifulSoup

from ..fetcher import FetchError
from ..models import Condition, MarketType
from ..normalize import parse_area, parse_price, parse_rooms
from .base import BaseSource

log = logging.getLogger(__name__)

BASE = "https://blagodeveloper.com"
# Перевірено: /plannings/page/N/ віддає той самий набір карток, тобто реальної
# пагінації немає. Тому обходимо тематичні розділи каталогу й дедуплікуємо.
SEED_URLS = (
    BASE + "/plannings/",
    BASE + "/odnokimnatni-kvartyry-v-ivano-frankivsku/",
    BASE + "/dvokimnatni-kvartyry-v-ivano-frankivsku/",
    BASE + "/trykimnatni-kvartyry-v-ivano-frankivsku/",
    BASE + "/kvartyry-gotovi-do-remontu/",
    BASE + "/kvartyry-po-programi-yeoselya/",
)


class BlagoSource(BaseSource):
    name = "blago"

    def iter_listings(self) -> Iterator[dict]:
        seen: set[str] = set()
        rate = None
        for n, url in enumerate(SEED_URLS[: max(1, self.cfg.max_pages)], 1):
            self.begin_page(n)
            try:
                html = self.fetcher.get(url)
            except FetchError as e:
                log.warning("blago: %s не завантажилась: %s", url, e)
                continue
            soup = BeautifulSoup(html, "lxml")
            if rate is None:
                rate = self._price_rate(soup)
            cards = soup.select(".project-card")
            new = 0
            for card in cards:
                rec = self._parse(card, rate)
                if not rec or rec["external_id"] in seen:
                    continue
                seen.add(rec["external_id"])
                new += 1
                yield rec
            log.info("blago: %s — %d планувань (%d нових)", url, len(cards), new)
            self.end_page()

    @staticmethod
    def _price_rate(soup: BeautifulSoup) -> tuple[float | None, str]:
        """Мінімальна ставка грн/м² по ЖК — єдина публічна ціна на сайті."""
        el = soup.select_one(".apartment-card__price")
        if not el:
            return None, "UAH"
        price, currency = parse_price(el.get_text(" ", strip=True))
        return price, currency

    def _parse(self, card, rate: tuple[float | None, str] | None) -> dict:
        text = card.get_text(" | ", strip=True)
        # Пряме посилання ховається в кнопках «поділитися» (основний <a> —
        # захищений Cloudflare редірект на пошту).
        pid, href = None, None
        for a in card.select("a[href]"):
            m = re.search(r"plannings(?:%2F|/)(\d+)", unquote(a["href"]))
            if m:
                pid = m.group(1)
                href = f"{BASE}/plannings/{pid}/"
                break
        if not pid:
            return {}

        area = parse_area(text)
        rooms = parse_rooms(re.sub(r"К-сть кімнат\s*\|\s*(\d)", r"\1 кім", text))
        if rooms is None:
            m = re.search(r"К-сть кімнат\s*\|\s*(\d)", text)
            rooms = int(m.group(1)) if m else None

        title_el = card.select_one(".project-card__title")
        complex_name = title_el.get_text(strip=True) if title_el else None

        price = currency = None
        estimated = False
        if rate and rate[0] and area:
            price, currency = round(rate[0] * area, 2), rate[1]
            estimated = True

        return {
            "external_id": pid,
            "original_url": href,
            "title": f"{rooms}-кім. планування, {complex_name}" if rooms else complex_name,
            "price": price,
            "currency": currency or "UAH",
            "rooms": rooms,
            "area_total": area,
            "location": complex_name and f"ЖК {complex_name}",
            "district": None,
            "published_at": None,
            # Забудовник = завжди первинний ринок; квартири здаються без ремонту.
            "market_type": MarketType.PRIMARY,
            "condition": Condition.NEEDS_REPAIR,
            "description": None,
            "complex_name": complex_name,
            "price_estimated": estimated,
            "raw": {"card_text": text[:300], "rate_uah_per_sqm": rate[0] if rate else None},
        }
