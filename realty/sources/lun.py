"""LUN (lun.ua) — структуровані дані з RSC-payload Next.js.

Розмітка LUN побудована на хешованих CSS-модулях і легко ламається, тому
беремо не DOM, а вбудований payload `self.__next_f`, де лежать повні об'єкти
оголошень (ціна, кімнати, площа, рік, посилання).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator

from ..config import LUN_CITY_CODE
from .. import identity
from ..fetcher import FetchError
from ..models import Condition, MarketType
from ..normalize import classify_condition, classify_market, in_ivano_frankivsk, parse_date
from ..places import extract as place_extract
from ..seller import evidence as seller_evidence
from . import rieltor
from .base import BaseSource

log = logging.getLogger(__name__)

# Стрічка квартир LUN. Розділ новобудов (/uk/new/if/flats) — це каталог ЖК на
# старому рендері без даних по квартирах, тому первинку сюди не тягнемо:
# її дають DIM.RIA, flombu і blago.
FLATS_URL = f"https://lun.ua/sale/{LUN_CITY_CODE}/flats"
_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')
# Довгі рядки RSC-потік зберігає окремими записами `31:T7e7,<текст>`, а в
# об'єкті лишає посилання `"$31"`. Без розв'язання таких посилань опис
# губиться приблизно в чверті оголошень.
# Записи йдуть впритул один за одним, БЕЗ переносу рядка:
#   `31:T7e7,<2023 байти тексту>32:T807,<...>`
# тому шукати їх треба послідовно, відмірюючи оголошену довжину, а не за
# початком рядка. Довжина — у байтах UTF-8.
_ROW_TEXT_RE = re.compile(rb"([0-9a-f]{1,6}):T([0-9a-f]+),")
_REF_RE = re.compile(r"^\$([0-9a-f]+)$")


def extract_payload(html: str) -> str:
    """Склеює RSC-чанки Next.js в один текст."""
    parts = []
    for raw in _CHUNK_RE.findall(html):
        try:
            parts.append(json.loads('"' + raw + '"'))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def resolve_text_rows(payload: str) -> dict[str, str]:
    """Мапа «id рядка -> текст» для посилань виду `"$31"`."""
    data = payload.encode("utf-8")
    rows: dict[str, str] = {}
    pos = 0
    while (m := _ROW_TEXT_RE.search(data, pos)) is not None:
        length = int(m.group(2), 16)
        start = m.end()
        rows[m.group(1).decode()] = data[start:start + length].decode("utf-8", errors="ignore")
        # Наступний запис починається одразу після тіла цього — продовжуємо
        # звідти, інакше можна натрапити на схожий шаблон усередині тексту.
        pos = start + length
    return rows


def deref(value, rows: dict[str, str]):
    """Замінює посилання `"$31"` на текст рядка, решту повертає як є."""
    if isinstance(value, str) and (m := _REF_RE.match(value)):
        return rows.get(m.group(1), "")
    return value


def iter_json_objects(payload: str, marker: str = '{"id":') -> Iterator[dict]:
    """Знаходить у тексті JSON-об'єкти, що починаються з `marker`."""
    pos = 0
    while (i := payload.find(marker, pos)) != -1:
        depth, end, in_str, esc = 0, None, False, False
        for j in range(i, min(len(payload), i + 200_000)):
            ch = payload[j]
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
                    end = j
                    break
        if end is None:
            pos = i + len(marker)
            continue
        try:
            yield json.loads(payload[i:end + 1])
        except json.JSONDecodeError:
            pass
        pos = end + 1


def page_url(page: int) -> str:
    """Адреса сторінки стрічки (та сама, що в iter_listings)."""
    return FLATS_URL if page == 1 else f"{FLATS_URL}?page={page}"


