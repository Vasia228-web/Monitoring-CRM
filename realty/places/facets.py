"""Фільтри «Район», «ЖК», «тільки місто» і лічильники біля кожного варіанта (E10, D57).

Одне визначення на обидва шляхи — і саме тому число біля варіанта дорівнює видачі за
побудовою:
  * `conditions(sel)` — умови SQL для запиту id списку (queries._apply_filters);
  * `predicate(sel)` — та сама умова в Python над (row_district, row_complex, row_area);
  * лічильники — ОДИН GROUP BY (row_district, row_complex, row_area) над тим самим
    запитом id без фільтрів місця (кеш за поколінням даних, web/speedcache.facets), а
    число варіанта — сума груп, що проходять `predicate` вибірки «інші фільтри + цей
    варіант». Район рахується з урахуванням «тільки місто», ЖК — району й місцевості,
    перемикач місцевості — району й ЖК.

Значення: district — ключ довідника (з дітьми: «Центр» включає «Центр (Німецька
колонія)»), '_unknown' (NULL); complex — ключ (парасолька включає ЖК усередині),
'_none' («не в ЖК»), '_unknown' (NULL); area — 'city' («Тільки місто»: row_area
'city', а невідома місцевість — теж, якщо rules.area.unknown_counts_as_city), '' — усе.
"""
from __future__ import annotations

from dataclasses import dataclass

from .directory import NONE, UNKNOWN

AREA_CITY = "city"


@dataclass(frozen=True)
class Selection:
    district: str = ""
    complex: str = ""
    area: str = ""
    districts: tuple = ()          # ключ + діти
    complexes: tuple = ()          # ключ + ЖК усередині парасольки
    unknown_city: bool = True

    @property
    def active(self) -> bool:
        return bool(self.district or self.complex or self.area)


def selection(d, rules, *, district: str = "", complex_: str = "", area: str = ""
              ) -> tuple[Selection, list[str]]:
    """Перевірена вибірка й попередження про невідомі значення (фільтр тоді не діє)."""
    warnings: list[str] = []
    district, complex_, area = (district or "").strip(), (complex_ or "").strip(), \
        (area or "").strip()
    if d is None:
        if district or complex_ or area:
            warnings.append("Довідник районів і ЖК зараз недоступний — фільтр за місцем "
                            "не застосовано.")
        return Selection(), warnings
    districts = complexes = ()
    if district and district != UNKNOWN:
        if district in d.districts:
            districts = d.family(district)
        else:
            warnings.append("Район не знайдено в довіднику — фільтр не застосовано.")
            district = ""
    if complex_ and complex_ not in (UNKNOWN, NONE):
        if complex_ in d.complexes:
            complexes = d.complex_family(complex_)
        else:
            warnings.append("ЖК не знайдено в довіднику — фільтр не застосовано.")
            complex_ = ""
    if area and area != AREA_CITY:
        warnings.append("Невідоме значення місцевості — фільтр не застосовано.")
        area = ""
    return Selection(district, complex_, area, districts, complexes,
                     rules.area.unknown_counts_as_city), warnings


def without_complex(sel: Selection) -> Selection:
    """Та сама вибірка без ЖК (ЖК, якого у вибраному районі немає, — скидається)."""
    from dataclasses import replace

    return replace(sel, complex="", complexes=())


def conditions(sel: Selection, model) -> list:
    """Умови SQL на row_* моделі Listing (або її псевдоніма)."""
    from sqlalchemy import or_

    out = []
    if sel.district == UNKNOWN:
        out.append(model.row_district.is_(None))
    elif sel.district:
        out.append(model.row_district.in_(sel.districts))
    if sel.complex == UNKNOWN:
        out.append(model.row_complex.is_(None))
    elif sel.complex == NONE:
        out.append(model.row_complex == NONE)
    elif sel.complex:
        out.append(model.row_complex.in_(sel.complexes))
    if sel.area == AREA_CITY:
        out.append(or_(model.row_area == AREA_CITY, model.row_area.is_(None))
                   if sel.unknown_city else model.row_area == AREA_CITY)
    return out


def _district_ok(sel: Selection, rd) -> bool:
    if sel.district == UNKNOWN:
        return rd is None
    return not sel.district or rd in sel.districts


def _complex_ok(sel: Selection, rc) -> bool:
    if sel.complex == UNKNOWN:
        return rc is None
    if sel.complex == NONE:
        return rc == NONE
    return not sel.complex or rc in sel.complexes


def _area_ok(sel: Selection, ra) -> bool:
    if sel.area != AREA_CITY:
        return True
    return ra == AREA_CITY or (ra is None and sel.unknown_city)


def predicate(sel: Selection):
    """Та сама умова, що й `conditions`, над (row_district, row_complex, row_area)."""
    return lambda rd, rc, ra: (_district_ok(sel, rd) and _complex_ok(sel, rc)
                               and _area_ok(sel, ra))


