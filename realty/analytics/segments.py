"""Сегментна статистика: з чим саме порівнювати конкретну квартиру.

Сегмент нарізається швидко — кімнатність × стан × ринок × район × смуга площі
дає сотні комбінацій, і в більшості з них буде по два об'єкти. Тому тут
працює драбина: беремо найвужчий сегмент, а якщо в ньому замало об'єктів —
розширюємо і чесно підписуємо, на якому рівні порівняли.
"""
from __future__ import annotations

import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Condition, Listing, MarketType, PriceEvent, Property
from .settings import Settings, load
from .stats import Summary, diff_pct, percentile_of, summarise

COND_LABEL = {"renovated": "з ремонтом", "needs_repair": "без ремонту",
              "unknown": "стан не визначено"}
MARKET_LABEL = {"primary": "новобудова", "secondary": "вторинка",
                "unknown": "ринок не визначено"}


def rooms_band(rooms: int | None) -> int | None:
    """4к, 5к і більші зводимо в одну смугу: окремо це вибірки по кілька штук."""
    if rooms is None:
        return None
    return rooms if rooms < 4 else 4


def rooms_label(band: int | None) -> str:
    if band is None:
        return "кімнатність невідома"
    return "3+ кімнат" if band == 4 else f"{band}-кімнатні"


@dataclass
class Item:
    """Один майстер-об'єкт у тому вигляді, в якому його бачить аналітика.

    `price_usd` — медіана цін склеєних оголошень, а не мінімум і не максимум.
    Медіана і тому, що це головне правило всіх розрахунків тут, і тому, що
    ціна за м² виводиться з неї ж: інакше на сторінці об'єкта стояли б ціна з
    одного оголошення й ціна за м² з іншого, які між собою не сходяться.
    """

    property_id: int
    rooms: int | None
    area: float | None
    price_usd: float | None
    ppsqm: float | None
    condition: str
    market: str
    district: str | None
    days_listed: float | None      # від публікації найранішого оголошення
    delisted_at: datetime | None
    sources: tuple[str, ...] = ()
    price_min: float | None = None
    price_max: float | None = None
    # Скільки об'єкт прожив на ринку, якщо він уже зник. Точної дати зняття не
    # існує: ми знаємо лише проміжок між останньою перевіркою «живе» і першою
    # «мертве». Беремо середину проміжку й окремо тримаємо його ширину — вона
    # показує, наскільки груба ця оцінка.
    lifetime_days: float | None = None
    interval_days: float | None = None
    price_drops: int = 0
    price_drop_pct: float | None = None

    # Вік оголошення на момент, коли ми його вперше побачили. Об'єкт входить
    # у спостереження саме з цього віку, а не з нуля.
    entry_days: float | None = None

    @property
    def observation(self) -> tuple[float, bool, float] | None:
        """(скільки тривало, чи завершилось, з якого віку спостерігали).

        Для зниклого об'єкта тривалість — строк ДО зникнення, для активного —
        вік дотепер. Взяти вік дотепер для зниклого означало б додати до
        строку продажу час, коли квартира вже не продавалась.
        """
        entry = max(0.0, self.entry_days or 0.0)
        if self.delisted_at is not None:
            if self.lifetime_days is None:
                return None
            return (self.lifetime_days, True, min(entry, self.lifetime_days))
        if self.days_listed is None:
            return None
        return (self.days_listed, False, min(entry, self.days_listed))

    @property
    def price_spread_pct(self) -> float | None:
        """Наскільки розходяться ціни майданчиків на цей самий об'єкт."""
        if not self.price_min or not self.price_max or self.price_min <= 0:
            return None
        return round(100 * (self.price_max / self.price_min - 1), 1)

    @property
    def band(self) -> int | None:
        return rooms_band(self.rooms)


