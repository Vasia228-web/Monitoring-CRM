"""Сторінки аналітики: сегменти й окрема квартира.

Роути тонкі — уся математика живе в `realty.analytics`. Тут лише збирання
даних для шаблона й чесні заглушки там, де даних поки бракує.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from sqlalchemy import func, select

from ..analytics import cache, forecast
from ..analytics.inventory import check_rate
from ..analytics.objects import analyse
from . import livecheck
from ..analytics.segments import (
    COND_LABEL, MARKET_LABEL, analytics_parts, filter_curve, rooms_label,
)
from ..analytics.settings import load
from ..dedup import resolve_property_id
from ..db import SessionLocal
from ..models import DataReport, Listing, Property

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
    """Значок «В обробці»: живим запитом за ix_listings_in_progress (Блок 2, E4)."""
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
    # Те, що від фільтра не залежить (пари «новобудова/вторинка», дні на ринку,
    # проксі ліквідності, зрізи), рахується раз на знімок, а не на кожен запит;
    # крива строку продажу — раз на фільтр (Блок 2, D48). Фільтр застосовується
    # до готових рядків тут, як і раніше. Отримане — спільне: не змінювати.
    parts = analytics_parts(universe, cfg)
    segments = _filtered(snapshot.segments, rooms=rooms, condition=condition, market=market)
    pairs = [r for r in parts["pairs"]
             if (not rooms or str(r["rooms"] or "") == rooms)
             and (not condition or r["condition"] == condition)]
    day_rows = parts["days"]
    proxy = _filtered(parts["proxy"], rooms=rooms, condition=condition, market=market)
    liquidity = filter_curve(universe, cfg, rooms=rooms, condition=condition, market=market)
    cuts = parts["cuts"]

    covered = sum(r["n"] for r in segments)
    return templates.TemplateResponse(request, "analytics.html", {
        "page": "analytics", "in_work": in_work, "path": "/analytics",
        "segments": segments, "pairs": pairs,
        "sources": snapshot.sources_matched,
        "composition": snapshot.sources_composition,
        "below": snapshot.below, "covered": covered, "liquidity": liquidity,
        "cuts": cuts, "days": day_rows, "days_title": parts["days_title"],
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


def _count_view(session, property_id: int) -> None:
    """Відмічає, що картку відкривали — у буфері, а не записом у запиті (Блок 2, E5).

    Лічильник рухає чергу перевірок: те, на що дивляться, варто перевіряти
    частіше за те, на що ніхто не дивиться. Досі GET писав у базу й чекав
    чужого блокування запису: під 12-секундним блокуванням кроку циклу
    сторінка відкривалась 12 047 мс, під 40-секундним — 500 «database is
    locked» (заміри плану Блоку 2, D48). Тепер id оголошень квартири йдуть у
    буфер (`web/deferred.py`), а фоновий потік раз на `deferred.views_flush_s`
    робить те саме, що й досі: views + число відкриттів, viewed_at — час
    останнього. Перегляд — факт незалежно від того, чи ходили ми на сайт.
    """
    from .perf import WRITER

    ids = list(session.scalars(select(Listing.id).where(Listing.property_id == property_id)))
    WRITER.add_views(ids, datetime.now(), engine=session.get_bind())


def property_rows(session, property_id: int) -> list[Listing]:
    """Оголошення квартири для її сторінки: від найдорожчого до найдешевшого.

    Рівні ціни — за id, явно. Досі їхній порядок давала база, і це був порядок
    id (перевірено на копії Етапу 0: 3 936 квартир із рівними цінами, 0
    відмінностей від ORDER BY ціна, id). Новий індекс міг би його змінити
    (Блок 2, D48).
    """
    return session.scalars(select(Listing)
                           .where(Listing.property_id == property_id)
                           .order_by(Listing.price_usd.desc(), Listing.id)).all()


def place_summary(prop, rows) -> dict | None:
    """Район і ЖК квартири для сторінки (Блок 4, E10, D57): назва з довідника, позначка
    «громада»/«поза громадою» і «звідки» — ступінь, яким визначено (з її оголошень)."""
    from ..places import directory
    from ..places.directory import NONE

    d = directory.current()
    if d is None or prop is None:
        return None
    labels = d.rules.labels
    dk, ck = prop.district_key, prop.complex_key
    d_how = next((r.district_how for r in rows if r.district_key == dk and dk), None)
    c_how = next((r.complex_how for r in rows if r.complex_key == ck and ck), None)
    area = prop.place_area
    return {
        "district": d.label(dk),
        "district_how": labels.how.get(d_how) if d_how else None,
        "complex": d.complex_display(ck),
        "complex_how": labels.how.get(c_how) if c_how and ck != NONE else None,
        "none": labels.none_complex if ck == NONE else None,
        "tag": labels.hromada if area == "hromada" else labels.outside if area == "outside"
        else None,
        "unknown": labels.unknown_district if not dk else None,
    }


def _analyse(session, property_id: int, cfg) -> dict | None:
    """analyse() за знімком; квартира, якої знімок ще не знає, — точкове оновлення.

    Знімок перебудовується за поколінням (після кроку «дублі» й у кінці
    циклу), а список живий: квартира, яку щойно створило зведення, вже є в
    списку, але ще не в знімку. Досі такий запит перебудовував увесь знімок
    (≈5 с на Fedora); тепер — рядок однієї квартири (Блок 2, E5, D50).
    """
    data = analyse(session, cache.get(session).universe, property_id, cfg)
    if data is None and session.get(Property, property_id) is not None:
        if cache.patch_properties(session, [property_id], rebuild=False):
            data = analyse(session, cache.get(session).universe, property_id, cfg)
    return data


@router.get("/property/{property_id}", response_class=HTMLResponse)
def property_page(request: Request, property_id: int, verify: str = Query("1"),
                  hl: str = Query("")):
    """Аналітика однієї квартири — головна відповідь «краща чи гірша за ринок».

    GET нічого не пише й у мережу не ходить (Блок 2, крок E5): перегляд — у
    буфер, перевірка актуальності — завданням у черзі (`livecheck`), результат
    якої сторінка показує банером. Функція та сама, що й досі: перевірка при
    кожному відкритті без verify=0.
    """
    from .app import templates

    cfg = load()
    with SessionLocal() as s:
        # Квартира злилась з іншою — старе посилання веде на ту, що лишилась.
        current = resolve_property_id(s, property_id)
        if current is not None and current != property_id:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/property/{current}{query}", status_code=302)
        _count_view(s, property_id)
        live_check = livecheck.request(property_id) if verify != "0" else None
        data = _analyse(s, property_id, cfg)
        in_work = _in_work(s)
        forecast_state = forecast.state(s, cfg)
        if data is None:
            return templates.TemplateResponse(
                request, "property_missing.html",
                {"page": "list", "in_work": in_work, "property_id": property_id,
                 "path": "/", "f": {}},
                status_code=404)
        # Оголошення потрібні шаблону для кнопки «взяти в обробку».
        data["rows"] = property_rows(s, property_id)
        # Пошук за посиланням (Блок 5, E14, D59): ?hl=<ключ «сайт:id»> — плашка зі станом
        # знайденого оголошення й підсвічування; з тих самих рядків, без нового запиту.
        from .find_routes import found_context
        data["find_hl"] = found_context(data["rows"], hl,
                                        getattr(request.state, "role", None))
        from . import speedcache
        from .app import places_ready

        # До першого `places assign` — як до E10 (сирий район), а не «не визначено».
        data["place"] = (place_summary(data.get("property"), data["rows"])
                         if places_ready(s, speedcache.data_key(s)) else None)
        # Об'єкт вважаємо взятим в обробку, якщо позначене хоч одне з оголошень:
        # інакше статус, поставлений зі списку, не було б видно на цій сторінці.
        data["in_progress"] = any(r.in_progress for r in data["rows"])
        # Ручне виправлення зведення — лише власникові (друг цих кнопок не бачить).
        if getattr(request.state, "role", None) in ("owner", None):
            from .dedup_routes import decisions_for
            data["can_fix_dedup"] = True
            data["decisions"] = decisions_for(s, {r.id for r in data["rows"]})
    data.update({"page": "list", "in_work": in_work, "forecast": forecast_state,
                 "path": "/", "f": {}, "live_check": live_check,
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
        property_id = resolve_property_id(s, property_id) or property_id
        data = _analyse(s, property_id, cfg)
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
