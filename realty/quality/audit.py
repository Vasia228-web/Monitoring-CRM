"""Аудит дедуплікації: пропущені дублі та хибні склеювання.

Помилки тут дорожчі за помилки валідації одного поля: саме на майстер-записах
рахується медіана й мінімум для порівняння з аналогами.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select

from ..db import SessionLocal
from ..dedup import (
    MERGE_THRESHOLD, STREET_OVERLAP, Shape, conflicts, match_score, shape_of,
    street_overlap,
)
from ..models import Listing

log = logging.getLogger(__name__)

# Наскільки близько до порога має бути пара, щоб вважати її «майже дублем».
NEAR_MISS_BAND = 2


def confidence(a: Shape, b: Shape) -> float:
    """Впевненість у тому, що це одна квартира, 0..1.

    Береться з тієї самої оцінки, що й злиття, але нормалізованої: потрібна,
    щоб слабкі збіги йшли на ручний перегляд, а не в автоматичний merge.
    """
    score = match_score(a, b)
    if score <= 0:
        return 0.0
    ceiling = 12.0        # адреса + будинок + поверх + площа + ціна
    return round(min(1.0, score / ceiling), 2)


def missed_duplicates(listings: list[Listing], limit: int = 200) -> list[dict]:
    """Пари з різних майстер-записів, які виглядають як одна квартира."""
    shapes = {r.id: shape_of(r) for r in listings}
    prop_of = {r.id: r.property_id for r in listings}
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for r in listings:
        if r.rooms is None or r.area_total is None:
            continue
        buckets[(r.rooms, int(round(r.area_total)))].append(r.id)

    found: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for ids in buckets.values():
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if prop_of[a] == prop_of[b] and prop_of[a] is not None:
                    continue                       # уже разом
                pair = (min(a, b), max(a, b))
                if pair in seen:
                    continue
                seen.add(pair)
                score = match_score(shapes[a], shapes[b])
                if score >= MERGE_THRESHOLD - NEAR_MISS_BAND and score > 0:
                    found.append({
                        "a": a, "b": b, "score": score,
                        "confidence": confidence(shapes[a], shapes[b]),
                        "merged_now": score >= MERGE_THRESHOLD,
                    })
                    if len(found) >= limit:
                        return found
    return found


def wrong_merges(listings: list[Listing]) -> list[dict]:
    """Майстер-записи, всередині яких дані суперечать одні одним."""
    by_prop: dict[int, list[Listing]] = defaultdict(list)
    for r in listings:
        if r.property_id is not None:
            by_prop[r.property_id].append(r)

    bad: list[dict] = []
    for pid, members in by_prop.items():
        if len(members) < 2:
            continue
        shapes = [shape_of(m) for m in members]
        problems = []
        if conflicts(shapes):
            problems.append("різні адреси")
        floors = {m.floor for m in members if m.floor is not None}
        if len(floors) > 1:
            problems.append(f"різні поверхи {sorted(floors)}")
        areas = [m.area_total for m in members if m.area_total]
        if areas and max(areas) - min(areas) > 1.5:
            problems.append(f"площа {min(areas)}–{max(areas)}")
        prices = [m.price_usd for m in members if m.price_usd]
        if prices and min(prices) > 0 and max(prices) / min(prices) > 1.5:
            problems.append(f"ціна ${min(prices):,.0f}–${max(prices):,.0f}")
        if problems:
            bad.append({"property_id": pid, "members": len(members),
                        "problems": problems,
                        "sources": sorted({m.source for m in members})})
    return bad


def run(limit: int = 200) -> dict:
    with SessionLocal() as s:
        listings = list(s.scalars(select(Listing)))
    missed = missed_duplicates(listings, limit=limit)
    wrong = wrong_merges(listings)
    low_conf = [m for m in missed if m["merged_now"] and m["confidence"] < 0.75]
    return {
        "listings": len(listings),
        "missed_pairs": len(missed),
        "missed_below_threshold": len([m for m in missed if not m["merged_now"]]),
        "low_confidence_merges": len(low_conf),
        "wrong_merges": len(wrong),
        "missed_examples": missed[:5],
        "wrong_examples": wrong[:5],
    }
