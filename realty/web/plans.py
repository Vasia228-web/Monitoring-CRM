"""Плани запитів сайту (EXPLAIN QUERY PLAN) — охоронець індексів Блоку 2 (крок E4, D50).

Навіщо. Швидкість списку тримається на тому, що SQLite бере індекси
ix_listings_visible / ix_listings_keeper / ix_listings_in_progress, а не
проходить усю 94-МБ таблицю (Етап 0: сім повних проходів на кожен перегляд «/»).
Вибір плану залежить від версії й збірки SQLite (Mac 3.51 ≠ Fedora), від нових
фільтрів (Блоки 3/4) і нових індексів — і може змінитись непомітно, бо вивід
лишається тим самим. Тому плани перевіряються машиною:
  * `cli.py db plans` — на робочій базі будь-якої машини (лише читання);
    `--preview` — на тимчасовій копії з індексами міграції (до `db migrate`);
  * tests/test_list_plans.py — на синтетичній базі в кожному прогоні тестів.

«Погане»:
  * для запиту id списку — будь-яке звернення до listings НЕ через покривний
    індекс: навіть «SEARCH … USING INDEX ix_listings_property_id (property_id>?)»
    на ділі читає майже всі рядки таблиці (так виглядає план до міграції);
  * для решти запитів — `SCAN listings` без «COVERING INDEX»: прохід усіх рядків
    таблиці або всього індексу з читанням рядків. SEARCH за індексом і прохід
    покривного індексу (без читання рядків) — допустимі.
"""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import event, func, select

from ..models import Listing, effective_active
from .queries import SORTS, list_ids_select, page_rows_select

# Фільтри списку, які перевіряє охоронець (× 8 сортувань).
LIST_FILTERS: list[tuple[str, dict]] = [
    ("без фільтра", {}),
    ("кімнат 2", {"rooms": "2"}),
    ("кімнат 4+", {"rooms": "4+"}),
    ("джерело", {"source": "olx"}),
    ("стан", {"condition": "renovated"}),
    ("ринок", {"market": "secondary"}),
    ("ціна", {"price_min": 50_000.0, "price_max": 80_000.0}),
    ("в обробці", {"in_progress": True}),
    ("усі оголошення", {"collapse": False}),
]


def _place_filters() -> list[tuple[str, dict]]:
    """Фільтри місця (Блок 4, E10, D57): район (з дитиною), ЖК, «не визначено», місто."""
    from ..places.facets import Selection

    return [
        ("район", {"place": Selection(district="tsentr",
                                      districts=("tsentr", "nimetska-koloniia"))}),
        ("район не визначено", {"place": Selection(district="_unknown")}),
        ("ЖК", {"place": Selection(complex="comfort-park", complexes=("comfort-park",))}),
        ("не в ЖК", {"place": Selection(complex="_none")}),
        ("тільки місто", {"place": Selection(area="city")}),
        ("район + кімнат 2", {"rooms": "2", "place": Selection(district="pasichna",
                                                                districts=("pasichna",))}),
    ]

_LISTINGS = re.compile(r"^(SCAN|SEARCH) listings(_\d+)?\b")


def is_bad(line: str, *, strict: bool = False) -> bool:
    """Рядок плану, що читає рядки таблиці listings там, де не мав би.

    `strict` — для запиту id списку: дозволено лише покривний індекс.
    """
    if not _LISTINGS.match(line) or "COVERING INDEX" in line:
        return False
    return strict or line.startswith("SCAN")


def list_queries():
    """(назва, запит) — запит id списку для кожного сортування × фільтра."""
    for sort in SORTS:
        for label, kw in LIST_FILTERS + _place_filters():
            yield f"список id: {sort}, {label}", list_ids_select(sort=sort, **kw)


def request_queries(now, sample_ids: list[int]):
    """(назва, запит) — те, що теплий запит «/», «В обробці», «Аналітики» робить щоразу."""
    yield "рядки сторінки списку (за id)", page_rows_select(sample_ids or [0])
    yield "значок «В обробці»", (select(func.count()).select_from(Listing)
                                 .where(Listing.in_progress.is_(True)))
    yield "«Аналітика»: перевірено за добу", (select(func.count(Listing.id))
                                             .where(Listing.last_checked >= now - timedelta(days=1)))
    yield "«Аналітика»: перевірено колись", (select(func.count(Listing.id))
                                            .where(Listing.last_checked.isnot(None)))
    yield "«Аналітика»: усього оголошень", select(func.count(Listing.id))
    # Зведення шапки списку — раз на покоління даних (кеш), але й тут без
    # проходу рядків там, де є індекс.
    yield "зведення: оновлено", select(func.max(Listing.last_seen))
    yield "зведення: неактуальних", (select(func.count()).select_from(Listing)
                                     .where(effective_active().is_(False)))
    # Лічильники фільтрів «Район»/«ЖК» (Блок 4, E10): один GROUP BY над запитом id —
    # раз на покоління й фільтр, але й тут без проходу рядків таблиці.
    from ..places.facets import grouped_select

    yield "лічильники району й ЖК", grouped_select(list_ids_select())
    yield "лічильники району й ЖК, кімнат 2", grouped_select(list_ids_select(rooms="2"))


def explain(session, stmt) -> list[str]:
    """Рядки EXPLAIN QUERY PLAN для запиту (виконується в тій самій сесії — читання)."""
    conn = session.connection()
    captured: list[list[str]] = []

    def before(_conn, cursor, statement, parameters, _context, _many):
        rows = cursor.connection.execute("EXPLAIN QUERY PLAN " + statement,
                                         parameters or ()).fetchall()
        captured.append([r[3] for r in rows])

    event.listen(conn, "before_cursor_execute", before)
    try:
        session.execute(stmt).all()
    finally:
        event.remove(conn, "before_cursor_execute", before)
    return captured[0] if captured else []


def report(session, now) -> list[dict]:
    """[{назва, план, погане, тип}] для всіх перевірених запитів."""
    sample = list(session.scalars(select(Listing.id).order_by(Listing.id.desc()).limit(50)))
    out = []
    for kind, queries in (("список", list_queries()),
                          ("запит", request_queries(now, sample))):
        for label, stmt in queries:
            plan = explain(session, stmt)
            strict = kind == "список"
            out.append({"kind": kind, "label": label, "plan": plan,
                        "bad": [line for line in plan if is_bad(line, strict=strict)]})
    return out
