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

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import aliased

from ..models import (
    CLEAN_STATUSES, Condition, Listing, MarketType, effective_active, is_clean,
)

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

    Останнім завжди йде `id` — унікальний ключ. Без нього порядок усередині
    групи з однаковою ціною не визначений, а таких груп у базі повно: підряд
    стоять кілька записів по $146 500. База має право віддавати їх щоразу
    по-різному, і тоді при перелистуванні одні з'являються двічі, інші
    зникають. Виглядає це як загадковий баг, а насправді — як відсутність
    другого ключа сортування.
    """
    columns = SORTS.get(sort) or SORTS[DEFAULT_SORT]
    return [c.nullslast() for c in columns] + [Listing.id.desc()]


def _keeper_id():
    """`id` оголошення, яке представляє свій об'єкт у списку.

    Дедуплікація зводить оголошення з різних майданчиків в один об'єкт, але
    список показував по рядку на кожне оголошення. Через це та сама квартира
    стояла у видачі тричі — з OLX, LUN і DOM.RIA, — і виглядало це як провал
    дедуплікації, хоч вона спрацювала.

    Представником беремо найбільший `id` серед склеєних, тобто найсвіжіше з
    побачених оголошень. Умови якості й актуальності повторені всередині
    навмисно: якщо представником вибрати запис, який сам не проходить у
    видачу, об'єкт зник би зі списку цілком.
    """
    other = aliased(Listing)
    return (select(func.max(other.id))
            .where(other.property_id == Listing.property_id,
                   other.quality_status.in_(CLEAN_STATUSES),
                   _active_for(other).is_(True))
            .correlate(Listing)
            .scalar_subquery())


def _active_for(model):
    """Та сама умова актуальності, але для довільного псевдоніма таблиці."""
    return case((model.manual_active.isnot(None), model.manual_active),
                else_=model.is_active)


def listing_query(*, condition: str = "", market: str = "", source: str = "",
                  rooms: str = "", price_min: float | None = None,
                  price_max: float | None = None, sort: str = DEFAULT_SORT,
                  in_progress: bool | None = None,
                  collapse: bool = True) -> Select:
    """Базова вибірка оголошень із застосованими фільтрами й сортуванням."""
    stmt = select(Listing).where(is_clean(), effective_active().is_(True))

    if collapse:
        # Одне оголошення на об'єкт. Умова стоїть у запиті, а не фільтрацією в
        # пам'яті: від неї залежать і лічильник результатів, і номери сторінок.
        stmt = stmt.where(or_(Listing.property_id.is_(None),
                              Listing.id == _keeper_id()))

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