@dataclass
class Universe:
    """Знімок бази для аналітики.

    Уся аналітика рахується по цьому знімку, а не окремими запитами на кожен
    блок сторінки: 7 тисяч об'єктів вміщаються в пам'ять, і це дешевше, ніж
    десятки агрегувальних запитів на кожне відкриття сторінки.
    """

    items: list[Item] = field(default_factory=list)
    built_at: datetime | None = None

    def __len__(self) -> int:
        return len(self.items)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _price_moves(session) -> dict[int, tuple[int, float | None]]:
    """Скільки разів ціна об'єкта падала і наскільки глибоко сумарно.

    Рахується в межах одного оголошення: поява того самого об'єкта на другому
    майданчику дає новий запис, але це не рух ціни продавця.
    """
    rows = session.execute(
        select(Listing.property_id, PriceEvent.listing_id, PriceEvent.price_usd,
               PriceEvent.observed_at)
        .join(Listing, Listing.id == PriceEvent.listing_id)
        .where(Listing.property_id.isnot(None), PriceEvent.price_usd.isnot(None))
        .order_by(PriceEvent.listing_id, PriceEvent.observed_at)).all()

    previous: dict[int, float] = {}
    drops: dict[int, int] = defaultdict(int)
    first: dict[int, float] = {}
    last: dict[int, float] = {}
    for prop_id, listing_id, price, _ in rows:
        before = previous.get(listing_id)
        if before is not None and price < before - 1:
            drops[prop_id] += 1
        previous[listing_id] = price
        first.setdefault(prop_id, price)
        last[prop_id] = price

    out: dict[int, tuple[int, float | None]] = {}
    for prop_id, count in drops.items():
        start, end = first.get(prop_id), last.get(prop_id)
        depth = round(100 * (1 - end / start), 1) if start and end and start > 0 else None
        out[prop_id] = (count, depth)
    return out


def build_universe(session) -> Universe:
    """Збирає знімок: один майстер-об'єкт — один рядок."""
    props = session.execute(
        select(Property.id, Property.rooms, Property.area_total, Property.price_usd_min,
               Property.price_per_sqm, Property.condition, Property.market_type,
               Property.district)).all()

    # Дату публікації і джерела беремо з оголошень: у майстер-записі їх немає,
    # а «скільки днів на ринку» рахується від найранішої публікації серед усіх
    # склеєних оголошень — інакше переклеєне оголошення виглядало б новим.
    listings = session.execute(
        select(Listing.property_id, Listing.published_at, Listing.source,
               Listing.delisted_at, Listing.price_usd, Listing.last_alive_at,
               Listing.first_seen)
        .where(Listing.property_id.isnot(None))).all()
    published: dict[int, datetime] = {}
    delisted: dict[int, datetime] = {}
    last_alive: dict[int, datetime] = {}
    first_seen_at: dict[int, datetime] = {}
    sources: dict[int, set[str]] = {}
    prices: dict[int, list[float]] = {}
    alive: set[int] = set()
    for prop_id, pub, source, gone, price, seen_alive, first_seen in listings:
        if price:
            prices.setdefault(prop_id, []).append(price)
        # Нижня межа проміжку: остання перевірка «живе», а якщо її не було —
        # момент, коли ми оголошення вперше побачили.
        bound = seen_alive or first_seen
        if bound and (prop_id not in last_alive or bound > last_alive[prop_id]):
            last_alive[prop_id] = bound
        if first_seen and (prop_id not in first_seen_at
                           or first_seen < first_seen_at[prop_id]):
            first_seen_at[prop_id] = first_seen
        if pub and (prop_id not in published or pub < published[prop_id]):
            published[prop_id] = pub
        sources.setdefault(prop_id, set()).add(source)
        if gone is None:
            alive.add(prop_id)
        elif prop_id not in delisted or gone > delisted[prop_id]:
            delisted[prop_id] = gone

    price_moves = _price_moves(session)
    now = _now()
    items = []
    for pid, rooms, area, stored_price, stored_ppsqm, cond, market, district in props:
        pub = published.get(pid)
        # Об'єкт вважаємо знятим лише тоді, коли зникли ВСІ його оголошення.
        gone = None if pid in alive else delisted.get(pid)
        own = prices.get(pid) or ([stored_price] if stored_price else [])
        price = st.median(own) if own else None
        lifetime = interval = entry = None
        if gone and pub:
            lower = last_alive.get(pid)
            # Середина проміжку — звичайна практика для інтервально
            # цензурованих спостережень: вона не вдає точності, якої немає.
            moment = (lower + (gone - lower) / 2) if lower and lower < gone else gone
            lifetime = (moment - pub).total_seconds() / 86400
            interval = ((gone - lower).total_seconds() / 86400) if lower else None
        seen_first = first_seen_at.get(pid)
        if pub and seen_first:
            entry = max(0.0, (seen_first - pub).total_seconds() / 86400)
        drops, depth = price_moves.get(pid, (0, None))
        # Ціна за м² виводиться з тієї самої ціни, що показана поруч. Значення
        # з майстер-запису лишається запасним варіантом, коли площі немає.
        ppsqm = (price / area) if price and area else stored_ppsqm
        items.append(Item(
            property_id=pid, rooms=rooms, area=area, price_usd=price, ppsqm=ppsqm,
            condition=cond.value, market=market.value, district=district,
            days_listed=(now - pub).total_seconds() / 86400 if pub else None,
            delisted_at=gone, sources=tuple(sorted(sources.get(pid, ()))),
            price_min=min(own) if own else None, price_max=max(own) if own else None,
            lifetime_days=lifetime, interval_days=interval, entry_days=entry,
            price_drops=drops, price_drop_pct=depth,
        ))
    return Universe(items=items, built_at=now)