def grouped_select(stmt_ids):
    """GROUP BY row_* із ТИМИ САМИМИ умовами, що й запит id списку (без фільтрів місця).

    Той самий Select (умови якості, актуальності, згортання, фільтри), лише стовпці —
    row_* і кількість: тож сума груп — рівно кількість id списку, а план — прохід
    покривного ix_listings_place без читання рядків таблиці.
    """
    from sqlalchemy import func

    from ..models import Listing

    return (stmt_ids.order_by(None)
            .with_only_columns(Listing.row_district, Listing.row_complex, Listing.row_area,
                               func.count())
            .group_by(Listing.row_district, Listing.row_complex, Listing.row_area))


def _sum(groups, ok) -> int:
    return sum(n for rd, rc, ra, n in groups if ok(rd, rc, ra))


def _with(sel: Selection, **kw) -> Selection:
    from dataclasses import replace

    return replace(sel, **kw)


def options(groups, sel: Selection, d, rules) -> dict:
    """Варіанти фільтрів із числами — кожне дорівнює видачі з цим варіантом.

    groups — [(row_district, row_complex, row_area, n)] вибірки БЕЗ фільтрів місця.
    """
    labels = rules.labels
    total = _sum(groups, predicate(sel))
    # --- Райони: з урахуванням місцевості, без ЖК (зміна району скидає ЖК).
    dsel = _with(sel, complex="", complexes=())
    per_district: dict = {}
    for rd, rc, ra, n in groups:
        if _area_ok(dsel, ra):
            per_district[rd] = per_district.get(rd, 0) + n
    groups_d = {"city": [], "hromada": [], "outside": []}
    for key, item in d.districts.items():
        if item.parent:
            continue
        count = sum(per_district.get(k, 0) for k in d.family(key))
        kids = [{"key": c, "label": d.districts[c].name, "n": per_district.get(c, 0),
                 "child": True, "selected": sel.district == c}
                for c in d.children(key)]
        kids = [k for k in kids if k["n"] or k["selected"]]
        if not count and sel.district != key and not kids:
            continue
        groups_d[item.area].append({"key": key, "label": item.name, "n": count,
                                    "child": False, "selected": sel.district == key,
                                    "children": sorted(kids, key=lambda k: _sort_key(k["label"]))})
    for name in groups_d:
        groups_d[name].sort(key=lambda o: _sort_key(o["label"]))
    district_unknown = per_district.get(None, 0)
    # --- ЖК: з урахуванням району й місцевості.
    csel = _with(sel, complex="", complexes=())
    per_complex: dict = {}
    for rd, rc, ra, n in groups:
        if _district_ok(csel, rd) and _area_ok(csel, ra):
            per_complex[rc] = per_complex.get(rc, 0) + n
    complexes = []
    # Список ЖК — лише для вибраного району (або вже вибраного ЖК): без району на
    # сторінці його немає (план Блоку 4), тож і не рахуємо.
    for key, item in (d.complexes.items() if (sel.district or sel.complex) else ()):
        if item.within:
            continue
        count = sum(per_complex.get(k, 0) for k in d.complex_family(key))
        kids = [{"key": c, "label": d.complexes[c].name, "n": per_complex.get(c, 0),
                 "child": True, "selected": sel.complex == c}
                for c in d.complex_family(key)[1:]]
        kids = [k for k in kids if k["n"] or k["selected"]]
        if not count and sel.complex != key and not kids:
            continue
        complexes.append({"key": key, "label": d.complex_display(key, prefix=False),
                          "n": count, "child": False,
                          "selected": sel.complex == key,
                          "children": sorted(kids, key=lambda k: _sort_key(k["label"]))})
    complexes.sort(key=lambda o: _sort_key(o["label"]))
    # --- Місцевість: з урахуванням району й ЖК.
    asel = _with(sel, area="")
    area_all = _sum(groups, predicate(asel))
    area_city = _sum(groups, predicate(_with(asel, area=AREA_CITY)))
    return {
        "total": total,
        "district_groups": [
            # Позначка «громада»/«поза громадою» вже біля кожного рядка списку — у назві
            # групи її не повторюємо (рецензія E10: «Громада (села) — громада»).
            {"name": "Місто", "area": "city", "options": groups_d["city"]},
            {"name": "Громада (села)", "area": "hromada", "options": groups_d["hromada"]},
            {"name": "Поза громадою", "area": "outside", "options": groups_d["outside"]},
        ],
        "district_unknown": district_unknown,
        "district_all": sum(per_district.values()),
        "complexes": complexes,
        "complex_all": sum(per_complex.values()),
        "complex_none": per_complex.get(NONE, 0),
        "complex_unknown": per_complex.get(None, 0),
        "area_all": area_all,
        "area_city": area_city,
        "labels": {"area_city": labels.area_city, "area_all": labels.area_all,
                   "unknown_district": labels.unknown_district,
                   "unknown_complex": labels.unknown_complex,
                   "none_complex": labels.none_complex},
    }


# Український алфавіт для сортування назв (casefold сам ставить «і», «ї», «є» не туди).
_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
_ORDER = {ch: i for i, ch in enumerate(_ALPHABET)}


def _sort_key(text: str) -> tuple:
    s = (text or "").casefold()
    return tuple((0, _ORDER[ch]) if ch in _ORDER else (1, ord(ch)) for ch in s)
