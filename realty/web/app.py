"""Веб-інтерфейс: таблиця оголошень із сортуванням і фільтрами."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..db import SessionLocal, init_db
from ..ops import init_ops
from ..models import (
    Condition, Listing, MarketType, PriceEvent, Property, effective_active, is_clean,
)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _relative_date(value) -> str:
    """«сьогодні» / «3 дні тому» / «12.04.2026» — залежно від давності."""
    if value is None:
        return "—"
    days = (datetime.now() - value).days
    if days < 0:
        return value.strftime("%d.%m.%Y")
    if days == 0:
        return "сьогодні"
    if days == 1:
        return "вчора"
    if days < 7:
        return f"{days} дні тому" if days < 5 else f"{days} днів тому"
    if days < 31:
        weeks = days // 7
        return f"{weeks} тиж. тому"
    return value.strftime("%d.%m.%Y")


def _money(value) -> str:
    """Розряди — вузькими нерозривними пробілами, щоб число не ламалось."""
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", "\u202f")


templates.env.filters["relative_date"] = _relative_date
templates.env.filters["money"] = _money

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_ops()
    yield


app = FastAPI(title="Нерухомість Івано-Франківська", docs_url="/api/docs",
              lifespan=lifespan)

# Сторінка стану системи та ручне управління збором.
from .status import router as status_router  # noqa: E402

app.include_router(status_router)

def _num(value: str | None) -> float | None:
    """Порожнє поле форми приходить як `price_min=` — це не число, але й не
    помилка: користувач просто не заповнив фільтр."""
    if value is None or not str(value).strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


SORTS = {
    "price_asc": Listing.price_usd.asc().nullslast(),
    "price_desc": Listing.price_usd.desc().nullslast(),
    "rooms_asc": Listing.rooms.asc().nullslast(),
    "rooms_desc": Listing.rooms.desc().nullslast(),
    "sqm_asc": Listing.price_per_sqm.asc().nullslast(),
    "sqm_desc": Listing.price_per_sqm.desc().nullslast(),
    "date_asc": Listing.published_at.asc().nullslast(),
    "date_desc": Listing.published_at.desc().nullslast(),
}
DEFAULT_SORT = "date_desc"
MAX_ROWS = 1000  # спільна стеля для сторінки та JSON


def _query(session, *, condition: str, market: str, source: str, rooms: str,
           price_min: float | None, price_max: float | None, sort: str,
           status: str = "active", quality: str = "clean"):
    stmt = select(Listing)
    # Записи з невизначеним чи підозрілим статусом не потрапляють у видачу.
    if quality == "clean":
        stmt = stmt.where(is_clean())
    elif quality in ("review", "rejected", "pending"):
        stmt = stmt.where(Listing.quality_status == quality)
    # Типово показуємо лише актуальні: зняті з продажу псують і перегляд,
    # і будь-яку статистику. «all» лишає їх видимими навмисно.
    if status == "active":
        stmt = stmt.where(effective_active().is_(True))
    elif status == "inactive":
        stmt = stmt.where(effective_active().is_(False))
    if condition in {c.value for c in Condition}:
        stmt = stmt.where(Listing.condition == Condition(condition))
    if market in {m.value for m in MarketType}:
        stmt = stmt.where(Listing.market_type == MarketType(market))
    if source:
        stmt = stmt.where(Listing.source == source)
    if rooms:
        if rooms == "4+":
            stmt = stmt.where(Listing.rooms >= 4)
        elif rooms.isdigit():
            stmt = stmt.where(Listing.rooms == int(rooms))
    if price_min is not None:
        stmt = stmt.where(Listing.price_usd >= price_min)
    if price_max is not None:
        stmt = stmt.where(Listing.price_usd <= price_max)
    return stmt.order_by(SORTS.get(sort, SORTS[DEFAULT_SORT]))


def _median(values: list[float]) -> float | None:
    """Медіана, а не середнє: на ринку нерухомості кілька дорогих об'єктів
    зміщують середнє так, що воно перестає описувати типову пропозицію."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _stats(session) -> dict:
    total = session.scalar(select(func.count(Listing.id))) or 0
    by_source = dict(
        session.execute(select(Listing.source, func.count(Listing.id))
                        .group_by(Listing.source)).all()
    )
    # Медіани рахуємо лише по записах, що пройшли контроль: інакше викиди
    # й сміття тягнуть за собою всі оцінки.
    sqm = list(session.scalars(select(Listing.price_per_sqm)
                               .where(Listing.price_per_sqm.isnot(None), is_clean())))
    prices = list(session.scalars(select(Listing.price_usd)
                                  .where(Listing.price_usd.isnot(None), is_clean())))
    median_sqm = _median(sqm)
    updated = session.scalar(select(func.max(Listing.last_seen)))
    inactive = session.scalar(
        select(func.count()).select_from(Listing).where(effective_active().is_(False))
    ) or 0
    properties = session.scalar(select(func.count()).select_from(Property)) or 0
    multi = session.scalar(select(func.count()).select_from(Property)
                           .where(Property.sources_count > 1)) or 0
    quality_counts = dict(session.execute(
        select(Listing.quality_status, func.count()).group_by(Listing.quality_status)).all())
    return {
        "total": total,
        "quality": quality_counts,
        "properties": properties,
        "multi_source": multi,
        "inactive": inactive,
        "by_source": by_source,
        "median_sqm": round(median_sqm) if median_sqm else None,
        "median_price": round(_median(prices)) if prices else None,
        "updated": updated,
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    condition: str = Query("", description="renovated | needs_repair | unknown"),
    market: str = Query(""),
    source: str = Query(""),
    rooms: str = Query(""),
    price_min: str | None = Query(None),
    price_max: str | None = Query(None),
    sort: str = Query(DEFAULT_SORT),
    status: str = Query("active", description="active | inactive | all"),
    quality: str = Query("clean", description="clean | review | rejected | all"),
    limit: int = Query(500, le=MAX_ROWS),
):
    price_min, price_max = _num(price_min), _num(price_max)
    with SessionLocal() as s:
        stmt = _query(s, condition=condition, market=market, source=source, rooms=rooms,
                      price_min=price_min, price_max=price_max, sort=sort, status=status,
                      quality=quality)
        # Скільки збігів насправді — щоб у зведенні не показувати ліміт сторінки
        # як «стільки знайдено».
        matched = s.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ) or 0
        rows = s.scalars(stmt.limit(limit)).all()
        stats = _stats(s)
        sources = sorted(stats["by_source"])
        active = any((condition, market, source, rooms, price_min, price_max)) \
            or status != "active" or quality != "clean"
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows, "stats": stats, "sources": sources, "active_filters": active,
        "matched": matched, "limit": limit,
        "f": {"condition": condition, "market": market, "source": source, "rooms": rooms,
              "price_min": price_min or "", "price_max": price_max or "", "sort": sort,
              "status": status, "quality": quality},
        "Condition": Condition, "MarketType": MarketType,
    })


