"""Спільний шар доступу до списків оголошень.

Усі сторінки будують вибірку через `listing_query`, тому правила, які мають
діяти скрізь, задані рівно в одному місці:

  * показуємо лише записи, що пройшли контроль якості;
  * показуємо лише ті, що ще продаються;
  * типове сортування — ціна за спаданням.

Нова вкладка успадковує це автоматично: їй достатньо викликати ту саму
функцію й, за потреби, додати власну умову через `extra`.

Список сторінок «/» і «В обробці» (Блок 2, крок E5, D50) читається у ДВІ фази:
`list_ids_select` — упорядковані id усіх рядків фільтра (з покривного індексу
ix_listings_visible, без читання самих рядків), і `page_rows_select` — рядки
лише поточної сторінки за id. Фільтри обох шляхів — одна функція
`_apply_filters`, тож вибірка та сама, що й у `listing_query` (її бере
/api/listings). Нові фільтри Блоків 3/4 додаються в `_apply_filters` і в
navstate.LIST_KEYS — тоді вони діють в обох шляхах і потрапляють у ключ кешу
списку (web/speedcache.list_key).
"""
from __future__ import annotations

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import aliased, defer

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


def _keeper_ids():
    """id представників УСІХ квартир одним запитом (Блок 2, крок E5, D50).

    Те саме правило, що й `_keeper_id` (найбільший id серед чистих і актуальних
    оголошень квартири), але не корельованим підзапитом на кожен рядок, а
    одним GROUP BY: рядок проходить, якщо його id — серед представників. id
    унікальні, тож «id серед представників» ⇔ «id — представник СВОЄЇ
    квартири»; рядки без квартири пропускає окрема умова, як і досі. План —
    один прохід покривного індексу ix_listings_keeper замість підзапиту на
    кожен з ~15 тис. рядків (лічильник зі згортанням 33 мс на M4 → ~2 мс).
    """
    other = aliased(Listing)
    return (select(func.max(other.id))
            .where(other.property_id.isnot(None),
                   other.quality_status.in_(CLEAN_STATUSES),
                   _active_for(other).is_(True))
            .group_by(other.property_id))


def _active_for(model):
    """Та сама умова актуальності, але для довільного псевдоніма таблиці."""
    return case((model.manual_active.isnot(None), model.manual_active),
                else_=model.is_active)


def _apply_filters(stmt, *, condition: str = "", market: str = "", source: str = "",
                   rooms: str = "", price_min: float | None = None,
                   price_max: float | None = None, in_progress: bool | None = None):
    """Фільтри списку — одні для `listing_query` і `list_ids_select`."""
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
    return stmt


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

    stmt = _apply_filters(stmt, condition=condition, market=market, source=source,
                          rooms=rooms, price_min=price_min, price_max=price_max,
                          in_progress=in_progress)
    return stmt.order_by(*order_clause(sort))


def list_ids_select(*, condition: str = "", market: str = "", source: str = "",
                    rooms: str = "", price_min: float | None = None,
                    price_max: float | None = None, sort: str = DEFAULT_SORT,
                    in_progress: bool | None = None, collapse: bool = True) -> Select:
    """Фаза 1 списку: id усіх рядків фільтра в порядку показу.

    Ті самі умови й той самий порядок (з id останнім ключем), що й
    `listing_query`, тож `len(ids)` — це лічильник «за фільтром», а
    `ids[offset:offset + size]` — рівно рядки сторінки. Вибираються лише id:
    усі поля умов і сортувань є в ix_listings_visible, тож запит обходиться
    без читання рядків таблиці. Виконувати через Core (`session.execute`,
    `.scalars()`): розбір 12 тис. id через ORM коштував 35 мс, через Core — ~2 мс
    (замір прототипу Блоку 2, D48).
    """
    stmt = select(Listing.id).where(is_clean(), effective_active().is_(True))
    if collapse:
        stmt = stmt.where(or_(Listing.property_id.is_(None),
                              Listing.id.in_(_keeper_ids())))
    stmt = _apply_filters(stmt, condition=condition, market=market, source=source,
                          rooms=rooms, price_min=price_min, price_max=price_max,
                          in_progress=in_progress)
    return stmt.order_by(*order_clause(sort))


# Великі колонки, яких сторінка списку не показує: опис, сирі дані джерела,
# ознаки ідентичності — не читаються для 50 рядків сторінки.
LIST_DEFERRED = (Listing.description, Listing.raw, Listing.identity)


def page_rows_select(ids) -> Select:
    """Фаза 2: рядки сторінки за id (порядок і повторну перевірку робить `visible_now`).

    Лише за первинним ключем: з умовами якості й актуальності в SQL
    планувальник SQLite обирав індекс ix_listings_visible і проходив усі ~12
    тис. його записів заради 50 рядків (EXPLAIN на копії Етапу 0, D50).
    """
    return (select(Listing).where(Listing.id.in_(list(ids)))
            .options(*(defer(c) for c in LIST_DEFERRED)))


def visible_now(row: Listing) -> bool:
    """Ті самі умови, що й у списку (is_clean() і effective_active()), — для рядка.

    Якщо список id узято з кешу, а рядок уже знято (крок циклу посеред
    покоління), він зникає зі сторінки одразу, а не показується як актуальний.
    На тих самих даних результат той самий, що й у запиті.
    """
    active = row.manual_active if row.manual_active is not None else row.is_active
    return row.quality_status in CLEAN_STATUSES and active is True