class LunSource(BaseSource):
    name = "lun"

    def parse_page(self, html: str) -> tuple[list[dict], int]:
        """Записи однієї сторінки стрічки й скільки об'єктів оголошень на ній (−1 —
        порожній payload: розмітка змінилась). Для нічного проходу стрічки (E11, D60):
        той самий розбір, що й у збору (`_parse`, гео-відбір), без запису."""
        payload = extract_payload(html)
        if not payload:
            return [], -1
        rows = resolve_text_rows(payload)
        recs, found = [], 0
        for obj in iter_json_objects(payload):
            if "price" not in obj or "urlRaw" not in obj:
                continue
            found += 1
            if rec := self._parse(obj, rows):
                recs.append(rec)
        return recs, found

    def _geo_types(self) -> tuple:
        """Які geoEntities зберігати в place_raw (config/places/rules.toml
        sources.lun_geo_types); зламаний конфіг — не зберігаємо нічого."""
        cached = getattr(self, "_geo_types_cache", None)
        if cached is None:
            from .. import configfiles

            try:
                cached = tuple(configfiles.load("places/rules").sources.lun_geo_types)
            except configfiles.ConfigError as e:
                log.error("config/places/rules.toml не читається — geoEntities LUN не "
                          "зберігаю: %s", e)
                cached = ()
            self._geo_types_cache = cached
        return cached

    def _seller(self, d: dict, rows: dict[str, str]) -> dict | None:
        cfg = seller_evidence.config()
        if cfg is None:
            return None
        return seller_evidence.from_feed_item(d, cfg.lun, deref=lambda v: deref(v, rows))

    def enrich(self, rec: dict, html: str) -> dict:
        """Рівень 2: добір із картки на сайті-першоджерелі.

        LUN — агрегатор, і його `original_url` веде на сайт, звідки взято
        оголошення. Майже завжди це rieltor.ua, картка якого має пряме поле
        «Загальний стан квартири» — того, чого в payload LUN немає.
        """
        if not rieltor.is_rieltor(rec.get("original_url")):
            return rec
        found = rieltor.parse_detail(html)
        if not found:
            return rec
        filled = False
        for field, value in found.items():
            if field in ("place_raw", "seller_evidence"):
                # Докази місця й продавця — лише нові ключі (як FILL_ONLY_JSON у pipeline).
                rec[field] = seller_evidence.merge_new(rec.get(field), value)
                continue
            if rec.get(field) in (None, "", MarketType.UNKNOWN, Condition.UNKNOWN):
                rec[field] = value
                filled = True
        if filled:
            rec["detail_enriched"] = True
        return rec

    def iter_listings(self) -> Iterator[dict]:
        for page in range(max(1, self.start_page), self.cfg.max_pages + 1):
            if self.stop_requested:
                break
            self.begin_page(page)
            url = FLATS_URL if page == 1 else f"{FLATS_URL}?page={page}"
            try:
                html = self.fetcher.get(url)
            except FetchError as e:
                self.give_up(f"{url} не завантажилась", e)
                break
            payload = extract_payload(html)
            if not payload:
                log.warning("LUN: порожній payload на %s — розмітка могла змінитись", url)
                self.enum_incomplete(f"порожній payload на сторінці {page}")
                break
            rows = resolve_text_rows(payload)
            found = 0
            for obj in iter_json_objects(payload):
                if "price" not in obj or "urlRaw" not in obj:
                    continue
                found += 1
                if rec := self._parse(obj, rows):
                    yield rec
            log.info("LUN: %s — %d об'єктів", url, found)
            self.end_page()
            if not found:
                break
        else:
            # Дійшли до стелі сторінок, а остання ще мала об'єкти: далі не дивились.
            self.enum_incomplete(f"стеля {self.cfg.max_pages} сторінок, а сторінки ще не "
                                 f"скінчились")

    def _parse(self, d: dict, rows: dict[str, str] | None = None) -> dict:
        rows = rows or {}
        geo = deref(d.get("geo"), rows) or deref(d.get("header"), rows) or ""
        loc = d.get("location") or []
        lat, lon = (loc[1], loc[0]) if len(loc) == 2 else (None, None)
        if not in_ivano_frankivsk(geo, lat, lon):
            self.stats["skipped_geo"] += 1
            return {}

        text = deref(d.get("text"), rows) or ""
        market = classify_market(text, built_year=d.get("builtYear"))
        if d.get("withoutRenovation") is True:
            condition = Condition.NEEDS_REPAIR
        else:
            condition = classify_condition(text, deref(d.get("header"), rows), market=market)

        # Адреса без хвоста «..., Івано-Франківськ, Івано-Франківська область».
        short_geo = geo.split(", Івано-Франківськ")[0] if geo else None

        return {
            "external_id": str(d.get("id")),
            "original_url": d.get("urlRaw"),
            "title": deref(d.get("header"), rows) or short_geo,
            "price": d.get("price"),
            "currency": (d.get("currency") or "usd").upper(),
            "price_per_sqm": d.get("priceSqm"),
            "rooms": d.get("roomCount"),
            "area_total": d.get("areaTotal"),
            "location": short_geo,
            "district": (d.get("poi") or {}).get("name"),
            "floor": d.get("floor"),
            "floors_total": d.get("floorCount"),
            "built_year": d.get("builtYear"),
            "published_at": parse_date(d.get("insertTime")),
            "market_type": market,
            "condition": condition,
            "description": text[:2000] or None,
            "identity": identity.from_lun(d),
            # Докази місця (Блок 4, E10, D57): geoEntities — мікрорайон, ЖК (з geoId),
            # село. district лишається poi.name, як і було (сирі поля не змінюємо).
            "place_raw": place_extract.from_lun_item(
                d, self._geo_types(), lambda v: deref(v, rows)) or None,
            # Докази типу продавця (Блок 3, E11, D60): лише булеві й коди з білого
            # списку config/seller.toml [lun] — без телефонів, імен, аватарів.
            "seller_evidence": self._seller(d, rows),
            # Телефони й контакти навмисно не зберігаємо.
            "raw": {
                "id": d.get("id"), "price": d.get("price"), "priceSqm": d.get("priceSqm"),
                "builtYear": d.get("builtYear"), "wallTypeName": d.get("wallTypeName"),
                "withoutRenovation": d.get("withoutRenovation"),
                "site": (d.get("site") or {}).get("displayName"),
            },
        }
