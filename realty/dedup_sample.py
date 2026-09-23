"""Щотижнева перевірка зведення: 20 випадкових квартир із 2+ оголошеннями (D41).

Автоматичний аналог ручної вибірки 22.09. Для кожної квартири — вердикт:
  * «кілька» — є доказ, що всередині різні квартири: різні id квартири
    DIM.RIA, різні корпуси, різні поверхи, різні будинки одного джерела;
  * «одна» — усі оголошення пов'язані прямими доказами: той самий id квартири
    DIM.RIA чи те саме посилання, а на вторинному ринку ще й майже дослівно
    однаковий опис або те саме головне фото;
  * «неясно» — ні того, ні іншого.
Опис і фото в новобудовах доказом не вважаються: на вибірці 22.09 майже
однаковий опис мали 45 пар оголошень із РІЗНИМИ id квартири DIM.RIA (шаблони
забудовника) проти 58 пар з однаковим, однакове головне фото — 15 проти 27
(рендери й плани однакові для всіх квартир того самого типу).

Частка помилок = «кілька» / 20; окремо — без неясних.
Нічого не пише в основну базу; результат — у ops.db, на /status і в Telegram.
"""
from __future__ import annotations

import io
import json
import logging
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

from sqlalchemy import func, select

from . import dedup, ops
from .db import SessionLocal
from .models import Listing

log = logging.getLogger(__name__)

N = 20
DESC_SIMILAR = 0.9
DESC_MIN_LEN = 80
PHOTO_DISTANCE = 6          # відмінних бітів у відбитку 8×8 — «те саме фото»
PHOTO_DELAY = 0.4
PHOTO_TIMEOUT = 20


def _norm(text: str | None) -> str:
    return " ".join(re.findall(r"\w+", (text or "").lower()))[:500]


def photo_hash(data: bytes) -> str | None:
    """Відбиток зображення 8×8 за різницею сусідніх пікселів (dhash).

    Стійкий до стиснення й зміни розміру, тому те саме фото в різних агентів
    дає близькі відбитки."""
    try:
        from PIL import Image
    except ImportError:                       # без Pillow перевірка просто без фото
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
    except Exception:
        return None
    px = list(im.getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0"
                   for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def photo_distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def fetch_photos(shapes: list, client=None) -> dict[int, str]:
    """Відбитки головних фото; недоступні чи невідомі — просто пропускаємо."""
    import httpx

    urls = {sh.id: (sh.photo or None) for sh in shapes}
    urls = {k: v for k, v in urls.items() if v}
    if not urls:
        return {}
    own = client is None
    client = client or httpx.Client(timeout=PHOTO_TIMEOUT, follow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0"})
    cache: dict[str, str | None] = {}
    out: dict[int, str] = {}
    try:
        for lid, url in urls.items():
            if url not in cache:
                time.sleep(PHOTO_DELAY)
                try:
                    r = client.get(url)
                    cache[url] = (photo_hash(r.content)
                                  if r.status_code == 200
                                  and r.headers.get("content-type", "").startswith("image")
                                  else None)
                except Exception as e:
                    log.info("фото %s не завантажилось: %s", url[:60], str(e)[:80])
                    cache[url] = None
            if cache[url]:
                out[lid] = cache[url]
    finally:
        if own:
            client.close()
    return out


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


def judge(shapes: list[dedup.Shape], texts: dict[int, str],
          photos: dict[int, str] | None = None) -> tuple[str, str]:
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
                pa, pb = (photos or {}).get(a.id), (photos or {}).get(b.id)
                linked = linked or bool(pa and pb and photo_distance(pa, pb) <= PHOTO_DISTANCE)
            if linked:
                parent[find(a.id)] = find(b.id)
    parts = len({find(sh.id) for sh in shapes})
    if parts == 1:
        return "one", "усі пов'язані прямими доказами"
    return "unclear", f"доказів не вистачає: {parts} незв'язаних частин"


def run(seed: int | None = None, record: bool = True, session=None,
        photos: bool = True) -> dict:
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
    hashes = fetch_photos(shapes) if photos else {}
    by_prop: dict[int, list] = defaultdict(list)
    for l, sh in zip(listings, shapes):
        by_prop[l.property_id].append(sh)
    details = []
    for pid in pids:
        verdict, why = judge(by_prop[pid], texts, hashes)
        details.append({"property_id": pid, "listings": len(by_prop[pid]),
                        "verdict": verdict, "why": why})
    count = {v: sum(1 for d in details if d["verdict"] == v) for v in ("one", "several", "unclear")}
    log.info("вибірка: %d квартир, фото звірено в %d оголошень", len(pids), len(hashes))
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
