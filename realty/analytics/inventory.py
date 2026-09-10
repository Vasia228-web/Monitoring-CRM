"""Інвентаризація бази перед побудовою аналітики.

Модуль тільки міряє. Він відповідає на одне питання: які з бажаних графіків
взагалі можна побудувати чесно на тих даних, що є зараз. Порогів і висновків
тут немає — вони приймаються за результатами цих вимірів.
"""
from __future__ import annotations

import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Listing, PriceEvent, Property

# Скільки об'єктів у сегменті вважаємо мінімумом для розмови про статистику.
# Тут це лише лінійка для звіту; робочий поріг живе в налаштуваннях аналітики.
REPORT_MIN = 30


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def masters(session) -> dict:
    """Скільки унікальних майстер-об'єктів і скільки з них зведені з кількох джерел."""
    total = session.scalar(select(func.count(Property.id))) or 0
    multi = session.scalar(
        select(func.count(Property.id)).where(Property.sources_count > 1)) or 0
    orphan = session.scalar(
        select(func.count(Listing.id)).where(Listing.property_id.is_(None))) or 0
    with_rooms = session.scalar(
        select(func.count(Property.id)).where(Property.rooms.isnot(None))) or 0
    with_district = session.scalar(
        select(func.count(Property.id)).where(Property.district.isnot(None))) or 0
    return {"properties": total, "merged_from_several_sources": multi,
            "listings_without_master": orphan,
            "with_rooms": with_rooms, "with_district": with_district}


def price_history_depth(session) -> dict:
    """Реальна глибина історії цін.

    Важлива різниця: подія пишеться і при першій появі оголошення (базова
    ціна), і при кожній зміні. Тому «є що будувати» — це не кількість подій,
    а кількість об'єктів, у яких подій більше, ніж оголошень: тільки такі
    пережили хоча б одну справжню зміну ціни.
    """
    first, last, events = session.execute(
        select(func.min(PriceEvent.observed_at), func.max(PriceEvent.observed_at),
               func.count(PriceEvent.id))).one()
    span_days = (last - first).total_seconds() / 86400 if first and last else 0.0

    changed_listings = session.scalar(select(func.count()).select_from(
        select(PriceEvent.listing_id).group_by(PriceEvent.listing_id)
        .having(func.count(PriceEvent.id) > 1).subquery())) or 0

    rows = session.execute(
        select(Listing.property_id, PriceEvent.listing_id)
        .join(Listing, Listing.id == PriceEvent.listing_id)
        .where(Listing.property_id.isnot(None))).all()
    per_master: dict[int, list[int]] = defaultdict(list)
    for prop_id, listing_id in rows:
        per_master[prop_id].append(listing_id)
    changed_masters = sum(1 for pid, ls in per_master.items() if len(ls) > len(set(ls)))

    return {"events": events, "first": first, "last": last,
            "span_days": round(span_days, 2),
            "listings_with_price_change": changed_listings,
            "masters_with_price_change": changed_masters,
            "masters_with_any_history": len(per_master)}


def lifecycle(session) -> dict:
    """Життєвий цикл: скільки оголошень зникло з ринку і скільки прожили.

    Строк рахуємо від `first_seen` — це момент, коли ми оголошення побачили,
    а не коли воно з'явилось. Вік від `published_at` показуємо окремо, бо це
    різні речі й змішувати їх не можна.
    """
    total = session.scalar(select(func.count(Listing.id))) or 0
    delisted = session.execute(
        select(Listing.first_seen, Listing.published_at, Listing.delisted_at)
        .where(Listing.delisted_at.isnot(None))).all()
    checked = session.scalar(
        select(func.count(Listing.id)).where(Listing.last_checked.isnot(None))) or 0

    observed = [(d - f).total_seconds() / 86400 for f, _, d in delisted if f and d]
    published_age = [(d - p).total_seconds() / 86400
                     for _, p, d in delisted if p and d]
    return {
        "listings": total, "delisted": len(delisted),
        "delisted_share": round(100 * len(delisted) / total, 3) if total else 0.0,
        "ever_checked": checked,
        "check_coverage": round(100 * checked / total, 1) if total else 0.0,
        "observed_days_median": round(st.median(observed), 1) if observed else None,
        "age_at_delist_median": round(st.median(published_age), 1) if published_age else None,
    }