@app.get("/api/listings")
def api_listings(
    condition: str = "", market: str = "", source: str = "", rooms: str = "",
    price_min: str | None = None, price_max: str | None = None,
    sort: str = DEFAULT_SORT, status: str = "active", quality: str = "clean",
    limit: int = Query(500, le=MAX_ROWS),
):
    """JSON-зріз тих самих даних."""
    price_min, price_max = _num(price_min), _num(price_max)
    with SessionLocal() as s:
        stmt = _query(s, condition=condition, market=market, source=source, rooms=rooms,
                      price_min=price_min, price_max=price_max, sort=sort, status=status,
                      quality=quality)
        rows = s.scalars(stmt.limit(limit)).all()
        return JSONResponse([{
            "source": r.source,
            "price": r.price,
            "currency": r.currency,
            "price_usd": r.price_usd,
            "rooms": r.rooms,
            "area_total": r.area_total,
            "location": r.location,
            "price_per_sqm": r.price_per_sqm,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "market_type": r.market_type.value,
            "condition": r.condition.value,
            "original_url": r.original_url,
            "price_estimated": r.price_estimated,
            "is_active": r.is_active,
            "manual_active": r.manual_active,
            "active": r.manual_active if r.manual_active is not None else r.is_active,
            "delisted_at": r.delisted_at.isoformat() if r.delisted_at else None,
            "quality_status": r.quality_status,
            "quality_reason": r.quality_reason,
        } for r in rows])