# --- Драбина сегментів --------------------------------------------------------

@dataclass
class Level:
    """Один щабель драбини: наскільки вузько порівнюємо."""

    key: str
    label: str
    match: object          # callable(Item) -> bool


def ladder(item: Item, cfg: Settings) -> list[Level]:
    """Щаблі від найвужчого до найширшого — для конкретного об'єкта.

    Порядок розширення не випадковий. Першою відпускаємо площу: вона впливає
    на ціну за м² помітно, але слабше за кімнатність і стан. Далі район — він
    відомий лише для 83% об'єктів і швидко залишає вибірку без даних. Стан і
    ринок тримаємо до останнього: новобудова-сирець і вторинка з ремонтом —
    це різні ринки, зводити їх разом безглуздо.
    """
    band = item.band
    area = item.area

    def area_match(width: float):
        if area is None:
            return lambda o: True
        lo, hi = area * (1 - width), area * (1 + width)
        return lambda o: o.area is not None and lo <= o.area <= hi

    def base(o: Item) -> bool:
        return (o.band == band and o.condition == item.condition
                and o.market == item.market)

    def with_district(o: Item) -> bool:
        return base(o) and o.district == item.district

    narrow, wide = area_match(cfg.area_band_pct), area_match(cfg.area_band_max_pct)
    rooms_only = lambda o: o.band == band                      # noqa: E731
    rooms_market = lambda o: o.band == band and o.market == item.market  # noqa: E731

    levels: list[Level] = []
    if item.district and area is not None:
        levels.append(Level(
            "district_area", f"{rooms_label(band)}, {COND_LABEL[item.condition]}, "
            f"{MARKET_LABEL[item.market]}, {item.district}, "
            f"площа {area * (1 - cfg.area_band_pct):.0f}–{area * (1 + cfg.area_band_pct):.0f} м²",
            lambda o: with_district(o) and narrow(o)))
    if item.district:
        levels.append(Level(
            "district", f"{rooms_label(band)}, {COND_LABEL[item.condition]}, "
            f"{MARKET_LABEL[item.market]}, {item.district}", with_district))
    if area is not None:
        levels.append(Level(
            "area", f"{rooms_label(band)}, {COND_LABEL[item.condition]}, "
            f"{MARKET_LABEL[item.market]}, площа "
            f"{area * (1 - cfg.area_band_pct):.0f}–{area * (1 + cfg.area_band_pct):.0f} м²",
            lambda o: base(o) and narrow(o)))
        levels.append(Level(
            "area_wide", f"{rooms_label(band)}, {COND_LABEL[item.condition]}, "
            f"{MARKET_LABEL[item.market]}, площа "
            f"{area * (1 - cfg.area_band_max_pct):.0f}–"
            f"{area * (1 + cfg.area_band_max_pct):.0f} м²",
            lambda o: base(o) and wide(o)))
    levels.append(Level(
        "base", f"{rooms_label(band)}, {COND_LABEL[item.condition]}, "
        f"{MARKET_LABEL[item.market]}", base))
    levels.append(Level(
        "rooms_market", f"{rooms_label(band)}, {MARKET_LABEL[item.market]}", rooms_market))
    levels.append(Level("rooms", rooms_label(band), rooms_only))
    return levels


@dataclass
class Comparison:
    """Результат порівняння об'єкта із сегментом."""

    level: str
    label: str
    n: int
    summary: Summary
    peers: list[float]

    @property
    def median(self) -> float:
        return self.summary.median