def segments(session, min_size: int = REPORT_MIN) -> dict:
    """Скільки майстер-об'єктів припадає на кожен сегмент.

    Рахуємо на рівні майстер-об'єктів, а не оголошень: та сама квартира з
    трьох майданчиків інакше потроїла б сегмент і зробила б вибірку більшою,
    ніж вона є насправді.
    """
    rows = session.execute(
        select(Property.rooms, Property.condition, Property.market_type,
               Property.district, Property.price_per_sqm, Property.area_total)
        .where(Property.price_per_sqm.isnot(None))).all()

    by3: dict[tuple, list] = defaultdict(list)
    by4: dict[tuple, list] = defaultdict(list)
    for rooms, cond, market, district, ppsqm, area in rows:
        rooms_band = None if rooms is None else (rooms if rooms < 4 else 4)
        key3 = (rooms_band, cond.value, market.value)
        by3[key3].append(ppsqm)
        if district:
            by4[key3 + (district,)].append(ppsqm)

    def summarise(buckets):
        ok = {k: v for k, v in buckets.items() if len(v) >= min_size}
        return {"combos": len(buckets), "combos_ok": len(ok),
                "covered": sum(len(v) for v in ok.values()),
                "total": sum(len(v) for v in buckets.values())}

    top = sorted(by3.items(), key=lambda kv: -len(kv[1]))
    return {
        "rooms_condition_market": summarise(by3),
        "with_district": summarise(by4),
        "table": [{"rooms": k[0], "condition": k[1], "market": k[2],
                   "n": len(v), "median_ppsqm": round(st.median(v))}
                  for k, v in top],
    }


def survivorship(session) -> dict:
    """Чи можна брати `published_at` за вісь часу для динаміки цін.

    Не можна, якщо в базі систематично бракує старих оголошень: ми бачимо лише
    ті, що досі висять, а вони — саме ті, що НЕ продались. Міряємо перекіс
    розподілу дат публікації, щоб це було видно цифрою, а не на око.
    """
    rows = session.execute(
        select(Listing.published_at).where(Listing.published_at.isnot(None))).all()
    dates = sorted(r[0] for r in rows)
    if not dates:
        return {"with_published_at": 0, "earliest": None, "latest": None,
                "by_age": {}, "share_last_90d": 0.0, "share_older_1y": 0.0}
    now = _now()
    buckets = defaultdict(int)
    for d in dates:
        age = (now - d).days
        key = ("0-30" if age <= 30 else "31-90" if age <= 90 else
               "91-180" if age <= 180 else "181-365" if age <= 365 else "365+")
        buckets[key] += 1
    n = len(dates)
    return {"with_published_at": n, "earliest": dates[0], "latest": dates[-1],
            "by_age": dict(buckets),
            "share_last_90d": round(100 * (buckets["0-30"] + buckets["31-90"]) / n, 1),
            "share_older_1y": round(100 * buckets["365+"] / n, 1)}


def source_composition(session) -> dict:
    """Склад оголошень по джерелах — підстава для чесного порівняння.

    Якщо частки новобудов різні, наївна «медіана по джерелу» міряє асортимент,
    а не ціни. Тут показуємо і наївну цифру, і склад — щоб різниця була видна.
    """
    rows = session.execute(
        select(Listing.source, Listing.market_type, Listing.price_per_sqm)
        .where(Listing.quality_status == "ok",
               Listing.price_per_sqm.isnot(None))).all()
    agg: dict[str, dict] = defaultdict(lambda: {"ppsqm": [], "primary": 0})
    for source, market, ppsqm in rows:
        agg[source]["ppsqm"].append(ppsqm)
        if market.value == "primary":
            agg[source]["primary"] += 1
    return {s: {"n": len(v["ppsqm"]),
                "naive_median_ppsqm": round(st.median(v["ppsqm"])),
                "primary_share": round(100 * v["primary"] / len(v["ppsqm"]), 1)}
            for s, v in sorted(agg.items(), key=lambda kv: -len(kv[1]["ppsqm"]))}


def area_effect(session) -> dict:
    """Наскільки ціна за м² залежить від площі всередині однієї кімнатності.

    Напрямок ефекту не постулюємо — міряємо. Якщо різниця між крайніми
    квартилями площі помітна, порівнювати об'єкт можна лише зі схожими за
    площею, інакше оцінка «дорожче/дешевше за ринок» буде про розмір.
    """
    out = {}
    for rooms in (1, 2, 3):
        rows = session.execute(
            select(Property.area_total, Property.price_per_sqm)
            .where(Property.rooms == rooms, Property.area_total.isnot(None),
                   Property.price_per_sqm.isnot(None))).all()
        if len(rows) < 4 * REPORT_MIN:
            continue
        areas = sorted(a for a, _ in rows)
        cuts = [areas[len(areas) * i // 4] for i in (1, 2, 3)]
        bands: list[list[float]] = [[], [], [], []]
        for area, ppsqm in rows:
            idx = sum(area >= c for c in cuts)
            bands[idx].append(ppsqm)
        medians = [round(st.median(b)) for b in bands if b]
        out[rooms] = {"n": len(rows), "area_cuts": [round(c, 1) for c in cuts],
                      "band_medians": medians,
                      "spread_pct": round(100 * (max(medians) / min(medians) - 1), 1)}
    return out


def run() -> dict:
    with SessionLocal() as s:
        return {"masters": masters(s), "history": price_history_depth(s),
                "lifecycle": lifecycle(s), "segments": segments(s),
                "survivorship": survivorship(s),
                "sources": source_composition(s), "area_effect": area_effect(s)}
