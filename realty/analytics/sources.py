"""Порівняння майданчиків — тільки на однакових сегментах.

Наївна «середня ціна на LUN проти середньої на OLX» міряє різницю в
асортименті, а не в цінах: в одного джерела 91% новобудов, в іншого 63%.
Тому порівнюємо медіани однакових сегментів і поруч підписуємо, на скількох
об'єктах кожна цифра порахована. Якщо зіставних сегментів немає — так і
пишемо, замість того щоб рахувати середнє по всьому.
"""
from __future__ import annotations

from sqlalchemy import select

from ..models import Listing
from .segments import COND_LABEL, MARKET_LABEL, rooms_band, rooms_label
from .settings import Settings, load
from .stats import summarise


def composition(session) -> list[dict]:
    """Склад кожного джерела — доказ того, чому наївне порівняння хибне."""
    rows = session.execute(
        select(Listing.source, Listing.market_type, Listing.price_per_sqm)
        .where(Listing.quality_status == "ok", Listing.price_per_sqm.isnot(None))).all()
    agg: dict[str, dict] = {}
    for source, market, ppsqm in rows:
        bucket = agg.setdefault(source, {"values": [], "primary": 0})
        bucket["values"].append(ppsqm)
        if market.value == "primary":
            bucket["primary"] += 1
    out = []
    for source, bucket in agg.items():
        summary = summarise(bucket["values"])
        out.append({
            "source": source, "n": len(bucket["values"]),
            "naive_median": round(summary.median) if summary else None,
            "primary_share": round(100 * bucket["primary"] / len(bucket["values"]), 1),
        })
    return sorted(out, key=lambda r: -r["n"])


def matched(session, cfg: Settings | None = None) -> dict:
    """Медіани по сегментах, у розрізі джерел.

    У таблицю потрапляє лише сегмент, у якому щонайменше два джерела мають
    достатню вибірку: порівнювати нема з чим, якщо джерело в сегменті одне.
    """
    cfg = cfg or load()
    rows = session.execute(
        select(Listing.source, Listing.rooms, Listing.condition, Listing.market_type,
               Listing.price_per_sqm)
        .where(Listing.quality_status == "ok", Listing.price_per_sqm.isnot(None),
               Listing.rooms.isnot(None))).all()

    groups: dict[tuple, dict[str, list[float]]] = {}
    for source, rooms, cond, market, ppsqm in rows:
        key = (rooms_band(rooms), cond.value, market.value)
        groups.setdefault(key, {}).setdefault(source, []).append(ppsqm)

    table = []
    for (band, cond, market), by_source in groups.items():
        cells = {}
        for source, values in by_source.items():
            summary = summarise(values, cfg)
            if summary is None or summary.n < cfg.min_sample_source:
                continue
            cells[source] = {"n": summary.n, "median": round(summary.median)}
        if len(cells) < 2:
            continue
        medians = [c["median"] for c in cells.values()]
        cheapest = min(cells, key=lambda s: cells[s]["median"])
        table.append({
            "rooms": band, "rooms_label": rooms_label(band),
            "condition": cond, "condition_label": COND_LABEL[cond],
            "market": market, "market_label": MARKET_LABEL[market],
            "cells": cells, "sources": sorted(cells),
            "spread_pct": round(100 * (max(medians) / min(medians) - 1), 1),
            "cheapest": cheapest,
            "total": sum(c["n"] for c in cells.values()),
        })

    table.sort(key=lambda r: -r["total"])
    covered = sorted({s for row in table for s in row["cells"]})
    missing = sorted({s for g in groups.values() for s in g} - set(covered))
    return {
        "table": table,
        "sources": covered,
        "excluded": missing,
        "threshold": cfg.min_sample_source,
        "note": ("Джерела порівнюються тільки в межах однакового сегмента. "
                 "Загальна медіана по майданчику показує різницю в асортименті, "
                 "а не в цінах, тому тут її немає."),
        "excluded_note": (
            f"Немає жодного сегмента з вибіркою ≥{cfg.min_sample_source}: "
            f"{', '.join(missing)}." if missing else None),
    }