def compare(universe: Universe, item: Item, cfg: Settings | None = None,
            value=lambda o: o.ppsqm, sided: str = "both") -> Comparison | None:
    """Спускається драбиною, доки не набереться достатня вибірка.

    Повертає None, якщо навіть найширший щабель замалий — це штатний
    результат, а не помилка: у деяких об'єктів просто немає з чим порівнювати.
    """
    cfg = cfg or load()
    for level in ladder(item, cfg):
        peers = [v for o in universe.items
                 if o.property_id != item.property_id and level.match(o)
                 and (v := value(o)) is not None]
        if len(peers) < cfg.min_sample:
            continue
        summary = summarise(peers, cfg, sided)
        if summary is None:
            continue
        kept = sorted(v for v in peers
                      if summary.low is None or summary.low <= v <= summary.high)
        return Comparison(level=level.key, label=level.label, n=summary.n,
                          summary=summary, peers=kept)
    return None


def verdict(item: Item, comparison: Comparison) -> dict | None:
    """Головна відповідь: дорожче чи дешевше за схожі об'єкти і на скільки."""
    if item.ppsqm is None:
        return None
    delta = diff_pct(item.ppsqm, comparison.median)
    if delta is None:
        return None
    return {
        "delta_pct": delta,
        "percentile": percentile_of(item.ppsqm, comparison.peers),
        "direction": "дорожче" if delta > 0 else "дешевше" if delta < 0 else "як ринок",
        "median": comparison.median,
        "n": comparison.n,
        "label": comparison.label,
        "level": comparison.level,
    }


# --- Зведення по сегментах для сторінки «Прогнози» ----------------------------

def segment_table(universe: Universe, cfg: Settings | None = None,
                  *, rooms: str = "", condition: str = "", market: str = "",
                  district: str = "") -> list[dict]:
    """Медіани по всіх сегментах, що набирають поріг вибірки.

    Сегменти, які поріг не набрали, у таблицю не потрапляють — але їх число
    повертається окремо, щоб сторінка могла чесно написати, скільки об'єктів
    лишилось поза статистикою.
    """
    cfg = cfg or load()
    groups: dict[tuple, list[Item]] = {}
    for o in universe.items:
        if o.ppsqm is None:
            continue
        if rooms and str(o.band or "") != rooms:
            continue
        if condition and o.condition != condition:
            continue
        if market and o.market != market:
            continue
        if district and o.district != district:
            continue
        groups.setdefault((o.band, o.condition, o.market), []).append(o)

    rows = []
    for (band, cond, mk), members in groups.items():
        summary = summarise([o.ppsqm for o in members], cfg)
        if summary is None or summary.n < cfg.min_sample:
            continue
        prices = summarise([o.price_usd for o in members if o.price_usd], cfg)
        listed = [o.days_listed for o in members if o.days_listed is not None]
        rows.append({
            "rooms": band, "rooms_label": rooms_label(band),
            "condition": cond, "condition_label": COND_LABEL[cond],
            "market": mk, "market_label": MARKET_LABEL[mk],
            "n": summary.n, "n_raw": summary.n_raw, "trimmed": summary.trimmed,
            "median_ppsqm": round(summary.median),
            "mean_ppsqm": round(summary.mean),
            "q1": round(summary.q1), "q3": round(summary.q3),
            "median_price": round(prices.median) if prices else None,
            "median_days": round(st.median(listed)) if listed else None,
            "days_n": len(listed),
        })
    return sorted(rows, key=lambda r: -r["n"])


def below_threshold(universe: Universe, cfg: Settings | None = None) -> dict:
    """Скільки об'єктів лишилось поза статистикою через замалі сегменти."""
    cfg = cfg or load()
    groups: dict[tuple, int] = {}
    for o in universe.items:
        if o.ppsqm is None:
            continue
        groups[(o.band, o.condition, o.market)] = groups.get((o.band, o.condition, o.market), 0) + 1
    small = {k: v for k, v in groups.items() if v < cfg.min_sample}
    return {"segments": len(small), "objects": sum(small.values()),
            "threshold": cfg.min_sample}


