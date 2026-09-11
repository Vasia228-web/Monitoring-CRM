"""Регулярна гігієна даних.

    щодня    — статуси знятих оголошень, перевірка нових записів
    щотижня  — повний аудит дедуплікації, перерахунок порогів
    щомісяця — повна ревалідація бази під свіжими порогами

Рутини нічого не видаляють. Найгірше, що може статись із записом, — статус
`rejected`: він лишається в базі з причиною, просто не потрапляє у видачу.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..db import SessionLocal, session_scope
from ..models import Listing
from . import audit, diagnose
from .rules import compute_thresholds, load_thresholds, save_thresholds, validate

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def revalidate(limit: int | None = None, thresholds=None) -> dict:
    """Проганяє наявні записи через поточні правила.

    Потрібно і на старті (щоб база не лишилась у стані `pending`), і після
    кожного перерахунку порогів.
    """
    t = thresholds or load_thresholds()
    before: dict[str, int] = {}
    after: dict[str, int] = {}

    with session_scope() as s:
        for status, n in s.execute(
            select(Listing.quality_status, func.count()).group_by(Listing.quality_status)
        ):
            before[status or "—"] = n

        stmt = select(Listing)
        if limit:
            stmt = stmt.order_by(Listing.quality_checked_at.is_(None).desc(),
                                 Listing.quality_checked_at.asc()).limit(limit)
        rows = s.scalars(stmt).all()

        for row in rows:
            # Набір полів має збігатися з тим, що читає `validate`. Раніше
            # сюди не потрапляли ні стан, ні тип ринку, ні текст оголошення —
            # тож перевірки класифікації не спрацьовували б навіть після того,
            # як їх додали.
            rec = {
                "price": row.price, "rooms": row.rooms, "location": row.location,
                "original_url": row.original_url, "price_usd": row.price_usd,
                "price_per_sqm": row.price_per_sqm, "area_total": row.area_total,
                "condition": row.condition, "market_type": row.market_type,
                "title": row.title, "description": row.description,
                "built_year": row.built_year,
            }
            verdict, reasons = validate(rec, t)
            row.quality_status = verdict if verdict != "ok" else "ok"
            row.quality_reason = "; ".join(reasons) or None
            row.quality_checked_at = _now()

        s.flush()
        for status, n in s.execute(
            select(Listing.quality_status, func.count()).group_by(Listing.quality_status)
        ):
            after[status or "—"] = n

    return {"checked": len(rows), "before": before, "after": after}


def daily() -> dict:
    """Легка щоденна рутина."""
    from ..verify import verify_batch

    log.info("Щоденна рутина: перевірка актуальності та нових записів")
    # Без `limit` кожен сайт бере свою порцію: там, де зникнення знаходить
    # різниця списків, сліпий обхід потрібен лише як підстраховка.
    fresh = verify_batch()
    new_records = revalidate(limit=500)
    return {"routine": "daily", "freshness": fresh, "revalidated": new_records}


def weekly() -> dict:
    """Глибша щотижнева рутина: аудит дедуплікації і свіжі пороги."""
    log.info("Щотижнева рутина: аудит дедуплікації та перерахунок порогів")
    audit_result = audit.run(limit=500)
    with SessionLocal() as s:
        t = compute_thresholds(s)
    save_thresholds(t)
    revalidated = revalidate(thresholds=t)
    return {"routine": "weekly", "audit": audit_result,
            "thresholds": {"price": [t.price_usd.review_low, t.price_usd.review_high],
                           "sample": t.sample_size},
            "revalidated": revalidated}


def monthly() -> dict:
    """Повна звірка: діагностика всієї бази під свіжими порогами."""
    log.info("Щомісячна рутина: повна діагностика")
    with SessionLocal() as s:
        t = compute_thresholds(s)
    save_thresholds(t)
    return {"routine": "monthly", "diagnostics": diagnose.run(),
            "revalidated": revalidate(thresholds=t),
            "audit": audit.run(limit=1000)}


ROUTINES = {"daily": daily, "weekly": weekly, "monthly": monthly}
