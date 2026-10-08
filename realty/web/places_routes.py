"""Сторінка «Райони й ЖК» (/places; Блок 4, крок E10, D57) — обидві ролі.

Розподіл рядків списку (квартир; з «Показати всі оголошення» — оголошень) за
місцевістю → районом → ЖК за поточними фільтрами списку. Числа — з того самого
GROUP BY над запитом id, що й лічильники біля фільтрів (кеш за поколінням даних), тож
клік на район чи ЖК відкриває список рівно з таким числом рядків.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


def _tree(groups, sel, d, rules) -> dict:
    """{area: [{key, label, n, children, complexes: [...], none, unknown}]} + невизначені."""
    from ..places import facets
    from ..places.directory import NONE

    area_ok = facets._area_ok
    per: dict = {}
    for rd, rc, ra, n in groups:
        if not area_ok(sel, ra):
            continue
        node = per.setdefault(rd, {})
        node[rc] = node.get(rc, 0) + n

    def complexes_of(keys) -> tuple[list, int, int]:
        merged: dict = {}
        for k in keys:
            for rc, n in per.get(k, {}).items():
                merged[rc] = merged.get(rc, 0) + n
        # Парасолька («Житловий район Княгинин») у списку — разом із ЖК усередині неї:
        # фільтр за нею їх включає (facets.Selection.complexes), тож і число те саме.
        items = [{"key": rc, "label": d.complex_display(rc) or rc,
                  "n": sum(merged.get(k, 0) for k in d.complex_family(rc))}
                 for rc in merged if rc not in (None, NONE)]
        items.sort(key=lambda o: (-o["n"], facets._sort_key(o["label"])))
        return items, merged.get(NONE, 0), merged.get(None, 0)

    areas = {"city": [], "hromada": [], "outside": []}
    for key, item in d.districts.items():
        if item.parent:
            continue
        fam = d.family(key)
        n = sum(sum(per.get(k, {}).values()) for k in fam)
        if not n:
            continue
        cx, none, unknown = complexes_of(fam)
        kids = []
        for c in d.children(key):
            cn = sum(per.get(c, {}).values())
            if cn:
                kids.append({"key": c, "label": d.label(c), "n": cn})
        areas[item.area].append({"key": key, "label": item.name, "n": n, "children": kids,
                                 "complexes": cx, "none": none, "unknown": unknown})
    for name in areas:
        areas[name].sort(key=lambda o: (-o["n"], facets._sort_key(o["label"])))
    cx, none, unknown = complexes_of([None])
    undefined = {"n": sum(per.get(None, {}).values()), "complexes": cx, "none": none,
                 "unknown": unknown}
    totals = {name: sum(o["n"] for o in rows) for name, rows in areas.items()}
    totals["unknown"] = undefined["n"]
    return {"areas": areas, "undefined": undefined, "totals": totals,
            "total": sum(totals.values())}


@router.get("/places", response_class=HTMLResponse)
def places_page(
    request: Request,
    condition: str = Query(""),
    market: str = Query(""),
    source: str = Query(""),
    rooms: str = Query(""),
    district: str = Query(""),
    complex_: str = Query("", alias="complex"),
    area: str = Query(""),
    price_min: str | None = Query(None),
    price_max: str | None = Query(None),
    all_ads: str = Query(""),
):
    from ..db import SessionLocal
    from ..places import facets
    from . import speedcache
    from .app import (DEFAULT_SORT, _base_groups, _in_work, _num, _places, places_ready,
                      templates)

    d, rules = _places()
    sel, warnings = facets.selection(d, rules, district=district, complex_=complex_,
                                     area=area)
    lo, hi = _num(price_min), _num(price_max)
    if lo is not None and hi is not None and lo > hi:
        warnings.append("Ціна «від» більша за «до» — фільтр ціни не застосовано.")
        lo = hi = None
    collapse = all_ads != "1"
    query = dict(condition=condition, market=market, source=source, rooms=rooms,
                 price_min=lo, price_max=hi, sort=DEFAULT_SORT, in_progress=None,
                 collapse=collapse)
    tree = None
    pending = False
    with SessionLocal() as s:
        in_work = _in_work(s)
        dk = speedcache.data_key(s)
        if d is not None and not places_ready(s, dk):
            pending = True                     # крок ще не визначав районів і ЖК
        elif d is not None:
            key = speedcache.list_key({**query, "district": "", "complex": "", "area": "",
                                       "all_ads": all_ads}, in_progress=None,
                                      collapse=collapse)
            tree = _tree(_base_groups(s, query, key, dk=dk), sel, d, rules)
    f = {"condition": condition, "market": market, "source": source, "rooms": rooms,
         "district": sel.district, "complex": sel.complex, "area": sel.area,
         "price_min": price_min or "", "price_max": price_max or "", "all_ads": all_ads}
    return templates.TemplateResponse(request, "places.html", {
        "page": "places", "path": "/places", "f": f, "in_work": in_work, "tree": tree,
        "pending": pending,
        "warning": " ".join(warnings) or None, "collapse": collapse,
        "labels": rules.labels if rules is not None else None,
        "area_city": sel.area == facets.AREA_CITY,
    })
