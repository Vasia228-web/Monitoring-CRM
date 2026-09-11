"""Аналітика окремої квартири: краща вона за ринок чи гірша.

Усе рахується для майстер-об'єкта, а не для того оголошення, яке випадково
відкрили. Це принципово: перша ціна, історія і склад джерел беруться по всіх
склеєних оголошеннях разом.
"""
from __future__ import annotations

import statistics as st
from datetime import datetime, timezone

from sqlalchemy import select

from ..models import Listing, PriceEvent, Property
from .segments import Item, Universe, compare, verdict
from .settings import Settings, load
from .stats import diff_pct
from .survival import Observation, estimate

# Спостереження почалось разом із системою, а не з появою оголошення. Тому
# «перша ціна» — це перша ціна, яку побачили МИ, і підпис має це говорити.
FIRST_PRICE_NOTE = ("Це перша ціна, яку ми побачили, — ми стежимо за цією "
                    "квартирою з {since}. Скільки вона коштувала раніше, "
                    "ми не знаємо.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def price_history(session, property_id: int) -> dict:
    """Історія ціни об'єкта, зібрана по всіх його оголошеннях.

    Тонке місце: зміною ціни є рух ціни ВСЕРЕДИНІ одного оголошення. Поява
    того самого об'єкта на другому майданчику дає новий запис із власною
    базовою ціною, і якщо порівняти його з попереднім записом сусіднього
    майданчика, вийде «стрибок ціни», якого продавець не робив. Тому дельти
    рахуються в межах оголошення, а не по спільній стрічці.

    Перша ціна натомість береться по ВСІХ склеєних оголошеннях разом — саме
    так її й треба розуміти: найраніше, що ми бачили про цю квартиру.
    """
    rows = session.execute(
        select(PriceEvent.observed_at, PriceEvent.price_usd, PriceEvent.source,
               PriceEvent.listing_id, Listing.original_url)
        .join(Listing, Listing.id == PriceEvent.listing_id)
        .where(Listing.property_id == property_id, PriceEvent.price_usd.isnot(None))
        .order_by(PriceEvent.observed_at)).all()
    if not rows:
        return {"points": [], "first": None, "last": None, "change_pct": None,
                "changes": 0, "single_point": True, "tracks": 0, "note": None}

    previous: dict[int, float] = {}
    points = []
    changes = 0
    for at, price, source, listing_id, url in rows:
        before = previous.get(listing_id)
        if before is None:
            kind, delta = "first", None
        else:
            delta = diff_pct(price, before)
            if abs(price - before) > 1:
                kind, changes = "change", changes + 1
            else:
                kind = "same"
        previous[listing_id] = price
        points.append({"at": at, "price": price, "source": source, "url": url,
                       "listing_id": listing_id, "kind": kind, "delta_pct": delta})

    first, last = points[0], points[-1]
    return {
        "points": points, "first": first, "last": last,
        # Сумарна зміна — від найранішої зафіксованої ціни до найпізнішої,
        # як вимагає постановка: по всіх склеєних оголошеннях разом.
        "change_pct": diff_pct(last["price"], first["price"]),
        "changes": changes,
        "tracks": len(previous),
        "single_point": changes == 0,
        "note": FIRST_PRICE_NOTE.format(since=first["at"].strftime("%d.%m.%Y")),
        "multi_note": (
            "Тут зібрані ціни з кількох сайтів. Відсоток показуємо лише тоді, "
            "коли ціну змінили в тому самому оголошенні: поява квартири на "
            "другому сайті — це не зміна ціни продавцем."
        ) if len(previous) > 1 else None,
    }


def cfg_or_default(cfg: Settings | None) -> Settings:
    return cfg or load()


def cross_source(session, property_id: int, cfg_hint: Settings | None = None) -> dict:
    """Розбіжність ціни між майданчиками для одного й того самого об'єкта."""
    rows = session.execute(
        select(Listing.source, Listing.price_usd, Listing.original_url,
               Listing.published_at, Listing.is_active)
        .where(Listing.property_id == property_id,
               Listing.price_usd.isnot(None))).all()
    listings = [{"source": s, "price": p, "url": u, "published_at": pub, "active": a}
                for s, p, u, pub, a in rows]
    prices = [x["price"] for x in listings]
    spread = (round(100 * (max(prices) / min(prices) - 1), 1)
              if len(prices) > 1 and min(prices) else None)
    cfg = cfg_or_default(cfg_hint)
    return {"listings": sorted(listings, key=lambda x: x["price"]),
            "count": len(listings), "spread_pct": spread,
            "min": min(prices) if prices else None,
            "max": max(prices) if prices else None,
            # Велика розбіжність — привід засумніватись у самій склейці, а не
            # подавати її як «продавець просить по-різному».
            "suspect_merge": spread is not None and spread > cfg.merge_suspect_spread,
            "suspect_threshold": cfg.merge_suspect_spread}


def days_on_market(universe: Universe, item: Item, cfg: Settings | None = None) -> dict:
    """Скільки днів об'єкт уже на ринку — проти медіани його сегмента.

    Це «висить стільки-то», а не «продавався стільки-то»: об'єкт ще не
    проданий. Строк продажу — окрема величина, і рахується вона інакше.
    """
    cfg = cfg or load()
    if item.days_listed is None:
        return {"available": False,
                "message": "Невідомо, коли оголошення з'явилось."}
    comparison = compare(universe, item, cfg, value=lambda o: o.days_listed,
                         sided="upper")
    if comparison is None:
        return {"available": False, "days": round(item.days_listed),
                "message": "Схожих квартир у базі надто мало, щоб порівняти."}
    return {
        "available": True,
        "days": round(item.days_listed),
        "median": round(comparison.median),
        "q1": round(comparison.summary.q1), "q3": round(comparison.summary.q3),
        "delta_pct": diff_pct(item.days_listed, comparison.median),
        "n": comparison.n, "label": comparison.label,
        "longer": item.days_listed > comparison.median,
        "note": "Це скільки квартира вже продається, а не скільки "
                "продаватиметься: вона ще в продажу.",
    }


def liquidity(universe: Universe, item: Item, cfg: Settings | None = None) -> dict:
    """Ліквідність сегмента через криву виживання.

    Поки зафіксованих зникнень мало, блок чесно відмовляється рахувати —
    крива по кількох подіях була б плоскою лінією без змісту.
    """
    cfg = cfg or load()
    comparison = compare(universe, item, cfg, value=lambda o: o.days_listed,
                         sided="upper")
    if comparison is None:
        return {"available": False, "events": 0, "needed": cfg.survival_min_events,
                "message": "Схожих квартир надто мало, щоб про це говорити."}
    peers = [o for o in universe.items
             if o.band == item.band and o.condition == item.condition
             and o.market == item.market]
    observations = [Observation(days=obs[0], event=obs[1], entry=obs[2])
                    for o in peers if (obs := o.observation) is not None]
    result = estimate(observations, cfg)
    result["segment"] = comparison.label
    return result


def analyse(session, universe: Universe, property_id: int,
            cfg: Settings | None = None) -> dict | None:
    """Повний набір для сторінки об'єкта."""
    cfg = cfg or load()
    item = next((o for o in universe.items if o.property_id == property_id), None)
    if item is None:
        return None
    prop = session.get(Property, property_id)

    comparison = compare(universe, item, cfg)
    market_verdict = verdict(item, comparison) if comparison else None
    price_comparison = compare(universe, item, cfg, value=lambda o: o.price_usd)

    return {
        "property": prop,
        "item": item,
        "verdict": market_verdict,
        "no_verdict_reason": None if market_verdict else (
            "Схожих квартир у базі надто мало, щоб порівнювати — "
            f"треба хоча б {cfg.min_sample}."),
        "price_band": {
            "median": round(price_comparison.median),
            "q1": round(price_comparison.summary.q1),
            "q3": round(price_comparison.summary.q3),
            "n": price_comparison.n, "label": price_comparison.label,
        } if price_comparison else None,
        "history": price_history(session, property_id),
        "sources": cross_source(session, property_id, cfg),
        "age": days_on_market(universe, item, cfg),
        "liquidity": liquidity(universe, item, cfg),
    }