@app.post("/api/listings/{listing_id}/status")
def api_set_status(listing_id: int, payload: dict = Body(default={})):
    """Ручна позначка актуальності.

    `active`: true — актуальна, false — неактуальна, null — зняти позначку
    й повернутись до автоматичного визначення.
    """
    value = payload.get("active", None)
    if value not in (True, False, None):
        return JSONResponse({"ok": False, "error": "active має бути true, false або null"},
                            status_code=400)
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "оголошення не знайдено"},
                                status_code=404)
        row.manual_active = value
        s.commit()
        return JSONResponse({
            "ok": True, "id": row.id, "manual_active": row.manual_active,
            "is_active": row.is_active,
            "active": row.manual_active if row.manual_active is not None else row.is_active,
        })


@app.get("/api/properties")
def api_properties(
    min_sources: int = Query(1, ge=1, description="лише об'єкти на N+ майданчиках"),
    condition: str = "", market: str = "", rooms: str = "",
    limit: int = Query(200, le=MAX_ROWS),
):
    """Майстер-записи: один об'єкт — один рядок із посиланнями на всі оголошення."""
    with SessionLocal() as s:
        stmt = select(Property).where(Property.sources_count >= min_sources)
        if condition in {c.value for c in Condition}:
            stmt = stmt.where(Property.condition == Condition(condition))
        if market in {m.value for m in MarketType}:
            stmt = stmt.where(Property.market_type == MarketType(market))
        if rooms == "4+":
            stmt = stmt.where(Property.rooms >= 4)
        elif rooms.isdigit():
            stmt = stmt.where(Property.rooms == int(rooms))
        rows = s.scalars(stmt.order_by(Property.sources_count.desc(),
                                       Property.last_seen.desc()).limit(limit)).all()
        return JSONResponse([{
            "id": p.id,
            "rooms": p.rooms,
            "area_total": p.area_total,
            "floor": p.floor,
            "floors_total": p.floors_total,
            "location": p.location,
            "street": p.street,
            "district": p.district,
            "price_usd_min": p.price_usd_min,
            "price_usd_max": p.price_usd_max,
            "price_per_sqm": p.price_per_sqm,
            "market_type": p.market_type.value,
            "condition": p.condition.value,
            "sources_count": p.sources_count,
            # Масив посилань на всі оригінальні оголошення цього об'єкта.
            "sources": [{"source": l.source, "url": l.original_url,
                         "price_usd": l.price_usd,
                         "published_at": l.published_at.isoformat() if l.published_at else None}
                        for l in p.listings],
        } for p in rows])


@app.get("/api/properties/{property_id}/prices")
def api_property_prices(property_id: int):
    """Єдина історія зміни ціни об'єкта — злита з усіх його оголошень."""
    with SessionLocal() as s:
        prop = s.get(Property, property_id)
        if prop is None:
            return JSONResponse({"detail": "не знайдено"}, status_code=404)
        ids = [l.id for l in prop.listings]
        events = s.scalars(
            select(PriceEvent).where(PriceEvent.listing_id.in_(ids))
            .order_by(PriceEvent.observed_at)
        ).all()
        return JSONResponse({
            "property_id": property_id,
            "sources_count": prop.sources_count,
            "history": [{"observed_at": e.observed_at.isoformat(), "source": e.source,
                         "price": e.price, "currency": e.currency,
                         "price_usd": e.price_usd} for e in events],
        })


@app.get("/api/stats")
def api_stats():
    with SessionLocal() as s:
        return _stats(s)
