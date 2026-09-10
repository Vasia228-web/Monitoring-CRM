"""Сторінки аналітики: сегменти й окрема квартира.

Роути тонкі — уся математика живе в `realty.analytics`. Тут лише збирання
даних для шаблона й чесні заглушки там, де даних поки бракує.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from ..analytics import cache, forecast
from ..analytics.inventory import check_rate
from ..analytics.objects import analyse
from ..analytics.segments import (
    COND_LABEL, MARKET_LABEL, days_distribution, primary_vs_secondary, rooms_label,
)
from ..analytics.settings import load
from ..analytics.survival import Observation, estimate
from ..db import SessionLocal
from ..models import Listing

router = APIRouter()


def _in_work(session) -> int:
    return session.scalar(select(func.count()).select_from(Listing)
                          .where(Listing.in_progress.is_(True))) or 0


def _filtered(rows: list[dict], *, rooms: str, condition: str, market: str) -> list[dict]:
    out = rows
    if rooms:
        out = [r for r in out if str(r.get("rooms") or "") == rooms]
    if condition:
        out = [r for r in out if r.get("condition") == condition]
    if market:
        out = [r for r in out if r.get("market") == market]
    return out


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, rooms: str = Query(""), condition: str = Query(""),
                   market: str = Query("")):
    """Сегментна аналітика: медіани, розкид, джерела, ліквідність, прогноз."""
    from .app import templates          # уникаємо циклічного імпорту при старті

    cfg = load()
    with SessionLocal() as s:
        snapshot = cache.get(s)
        forecast_state = forecast.state(s, cfg)
        in_work = _in_work(s)
        sweep = check_rate(s)

    universe = snapshot.universe
    segments = _filtered(snapshot.segments, rooms=rooms, condition=condition, market=market)
    pairs = [r for r in primary_vs_secondary(universe, cfg)
             if (not rooms or str(r["rooms"] or "") == rooms)
             and (not condition or r["condition"] == condition)]
    days = _filtered(days_distribution(universe, cfg),
                     rooms=rooms, condition=condition, market=market)

    # Ліквідність рахуємо тут же, а не заглушкою в шаблоні: блок сам увімкнеться,
    # щойно накопичиться достатньо зафіксованих зникнень.
    peers = [o for o in universe.items if o.days_listed is not None
             and (not rooms or str(o.band or "") == rooms)
             and (not condition or o.condition == condition)
             and (not market or o.market == market)]
    liquidity = estimate([Observation(days=o.days_listed,
                                      event=o.delisted_at is not None) for o in peers], cfg)

    covered = sum(r["n"] for r in segments)
    return templates.TemplateResponse(request, "analytics.html", {
        "page": "analytics", "in_work": in_work,
        "segments": segments, "pairs": pairs, "days": days,
        "sources": snapshot.sources_matched,
        "composition": snapshot.sources_composition,
        "below": snapshot.below, "covered": covered, "liquidity": liquidity,
        "sweep": sweep,
        "universe_size": len(universe),
        "forecast": forecast_state,
        "cfg": cfg,
        "empty": not segments,
        "f": {"rooms": rooms, "condition": condition, "market": market},
        "rooms_options": [("1", "1-кімнатні"), ("2", "2-кімнатні"),
                          ("3", "3-кімнатні"), ("4", "3+ кімнат")],
        "condition_options": list(COND_LABEL.items()),
        "market_options": list(MARKET_LABEL.items()),
    })


@router.get("/property/{property_id}", response_class=HTMLResponse)
def property_page(request: Request, property_id: int):
    """Аналітика однієї квартири — головна відповідь «краща чи гірша за ринок»."""
    from .app import templates

    cfg = load()
    with SessionLocal() as s:
        snapshot = cache.get(s)
        data = analyse(s, snapshot.universe, property_id, cfg)
        in_work = _in_work(s)
        forecast_state = forecast.state(s, cfg)
        if data is None:
            return templates.TemplateResponse(
                request, "property_missing.html",
                {"page": "list", "in_work": in_work, "property_id": property_id},
                status_code=404)
        # Оголошення потрібні шаблону для кнопки «взяти в обробку».
        data["rows"] = s.scalars(select(Listing)
                                 .where(Listing.property_id == property_id)
                                 .order_by(Listing.price_usd.desc())).all()
        # Об'єкт вважаємо взятим в обробку, якщо позначене хоч одне з оголошень:
        # інакше статус, поставлений зі списку, не було б видно на цій сторінці.
        data["in_progress"] = any(r.in_progress for r in data["rows"])
    data.update({"page": "list", "in_work": in_work, "forecast": forecast_state,
                 "cfg": cfg, "rooms_label": rooms_label(data["item"].band),
                 "condition_label": COND_LABEL[data["item"].condition],
                 "market_label": MARKET_LABEL[data["item"].market]})
    return templates.TemplateResponse(request, "property.html", data)


@router.get("/api/analytics/segments")
def api_segments(rooms: str = Query(""), condition: str = Query(""),
                 market: str = Query("")):
    """Ті самі цифри, що на сторінці, — для перевірки й вивантаження."""
    cfg = load()
    with SessionLocal() as s:
        snapshot = cache.get(s)
        state = forecast.state(s, cfg)
    r = state["readiness"]
    return {
        "segments": _filtered(snapshot.segments, rooms=rooms, condition=condition,
                              market=market),
        "below_threshold": snapshot.below,
        "sources": snapshot.sources_matched,
        "forecast": {
            "available": r.ready, "message": state["message"],
            "span_days": r.span_days, "points": r.points,
            "points_needed": r.points_needed,
            "available_from": r.available_from.isoformat() if r.available_from else None,
            "horizon_months": r.horizon_months,
            "schedule": [{**row, "date": row["date"].isoformat()}
                         for row in state["schedule"]],
        },
        "min_sample": cfg.min_sample,
    }


@router.get("/api/analytics/property/{property_id}")
def api_property(property_id: int):
    cfg = load()
    with SessionLocal() as s:
        data = analyse(s, cache.get(s).universe, property_id, cfg)
    if data is None:
        return {"error": "not_found", "property_id": property_id}
    item, history = data["item"], data["history"]
    return {
        "property_id": property_id,
        "rooms": item.rooms, "area": item.area, "price_usd": item.price_usd,
        "price_per_sqm": item.ppsqm,
        "verdict": data["verdict"], "price_band": data["price_band"],
        "history": {"changes": history["changes"], "change_pct": history["change_pct"],
                    "points": [{"at": p["at"].isoformat(), "price": p["price"],
                                "source": p["source"]} for p in history["points"]]},
        "sources": {"count": data["sources"]["count"],
                    "spread_pct": data["sources"]["spread_pct"]},
        "age": data["age"],
        "liquidity": {k: v for k, v in data["liquidity"].items() if k != "curve"},
    }