def primary_vs_secondary(universe: Universe, cfg: Settings | None = None) -> list[dict]:
    """Новобудова проти вторинки — у межах однакової кімнатності й стану.

    Порівнювати «всю новобудову» з «усією вторинкою» немає сенсу з тієї самої
    причини, що й порівнювати джерела навпростець: набори різні за складом.
    Тут пара будується тільки тоді, коли обидві сторони набирають поріг.
    """
    cfg = cfg or load()
    groups: dict[tuple, dict[str, list[float]]] = {}
    for o in universe.items:
        if o.ppsqm is None or o.market not in ("primary", "secondary"):
            continue
        groups.setdefault((o.band, o.condition), {}).setdefault(o.market, []).append(o.ppsqm)

    rows = []
    for (band, cond), sides in groups.items():
        cells = {}
        for market, values in sides.items():
            summary = summarise(values, cfg)
            if summary is None or summary.n < cfg.min_sample:
                continue
            cells[market] = {"n": summary.n, "median": round(summary.median),
                             "q1": round(summary.q1), "q3": round(summary.q3)}
        if len(cells) < 2:
            continue
        rows.append({
            "rooms": band, "rooms_label": rooms_label(band),
            "condition": cond, "condition_label": COND_LABEL[cond],
            "primary": cells["primary"], "secondary": cells["secondary"],
            "gap_pct": diff_pct(cells["primary"]["median"], cells["secondary"]["median"]),
        })
    return sorted(rows, key=lambda r: (r["rooms"] or 0, r["condition"]))


def days_distribution(universe: Universe, cfg: Settings | None = None) -> list[dict]:
    """Скільки днів об'єкти сегмента вже на ринку.

    Це не строк продажу — об'єкти ще не продані. Але на питання «цей висить
    довше чи менше за схожі» відповідає вже сьогодні, на відміну від кривої
    виживання, якій потрібні зафіксовані зникнення.
    """
    cfg = cfg or load()
    groups: dict[tuple, list[float]] = {}
    for o in universe.items:
        if o.days_listed is None:
            continue
        groups.setdefault((o.band, o.condition, o.market), []).append(o.days_listed)

    rows = []
    for (band, cond, market), values in groups.items():
        summary = summarise(values, cfg, "upper")
        if summary is None or summary.n < cfg.min_sample:
            continue
        rows.append({
            "rooms": band, "rooms_label": rooms_label(band),
            "condition_label": COND_LABEL[cond], "market_label": MARKET_LABEL[market],
            "n": summary.n, "median": round(summary.median),
            "q1": round(summary.q1), "q3": round(summary.q3),
        })
    return sorted(rows, key=lambda r: r["median"])


def liquidity_proxy(universe: Universe, cfg: Settings | None = None) -> list[dict]:
    """Ліквідність сегментів за тим, що вимірне вже сьогодні.

    Крива виживання потребує накопичених зникнень і з'являється пізніше. Але
    два спостережні показники доступні одразу і відповідають на те саме
    питання «що йде важче»:

      * частка об'єктів, які знижували ціну, і глибина зниження — квартири,
        які скидають ціну, продаються гірше;
      * вік активних оголошень — де він більший, там ринок густіший.

    Це саме проксі, а не строк продажу: жодне з цих чисел не каже, скільки
    триває продаж. Підпис про це має лишатися поруч із цифрами.
    """
    cfg = cfg or load()
    groups: dict[tuple, list[Item]] = {}
    for o in universe.items:
        if o.days_listed is None:
            continue
        groups.setdefault((o.band, o.condition, o.market), []).append(o)

    rows = []
    for (band, cond, market), members in groups.items():
        ages = summarise([o.days_listed for o in members], cfg, "upper")
        if ages is None or ages.n < cfg.min_sample:
            continue
        with_drop = [o for o in members if o.price_drops]
        depths = [o.price_drop_pct for o in with_drop if o.price_drop_pct]
        gone = [o for o in members if o.delisted_at is not None]
        rows.append({
            "rooms": band, "rooms_label": rooms_label(band),
            "condition_label": COND_LABEL[cond], "market_label": MARKET_LABEL[market],
            "n": len(members),
            "median_age": round(ages.median),
            "age_q1": round(ages.q1), "age_q3": round(ages.q3),
            "drop_share": round(100 * len(with_drop) / len(members), 1),
            "drop_depth": round(st.median(depths), 1) if depths else None,
            "gone": len(gone),
            "gone_share": round(100 * len(gone) / len(members), 1),
        })
    # Важче йде те, де більше знижень ціни; за рівності — де старші оголошення.
    return sorted(rows, key=lambda r: (-r["drop_share"], -r["median_age"]))
