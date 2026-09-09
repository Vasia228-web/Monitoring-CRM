"""Спільний шар доступу до списків оголошень.

Усі сторінки будують вибірку через `listing_query`, тому правила, які мають
діяти скрізь, задані рівно в одному місці:

  * показуємо лише записи, що пройшли контроль якості;
  * показуємо лише ті, що ще продаються;
  * типове сортування — ціна за спаданням.

Нова вкладка успадковує це автоматично: їй достатньо викликати ту саму
функцію й, за потреби, додати власну умову через `extra`.
"""
from __future__ import annotations

from sqlalchemy import Select, select

from ..models import Condition, Listing, MarketType, effective_active, is_clean

# Ціна від найбільшої до найменшої — скрізь і завжди.
DEFAULT_SORT = "price_desc"

SORTS: dict[str, tuple] = {
    "price_desc": (Listing.price_usd.desc(),),
    "price_asc": (Listing.price_usd.asc(),),
    "rooms_desc": (Listing.rooms.desc(), Listing.price_usd.desc()),
    "rooms_asc": (Listing.rooms.asc(), Listing.price_usd.desc()),
    "sqm_desc": (Listing.price_per_sqm.desc(), Listing.price_usd.desc()),
    "sqm_asc": (Listing.price_per_sqm.asc(), Listing.price_usd.desc()),
    "date_desc": (Listing.published_at.desc(), Listing.price_usd.desc()),
    "date_asc": (Listing.published_at.asc(), Listing.price_usd.desc()),
}

MAX_ROWS = 1000


def order_clause(sort: str):
    """Порядок сортування з однозначним місцем для порожніх значень.

    Записи без ціни (як і без площі чи дати) завжди йдуть у кінець списку —
    незалежно від напрямку сортування. Інакше при сортуванні за спаданням
    вгорі опинялися б саме ті об'єкти, про які ми знаємо найменше.
    """
    columns = SORTS.get(sort) or SORTS[DEFAULT_SORT]
    return [c.nullslast() for c in columns] + [Listing.id.desc()]


def listing_query(*, condition: str = "", market: str = "", source: str = "",
                  rooms: str = "", price_min: float | None = None,
                  price_max: float | None = None, sort: str = DEFAULT_SORT,
                  in_progress: bool | None = None) -> Select:
    """Базова вибірка оголошень із застосованими фільтрами й сортуванням."""
    stmt = select(Listing).where(is_clean(), effective_active().is_(True))

    if in_progress is True:
        stmt = stmt.where(Listing.in_progress.is_(True))
    elif in_progress is False:
        stmt = stmt.where(Listing.in_progress.is_(False))

    if condition in {c.value for c in Condition}:
        stmt = stmt.where(Listing.condition == Condition(condition))
    if market in {m.value for m in MarketType}:
        stmt = stmt.where(Listing.market_type == MarketType(market))
    if source:
        stmt = stmt.where(Listing.source == source)
    if rooms == "4+":
        stmt = stmt.where(Listing.rooms >= 4)
    elif rooms.isdigit():
        stmt = stmt.where(Listing.rooms == int(rooms))
    if price_min is not None:
        stmt = stmt.where(Listing.price_usd >= price_min)
    if price_max is not None:
        stmt = stmt.where(Listing.price_usd <= price_max)

    return stmt.order_by(*order_clause(sort))
