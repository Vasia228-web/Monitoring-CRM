"""Самоперевірка зведення після кожного кроку «дублі» (D41).

Помилки зведення мають ловитись самі, а не вибіркою раз на місяць:
  * протиріччя всередині квартири — різні корпуси, різні id квартири DIM.RIA,
    різні будинки, розкид цін серед одночасних оголошень, різна площа, поверх
    чи стан, координати далеко одна від одної. Такі квартири — у чергу на
    перегляд (/status), де власник вирішує кнопками на сторінці квартири;
  * пропущені дублі — той самий id квартири DIM.RIA чи те саме посилання в
    різних квартирах.
Пари, про які власник сказав «це одна квартира» / «це різні», не рахуються.
Тривогу в Telegram дає сторож, коли підозрілих різко більше, ніж звичайно.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from sqlalchemy import select

from . import dedup, ops
from .models import Condition, Listing

KINDS = {
    "ria_flat": "різні id квартири DIM.RIA",
    "korpus": "різні корпуси",
    "building": "різні будинки (за id джерела)",
    "price": "ціни одночасних оголошень різняться >10%",
    "area": "площа різниться більш ніж на 1,5 м²",
    "floor": "різні поверхи",
    "condition": "«з ремонтом» і «без ремонту»",
    "geo": "точні координати далі, ніж 250 м",
}
MISSED = {"ria_flat": "той самий id квартири DIM.RIA в різних квартирах",
          "url": "те саме посилання в різних квартирах"}
# Площу агенти пишуть по-різному навіть для однієї квартири (40,45–41,3 м²
# в одного id DIM.RIA) — протиріччям вважаємо лише помітно більший розкид.
AREA_SPREAD = 1.5
QUEUE_LIMIT = 300


def contradictions(a: dedup.Shape, b: dedup.Shape) -> set[str]:
    """Що в цій парі свідчить «це різні квартири»."""
    out = set()
    if a.flat and b.flat and a.flat != b.flat:
        out.add("ria_flat")
    if a.korpus and b.korpus and a.korpus != b.korpus:
        out.add("korpus")
    if (a.building and b.building and a.building != b.building
            and a.building.split(":")[0] == b.building.split(":")[0]
            and not (a.osm and b.osm and a.osm == b.osm)):
        out.add("building")
    gap = dedup.concurrent_gap(a, b)
    if gap is not None and gap > dedup.PRICE_CONCURRENT:
        out.add("price")
    if a.area and b.area and abs(a.area - b.area) > AREA_SPREAD:
        out.add("area")
    if a.floor is not None and b.floor is not None and a.floor != b.floor:
        out.add("floor")
    if {a.condition, b.condition} == {Condition.RENOVATED, Condition.NEEDS_REPAIR}:
        out.add("condition")
    d = dedup.distance(a, b)
    if d is not None and d > dedup.GEO_VETO_M:
        out.add("geo")
    return out


def audit(session) -> dict:
    listings = list(session.scalars(select(Listing).where(Listing.property_id.is_not(None))))
    shapes = dedup.load_shapes(session, listings)
    manual = dedup.Manual.load(session)
    pid_of = {l.id: l.property_id for l in listings}
    by_prop: dict[int, list] = defaultdict(list)
    for sh in shapes:
        by_prop[pid_of[sh.id]].append(sh)

    queue, counts = [], Counter()
    multi = 0
    for pid, members in by_prop.items():
        if len(members) < 2:
            continue
        multi += 1
        kinds: Counter = Counter()
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if manual and manual.relation(a.id, b.id) == "same":
                    continue
                kinds.update(contradictions(a, b))
        if kinds:
            counts.update(kinds.keys())
            queue.append({"property_id": pid, "n": len(members),
                          "kinds": sorted(kinds, key=lambda k: -kinds[k])})
    # Спершу — найбільше видів протиріч, далі — найбільші квартири.
    queue.sort(key=lambda q: (-len(q["kinds"]), -q["n"], q["property_id"]))

    missed = []
    for kind, key_of in (("ria_flat", lambda sh: sh.flat),
                         ("url", lambda sh: dedup._url_key(sh.url) if sh.url else None)):
        groups: dict[str, list] = defaultdict(list)
        for sh in shapes:
            if (k := key_of(sh)):
                groups[k].append(sh.id)
        for key, ids in groups.items():
            pids = sorted({pid_of[i] for i in ids})
            if len(pids) < 2:
                continue
            if manual and any(manual.relation(x, y) == "different"
                              for x in ids for y in ids if pid_of[x] != pid_of[y]):
                continue
            missed.append({"kind": kind, "key": key, "properties": pids})
    return {"properties": multi, "suspicious": len(queue), "by_kind": dict(counts),
            "missed": len(missed), "queue": queue[:QUEUE_LIMIT], "missed_list": missed[:QUEUE_LIMIT],
            "missed_by_kind": dict(Counter(m["kind"] for m in missed))}


def record(result: dict, rules) -> None:
    ops.init_ops()
    with ops.ops_session() as s:
        s.add(ops.DedupAudit(
            rules=",".join(sorted(rules)) or None, properties=result["properties"],
            suspicious=result["suspicious"], missed=result["missed"],
            by_kind=json.dumps(result["by_kind"], ensure_ascii=False),
            queue=json.dumps(result["queue"], ensure_ascii=False),
            missed_list=json.dumps(result["missed_list"], ensure_ascii=False)))


def recent(limit: int = 10) -> list:
    ops.init_ops()
    with ops.ops_session() as s:
        return list(s.scalars(select(ops.DedupAudit).order_by(ops.DedupAudit.id.desc())
                              .limit(limit)))
