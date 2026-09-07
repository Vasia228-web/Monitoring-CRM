"""Розбір картки оголошення на rieltor.ua.

Це не окреме джерело: власної стрічки ми тут не читаємо. LUN агрегує
оголошення з інших сайтів, і його `original_url` майже завжди веде саме сюди
(143 зі 144 зібраних оголошень). Картка rieltor.ua містить те, чого немає в
payload LUN, — зокрема пряме поле «Загальний стан квартири».
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..models import Condition, MarketType
from ..normalize import classify_condition, classify_market, parse_area, parse_rooms

log = logging.getLogger(__name__)

HOST = "rieltor.ua"

# Класи на rieltor.ua семантичні (не згенеровані складальником), тож на них
# спиратись безпечніше, ніж на структуру дерева.
SEL = {
    "params": ".offer-view-planning-text",   # пари «Ключ: Значення»
    "details": ".offer-view-details-row",    # кімнати, площі, поверх, тип, рік
    "description": ".offer-view-section-text",
}

# Значення поля «Загальний стан квартири». «Частковий ремонт» свідомо не
# мапиться: у схемі стан бінарний, а частковий ремонт не є ні готовністю до
# проживання, ні сирцем — краще лишити `unknown`, ніж вгадувати.
_STATE_MAP = {
    "з ремонтом": Condition.RENOVATED,
    "євроремонт": Condition.RENOVATED,
    "дизайнерський ремонт": Condition.RENOVATED,
    "після ремонту": Condition.RENOVATED,
    "без ремонту": Condition.NEEDS_REPAIR,
    "сирець": Condition.NEEDS_REPAIR,
    "чорнові роботи": Condition.NEEDS_REPAIR,
}
_YEAR_RE = re.compile(r"(\d{4})\s*рік\s*побудови", re.I)


def is_rieltor(url: str | None) -> bool:
    return bool(url) and HOST in url


def parse_detail(html: str) -> dict:
    """Витягує поля з картки rieltor.ua. Повертає лише знайдене."""
    soup = BeautifulSoup(html, "lxml")

    params: dict[str, str] = {}
    for el in soup.select(SEL["params"]):
        text = el.get_text(" | ", strip=True)
        key, sep, value = text.partition("|")
        if sep and value.strip():
            params.setdefault(key.strip().rstrip(":").lower(), value.strip())

    details = [el.get_text(" ", strip=True) for el in soup.select(SEL["details"])]
    details_text = " ".join(details)

    description = " ".join(
        el.get_text(" ", strip=True) for el in soup.select(SEL["description"])
    ).strip()

    out: dict = {}
    if description:
        out["description"] = description[:2000]
    if v := params.get("кількість кімнат"):
        out["rooms"] = parse_rooms(v) or (int(v) if v.isdigit() else None)
    if v := params.get("загальна площа"):
        out["area_total"] = parse_area(v)
    if m := _YEAR_RE.search(details_text):
        out["built_year"] = int(m.group(1))

    market = classify_market(details_text, description, built_year=out.get("built_year"))
    if market is not MarketType.UNKNOWN:
        out["market_type"] = market

    # Пряме поле сайту точніше за здогад із тексту, тому має пріоритет.
    state = (params.get("загальний стан квартири") or "").lower()
    condition = _STATE_MAP.get(state)
    if condition is None:
        condition = classify_condition(state, description, details_text, market=market)
    if condition is not Condition.UNKNOWN:
        out["condition"] = condition

    return {k: v for k, v in out.items() if v is not None}
