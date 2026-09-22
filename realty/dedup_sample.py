"""Щотижнева перевірка зведення: 20 випадкових квартир із 2+ оголошеннями (D41).

Автоматичний аналог ручної вибірки 22.09. Для кожної квартири — вердикт:
  * «кілька» — є доказ, що всередині різні квартири: різні id квартири
    DIM.RIA, різні корпуси, різні поверхи, різні будинки одного джерела;
  * «одна» — усі оголошення пов'язані прямими доказами: той самий id квартири
    DIM.RIA чи те саме посилання, а на вторинному ринку ще й майже дослівно
    однаковий опис;
  * «неясно» — ні того, ні іншого.
Опис у новобудовах доказом не вважається: на вибірці 22.09 майже однаковий
опис мали 45 пар оголошень із РІЗНИМИ id квартири DIM.RIA (шаблони
забудовника) проти 58 пар з однаковим. Фото — так само (15 проти 27).

Частка помилок = «кілька» / 20; окремо — без неясних.
Нічого не пише в основну базу; результат — у ops.db, на /status і в Telegram.
"""
from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

from sqlalchemy import func, select

from . import dedup, ops
from .db import SessionLocal
from .models import Listing

N = 20
DESC_SIMILAR = 0.9
DESC_MIN_LEN = 80


def _norm(text: str | None) -> str:
    return " ".join(re.findall(r"\w+", (text or "").lower()))[:500]


def pick(session, n: int = N, seed: int | None = None) -> list[int]:
    """Випадкові квартири з 2+ оголошеннями; зерно — номер тижня, щоб повторний
    запуск того ж тижня перевіряв ті самі квартири."""
    pids = sorted(session.scalars(
        select(Listing.property_id).where(Listing.property_id.is_not(None))
        .group_by(Listing.property_id).having(func.count() >= 2)))
    if seed is None:
        iso = datetime.now(timezone.utc).isocalendar()
        seed = iso.year * 100 + iso.week
    return sorted(random.Random(seed).sample(pids, min(n, len(pids))))


def judge(shapes: list[dedup.Shape], texts: dict[int, str]) -> tuple[str, str]:
    """Вердикт для однієї квартири і коротке пояснення."""
    for i, a in enumerate(shapes):
        for b in shapes[i + 1:]:
            if a.flat and b.flat and a.flat != b.flat:
                return "several", f"різні id квартири DIM.RIA ({a.id} і {b.id})"
            if a.korpus and b.korpus and a.korpus != b.korpus:
                return "several", f"різні корпуси {a.korpus} і {b.korpus} ({a.id} і {b.id})"
            if a.floor is not None and b.floor is not None and a.floor != b.floor:
                return "several", f"різні поверхи {a.floor} і {b.floor} ({a.id} і {b.id})"
            if (a.building and b.building and a.building != b.building
                    and a.building.split(":")[0] == b.building.split(":")[0]
                    and not (a.osm and b.osm and a.osm == b.osm)):
                return "several", f"різні будинки ({a.id} і {b.id})"

    newbuild = any(sh.primary for sh in shapes)
    parent = {sh.id: sh.id for sh in shapes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(shapes):
        for b in shapes[i + 1:]:
            linked = dedup.strong(a, b, {"ria_flat"})
            if not linked and not newbuild:
                ta, tb = texts.get(a.id, ""), texts.get(b.id, "")
                linked = (len(ta) >= DESC_MIN_LEN and len(tb) >= DESC_MIN_LEN
                          and SequenceMatcher(None, ta, tb).ratio() >= DESC_SIMILAR)
            if linked:
                parent[find(a.id)] = find(b.id)
    parts = len({find(sh.id) for sh in shapes})
    if parts == 1:
        return "one", "усі пов'язані прямими доказами"
    return "unclear", f"доказів не вистачає: {parts} незв'язаних частин"


def run(seed: int | None = None, record: bool = True, session=None) -> dict:
    own = session is None
    s = session or SessionLocal()
    try:
        pids = pick(s, seed=seed)
        listings = list(s.scalars(select(Listing).where(Listing.property_id.in_(pids))))
        shapes = dedup.load_shapes(s, listings)
    finally:
        if own:
            s.close()
    texts = {l.id: _norm(l.description) for l in listings}
    by_prop: dict[int, list] = defaultdict(list)
    for l, sh in zip(listings, shapes):
        by_prop[l.property_id].append(sh)
    details = []
    for pid in pids:
        verdict, why = judge(by_prop[pid], texts)
        details.append({"property_id": pid, "listings": len(by_prop[pid]),
                        "verdict": verdict, "why": why})
    count = {v: sum(1 for d in details if d["verdict"] == v) for v in ("one", "several", "unclear")}
    n = len(details)
    result = {"n": n, **count,
              "error_share": round(count["several"] / n, 3) if n else None,
              "error_share_decided": (round(count["several"] / (count["one"] + count["several"]), 3)
                                      if count["one"] + count["several"] else None),
              "details": details}
    if record:
        ops.init_ops()
        with ops.ops_session() as o:
            o.add(ops.DedupSample(n=n, one=count["one"], several=count["several"],
                                  unclear=count["unclear"], error_share=result["error_share"],
                                  details=json.dumps(details, ensure_ascii=False)))
    return result


def recent(limit: int = 8) -> list:
    ops.init_ops()
    with ops.ops_session() as o:
        return list(o.scalars(select(ops.DedupSample).order_by(ops.DedupSample.id.desc())
                              .limit(limit)))


def render(res: dict) -> str:
    share = f"{res['error_share']:.0%}" if res["error_share"] is not None else "—"
    lines = [f"🧩 Щотижнева перевірка зведення: {res['n']} випадкових квартир",
             f"одна — {res['one']}, кілька різних — {res['several']}, неясно — {res['unclear']}",
             f"частка помилок: {share}"
             + (f" (серед вирішених {res['error_share_decided']:.0%})"
                if res.get("error_share_decided") is not None else "")]
    bad = [d for d in res["details"] if d["verdict"] == "several"]
    if bad:
        lines.append("Злито різні квартири: " + "; ".join(
            f"/property/{d['property_id']} — {d['why']}" for d in bad[:8]))
    return "\n".join(lines)


def notify_owner(res: dict) -> None:
    from . import notify, watchdog
    notify.send_message(f"{watchdog._header()}\n{render(res)}")
