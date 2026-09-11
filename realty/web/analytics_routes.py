"""Сторінки аналітики: сегменти й окрема квартира.

Роути тонкі — уся математика живе в `realty.analytics`. Тут лише збирання
даних для шаблона й чесні заглушки там, де даних поки бракує.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from ..analytics import cache, forecast
from ..analytics.inventory import check_rate
from ..analytics.objects import analyse
from ..analytics.segments import (
    COND_LABEL, MARKET_LABEL, days_distribution, liquidity_proxy,
    primary_vs_secondary, rooms_label,
)
from ..analytics.settings import load
from ..analytics.survival import Observation, estimate
from ..db import SessionLocal
from ..models import DataReport, Listing

log = logging.getLogger(__name__)

router = APIRouter()


FIELDS = {"condition": "стан", "market": "тип ринку", "rooms": "кімнатність",
          "area": "площа", "price": "ціна", "gone": "оголошення вже немає",
          "": "щось не збігається"}


@router.post("/api/listings/{listing_id}/report")
def api_report(listing_id: int, payload: dict = Body(default={})):
    """«Дані не збігаються» — одне натискання, без форми."""
    field = (payload.get("field") or "").strip()
    if field not in FIELDS:
        return JSONResponse({"ok": False, "error": "невідоме поле"}, status_code=400)
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "оголошення не знайдено"},
                                status_code=404)
        s.add(DataReport(
            listing_id=row.id, property_id=row.property_id, field=field or None,
            # Знімок полів: дані потім зміняться, і без нього буде незрозуміло,
            # на що саме скаржились.
            snapshot={"price_usd": row.price_usd, "rooms": row.rooms,
                      "area_total": row.area_total,
                      "condition": row.condition.value,
                      "market_type": row.market_type.value,
                      "source": row.source, "url": row.original_url},
        ))
        s.commit()
    return JSONResponse({"ok": True, "field": field or None,
                         "label": FIELDS[field]})


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
    proxy = _filtered(liquidity_proxy(universe, cfg),
                      rooms=rooms, condition=condition, market=market)

    # Ліквідність рахуємо тут же, а не заглушкою в шаблоні: блок сам увімкнеться,
    # щойно накопичиться достатньо зафіксованих зникнень.
    peers = [o for o in universe.items
             if (not rooms or str(o.band or "") == rooms)
             and (not condition or o.condition == condition)
             and (not market or o.market == market)]
    liquidity = estimate([Observation(days=obs[0], event=obs[1], entry=obs[2])
                          for o in peers if (obs := o.observation) is not None], cfg)

    covered = sum(r["n"] for r in segments)
    return templates.TemplateResponse(request, "analytics.html", {
        "page": "analytics", "in_work": in_work, "path": "/analytics",
        "segments": segments, "pairs": pairs, "days": days,
        "sources": snapshot.sources_matched,
        "composition": snapshot.sources_composition,
        "below": snapshot.below, "covered": covered, "liquidity": liquidity,
        "sweep": sweep, "proxy": proxy,
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


def _refresh_liveness(session, property_id: int) -> dict:
    """Перевіряє саме це оголошення просто зараз.

    Один запит у момент, коли він справді потрібен: людина відкрила картку й
    зараз на неї дивитиметься. Мертве посилання у видачі дратує найбільше саме
    тут, а черга сліпих перевірок дійде сюди нескоро.

    Заодно піднімаємо об'єкт у черзі: те, що відкривають, варто перевіряти
    частіше за те, на що ніхто не дивиться.
    """
    from ..verify import is_checkable, verify_batch

    ids = [row.id for row in session.scalars(
        select(Listing).where(Listing.property_id == property_id,
                              Listing.is_active.is_(True))).all()
        if is_checkable(row.original_url)]
    if not ids:
        return {"checked": 0, "delisted": 0}
    try:
        stats = verify_batch(limit=len(ids), ids=ids, reason="opened")
    except Exception as e:                                      # noqa: BLE001
        # Сторінка не має падати через те, що джерело не відповіло.
        log.warning("Перевірка при відкритті %s не вдалась: %s", property_id, e)
        return {"checked": 0, "delisted": 0}
    return {"checked": stats["checked"], "delisted": stats["delisted"]}


@router.get("/property/{property_id}", response_class=HTMLResponse)
def property_page(request: Request, property_id: int, verify: str = Query("1")):
    """Аналітика однієї квартири — головна відповідь «краща чи гірша за ринок»."""
    from .app import templates

    cfg = load()
    with SessionLocal() as s:
        if verify != "0":
            _refresh_liveness(s, property_id)
        snapshot = cache.get(s)
        data = analyse(s, snapshot.universe, property_id, cfg)
        in_work = _in_work(s)
        forecast_state = forecast.state(s, cfg)
        if data is None:
            return templates.TemplateResponse(
                request, "property_missing.html",
                {"page": "list", "in_work": in_work, "property_id": property_id,
                 "path": "/", "f": {}},
                status_code=404)
        # Оголошення потрібні шаблону для кнопки «взяти в обробку».
        data["rows"] = s.scalars(select(Listing)
                                 .where(Listing.property_id == property_id)
                                 .order_by(Listing.price_usd.desc())).all()
        # Об'єкт вважаємо взятим в обробку, якщо позначене хоч одне з оголошень:
        # інакше статус, поставлений зі списку, не було б видно на цій сторінці.
        data["in_progress"] = any(r.in_progress for r in data["rows"])
    data.update({"page": "list", "in_work": in_work, "forecast": forecast_state,
                 "path": "/", "f": {},
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
