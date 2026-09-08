"""Діагностика бази: які аномалії там фактично є.

Порогів тут навмисно немає — модуль лише міряє. Постійні правила виводяться
з цих вимірів, а не вигадуються наперед.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Listing, PriceEvent, Property, effective_active

REQUIRED = ("price", "rooms", "location", "original_url")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def percentiles(values: list[float], points=(1, 5, 25, 50, 75, 95, 99)) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    out = {}
    for p in points:
        idx = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
        out[f"p{p}"] = round(ordered[idx], 2)
    return out


def missing_fields(session) -> dict:
    """Скільки записів не мають обов'язкових полів."""
    out = {}
    for field in REQUIRED:
        col = getattr(Listing, field)
        cond = col.is_(None)
        if field in ("location", "original_url"):
            cond = col.is_(None) | (col == "")
        out[field] = session.scalar(
            select(func.count()).select_from(Listing).where(cond)
        ) or 0
    out["any"] = session.scalar(select(func.count()).select_from(Listing).where(
        Listing.price.is_(None) | Listing.rooms.is_(None)
        | Listing.location.is_(None) | (Listing.location == "")
        | Listing.original_url.is_(None) | (Listing.original_url == "")
    )) or 0
    return out


def price_shape(session) -> dict:
    """Розподіл цін і ціни за м² — основа для порогів."""
    prices = [v for v in session.scalars(
        select(Listing.price_usd).where(Listing.price_usd.isnot(None),
                                        effective_active().is_(True)))]
    sqm = [v for v in session.scalars(
        select(Listing.price_per_sqm).where(Listing.price_per_sqm.isnot(None),
                                            effective_active().is_(True)))]
    areas = [v for v in session.scalars(
        select(Listing.area_total).where(Listing.area_total.isnot(None),
                                         effective_active().is_(True)))]
    return {"price_usd": percentiles(prices), "price_per_sqm": percentiles(sqm),
            "area_total": percentiles(areas),
            "n_price": len(prices), "n_sqm": len(sqm)}


def sanity_mismatch(session) -> dict:
    """Чи збігається збережена ціна за м² з price/area."""
    rows = session.execute(select(
        Listing.id, Listing.source, Listing.price_usd, Listing.area_total,
        Listing.price_per_sqm
    ).where(Listing.price_usd.isnot(None), Listing.area_total > 0,
            Listing.price_per_sqm.isnot(None))).all()
    per_source = defaultdict(int)
    total = defaultdict(int)
    worst = []
    for _id, src, price, area, stored in rows:
        total[src] += 1
        calc = price / area
        if calc and abs(stored - calc) / calc > 0.10:
            per_source[src] += 1
            worst.append((round(stored / calc, 1), src, _id))
    worst.sort(reverse=True)
    return {"checked": len(rows), "mismatched": dict(per_source),
            "total_per_source": dict(total), "worst_ratios": worst[:5]}


def freshness(session) -> dict:
    """Життєвий цикл: що перевірено, що знято, що застаріло."""
    tot = session.scalar(select(func.count()).select_from(Listing)) or 0
    checked = session.scalar(select(func.count()).select_from(Listing)
                             .where(Listing.last_checked.isnot(None))) or 0
    inactive = session.scalar(select(func.count()).select_from(Listing)
                              .where(effective_active().is_(False))) or 0
    manual = session.scalar(select(func.count()).select_from(Listing)
                            .where(Listing.manual_active.isnot(None))) or 0
    stale = {}
    for days in (7, 30, 90):
        stale[f"{days}d"] = session.scalar(
            select(func.count()).select_from(Listing).where(
                Listing.last_seen < _now() - timedelta(days=days))
        ) or 0
    return {"total": tot, "checked": checked, "never_checked": tot - checked,
            "inactive": inactive, "manual_marked": manual, "not_seen_since": stale}


def price_history(session) -> dict:
    """Чи справді історія лише дописується."""
    events = session.scalar(select(func.count()).select_from(PriceEvent)) or 0
    listings_with = session.scalar(
        select(func.count(func.distinct(PriceEvent.listing_id)))) or 0
    changed = session.execute(
        select(PriceEvent.listing_id, func.count())
        .group_by(PriceEvent.listing_id).having(func.count() > 1)).all()
    orphans = session.scalar(select(func.count()).select_from(PriceEvent).where(
        ~PriceEvent.listing_id.in_(select(Listing.id)))) or 0
    # різкі стрибки між сусідніми точками
    jumps = []
    for lid, _ in changed:
        pts = [e for e in session.scalars(
            select(PriceEvent).where(PriceEvent.listing_id == lid)
            .order_by(PriceEvent.observed_at))]
        for a, b in zip(pts, pts[1:]):
            if a.price_usd and b.price_usd:
                delta = (b.price_usd - a.price_usd) / a.price_usd
                if abs(delta) >= 0.30:
                    jumps.append((lid, round(delta * 100)))
    return {"events": events, "listings_with_history": listings_with,
            "listings_with_change": len(changed), "orphan_events": orphans,
            "sharp_jumps_30pct": len(jumps), "examples": jumps[:5]}


def llm_records(session) -> dict:
    """Що саме зробив LLM-фолбек і чи адекватні значення."""
    rows = session.scalars(select(Listing).where(Listing.llm_extracted.is_(True))).all()
    bad = []
    for r in rows:
        problems = []
        if not r.price or r.price <= 0:
            problems.append("ціна")
        if r.rooms is not None and not (1 <= r.rooms <= 9):
            problems.append("кімнати")
        if r.area_total is not None and not (8 <= r.area_total <= 1000):
            problems.append("площа")
        if problems:
            bad.append((r.id, r.source, problems))
    return {"total": len(rows), "suspicious": len(bad), "examples": bad[:5]}


def dedup_state(session) -> dict:
    listings = session.scalar(select(func.count()).select_from(Listing)) or 0
    props = session.scalar(select(func.count()).select_from(Property)) or 0
    unassigned = session.scalar(select(func.count()).select_from(Listing)
                                .where(Listing.property_id.is_(None))) or 0
    sizes = session.execute(
        select(Property.sources_count, func.count()).group_by(Property.sources_count)).all()
    return {"listings": listings, "properties": props, "unassigned": unassigned,
            "merged": listings - props, "by_sources_count": dict(sizes)}


def run() -> dict:
    with SessionLocal() as s:
        return {
            "missing": missing_fields(s),
            "prices": price_shape(s),
            "sanity": sanity_mismatch(s),
            "freshness": freshness(s),
            "history": price_history(s),
            "llm": llm_records(s),
            "dedup": dedup_state(s),
        }
