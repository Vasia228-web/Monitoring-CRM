"""Сегментна статистика: з чим саме порівнювати конкретну квартиру.

Сегмент нарізається швидко — кімнатність × стан × ринок × район × смуга площі
дає сотні комбінацій, і в більшості з них буде по два об'єкти. Тому тут
працює драбина: беремо найвужчий сегмент, а якщо в ньому замало об'єктів —
розширюємо і чесно підписуємо, на якому рівні порівняли.
"""
from __future__ import annotations

import logging
import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from .. import configfiles
from ..db import SessionLocal
from ..models import Condition, Listing, MarketType, PriceEvent, Property
from .memo import Memo, curve_capacity
from .settings import Settings, load
from .stats import Summary, diff_pct, percentile_of, summarise
from .survival import Observation, estimate

log = logging.getLogger(__name__)

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
    # Покажчики й пам'ять знімка (Блок 2, D48). Сторінка квартири шукала свій
    # об'єкт і «схожі» повним проходом по 15 тис. об'єктів на кожен щабель
    # драбини — 75–114 мс на M4. Покажчики будуються з `items` один раз;
    # пам'ять (`memo.Memo`) тримає пораховане для цього знімка. У порівняння
    # й repr не входять: це похідне від items, а не дані.
    _index: tuple | None = field(default=None, init=False, repr=False, compare=False)
    curves: Memo = field(default_factory=Memo, init=False, repr=False, compare=False)
    parts_memo: Memo = field(default_factory=Memo, init=False, repr=False, compare=False)

    def __len__(self) -> int:
        return len(self.items)

    def reset_derived(self) -> None:
        """Забути все, що пораховано з `items`: покажчики, криві, частини сторінки.

        ОБОВ'ЯЗКОВО для коду, що змінює `items` живого знімка на місці (крок
        E5 Блоку 2: оновлення квартир після «розділити/злити»). Покажчики
        самі помічають лише підміну чи зміну довжини списку; заміна елемента
        `items[i] = …` тієї самої довжини і пам'ять кривих та частин цього не
        помічають — без виклику наступне відкриття сторінки показало б старе.
        """
        self._index = None
        self.curves = Memo()
        self.parts_memo = Memo()

    def _indexes(self) -> tuple[dict[int, Item], dict[int | None, list[Item]]]:
        # Покажчики перебудовуються, якщо список items підмінили чи доповнили
        # (так роблять тести); заміну елемента на місці бачить лише
        # reset_derived(). Порядок у кожному кошику — як в items: від
        # нього залежать суми з плаваючою комою в статистиці «схожих».
        key = (id(self.items), len(self.items))
        index = self._index
        if index is None or index[0] != key:
            by_id: dict[int, Item] = {}
            by_band: dict[int | None, list[Item]] = {}
            for o in self.items:
                by_id.setdefault(o.property_id, o)      # перший, як раніше next(...)
                by_band.setdefault(o.band, []).append(o)
            index = self._index = (key, by_id, by_band)
        return index[1], index[2]

    @property
    def by_id(self) -> dict[int, Item]:
        """property_id → об'єкт (замість пошуку проходом по всіх)."""
        return self._indexes()[0]

    @property
    def by_band(self) -> dict[int | None, list[Item]]:
        """Смуга кімнатності → об'єкти цієї смуги в порядку `items`."""
        return self._indexes()[1]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _price_moves(session, property_ids=None) -> dict[int, tuple[int, float | None]]:
    """Скільки разів ціна об'єкта падала і наскільки глибоко сумарно.

    Рахується в межах одного оголошення: поява того самого об'єкта на другому
    майданчику дає новий запис, але це не рух ціни продавця.

    Останній ключ порядку — id події (Блок 2, D49): дві події одного
    оголошення з однаковим часом інакше йшли б у порядку плану запиту. На
    копії Етапу 0 таких 0, і індекс (listing_id, observed_at) дає цей самий
    порядок без сортування (rowid — неявна остання колонка індексу).
    """
    stmt = (select(Listing.property_id, PriceEvent.listing_id, PriceEvent.price_usd,
                   PriceEvent.observed_at)
            .join(Listing, Listing.id == PriceEvent.listing_id)
            .where(Listing.property_id.isnot(None), PriceEvent.price_usd.isnot(None))
            .order_by(PriceEvent.listing_id, PriceEvent.observed_at, PriceEvent.id))
    if property_ids is not None:
        stmt = stmt.where(Listing.property_id.in_(property_ids))
    rows = session.execute(stmt).all()

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
    """Збирає знімок: один майстер-об'єкт — один рядок.

    Порядок рядків задано явно (ORDER BY id), а не залишено планувальнику:
    від порядку об'єктів залежать порядок рядків у таблицях і суми з
    плаваючою комою в статистиці «схожих». Досі це був порядок повного
    проходу таблиці (за rowid = id) — той самий, що дає ORDER BY id; але
    новий індекс міг би непомітно його змінити (Блок 2, D48: так у прототипі
    перескочила позначка «найдешевше» в порівнянні джерел).
    """
    now = _now()
    return Universe(items=build_items(session, None, now=now), built_at=now)


def build_items(session, property_ids, *, now: datetime) -> list[Item]:
    """Рядки знімка для квартир `property_ids` (None — для всіх), у порядку id.

    Той самий розрахунок, що й для повного знімка: кожне поле квартири
    залежить лише від її власних оголошень і подій ціни. Тому точкове
    оновлення знімка після «розділити/злити» (Блок 2, крок E5, D50) дає ті
    самі рядки, що й повна перебудова з тим самим `now` (тест
    test_universe_patch_equals_rebuild). Квартири, якої вже немає, у
    результаті немає.
    """
    ids = None if property_ids is None else sorted(set(property_ids))
    props_stmt = (select(Property.id, Property.rooms, Property.area_total,
                         Property.price_usd_min, Property.price_per_sqm, Property.condition,
                         Property.market_type, Property.district).order_by(Property.id))
    # Дату публікації і джерела беремо з оголошень: у майстер-записі їх немає,
    # а «скільки днів на ринку» рахується від найранішої публікації серед усіх
    # склеєних оголошень — інакше переклеєне оголошення виглядало б новим.
    listings_stmt = (select(Listing.property_id, Listing.published_at, Listing.source,
                            Listing.delisted_at, Listing.price_usd, Listing.last_alive_at,
                            Listing.first_seen, Listing.source_removed_at)
                     .where(Listing.property_id.isnot(None)).order_by(Listing.id))
    if ids is not None:
        props_stmt = props_stmt.where(Property.id.in_(ids))
        listings_stmt = listings_stmt.where(Listing.property_id.in_(ids))
    props = session.execute(props_stmt).all()
    listings = session.execute(listings_stmt).all()
    published: dict[int, datetime] = {}
    delisted: dict[int, datetime] = {}
    last_alive: dict[int, datetime] = {}
    first_seen_at: dict[int, datetime] = {}
    sources: dict[int, set[str]] = {}
    prices: dict[int, list[float]] = {}
    alive: set[int] = set()
    for prop_id, pub, source, gone, price, seen_alive, first_seen, src_gone in listings:
        if price:
            prices.setdefault(prop_id, []).append(price)
        # Нижня межа проміжку: остання перевірка «живе», а якщо її не було —
        # момент, коли ми оголошення вперше побачили.
        bound = seen_alive or first_seen
        # Дата зняття на самому джерелі (DOM.RIA deleted_at, Блок 1, E8, D52): зняте
        # раніше, ніж ми це виявили, жило до неї, а не до нашої перевірки. Якщо
        # джерело зняло раніше за наше «бачили живим», та стара перевірка (HEAD,
        # сліпий до банера) була хибною — інтервал стискається до точки. Повернене
        # оголошення (delisted_at = NULL) — цензуроване спостереження, не подія.
        # Лише в межах НАШОГО спостереження рядка: дата джерела раніша за першу
        # зустріч (копію LUN підхопили, коли DOM.RIA вже зняв) дала б зникнення
        # раніше за вхід у спостереження — подію, що ніколи не була під ризиком
        # (у Каплана—Меєра S ≤ 0; рецензія E8, D52). Тоді — наші дати, як були.
        if gone is not None and src_gone is not None and (
                first_seen is None or src_gone >= first_seen):
            gone = min(gone, src_gone)
            bound = min(bound, src_gone) if bound else src_gone
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

    price_moves = _price_moves(session, ids)
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
    return items


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

    Кожен щабель вимагає тієї самої смуги кімнатності (o.band == item.band):
    на цьому стоїть пошук «схожих» лише в кошику смуги (`compare`). Новий
    щабель без цієї умови — лише разом зі зміною `compare`.
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
    # Кожен щабель драбини вимагає тієї самої смуги кімнатності (o.band ==
    # item.band), тож шукати «схожих» досить у кошику цієї смуги, а не серед
    # усіх об'єктів бази. Кошик зберігає порядок `universe.items`, тому список
    # схожих і суми над ним ті самі до біта (Блок 2, D48).
    pool = universe.by_band.get(item.band, ())
    for level in ladder(item, cfg):
        peers = [v for o in pool
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


def _by(universe: Universe, key, cfg: Settings, value=None) -> list[dict]:
    """Групує об'єкти однією ознакою й рахує типову ціну за м².

    Для сторінки, де кожен графік має нести одну думку, зрізи потрібні
    прості: окремо кімнатність, окремо ремонт, окремо ринок. Складені
    сегменти лишаються для порівняння конкретної квартири.
    """
    value = value or (lambda o: o.ppsqm)
    groups: dict[str, list[float]] = {}
    for o in universe.items:
        got = value(o)
        if got is None:
            continue
        label = key(o)
        if label is None:
            continue
        groups.setdefault(label, []).append(got)

    rows = []
    for label, values in groups.items():
        summary = summarise(values, cfg)
        if summary is None or summary.n < cfg.min_sample:
            continue
        rows.append({"label": label, "n": summary.n,
                     "typical": round(summary.median),
                     "low": round(summary.q1), "high": round(summary.q3)})
    return rows


def by_rooms(universe: Universe, cfg: Settings | None = None) -> list[dict]:
    """Ціна метра за кімнатністю — одна думка, три-чотири стовпчики."""
    cfg = cfg or load()
    rows = _by(universe, lambda o: rooms_label(o.band) if o.band else None, cfg)
    order = {rooms_label(b): b for b in (1, 2, 3, 4)}
    return sorted(rows, key=lambda r: order.get(r["label"], 9))


def by_condition(universe: Universe, cfg: Settings | None = None) -> list[dict]:
    """Що дає ремонт. Порівнюємо лише там, де решта умов однакова."""
    cfg = cfg or load()
    rows = _by(universe, lambda o: COND_LABEL.get(o.condition)
               if o.condition in ("renovated", "needs_repair") else None, cfg)
    return sorted(rows, key=lambda r: -r["typical"])


def by_market(universe: Universe, cfg: Settings | None = None) -> list[dict]:
    cfg = cfg or load()
    rows = _by(universe, lambda o: MARKET_LABEL.get(o.market)
               if o.market in ("primary", "secondary") else None, cfg)
    return sorted(rows, key=lambda r: -r["typical"])


def _gap(rows: list[dict]) -> int | None:
    """На скільки відсотків найдорожча група дорожча за найдешевшу."""
    if len(rows) < 2 or not rows[-1]["typical"]:
        return None
    return round(100 * (rows[0]["typical"] / rows[-1]["typical"] - 1))


def days_by_condition(universe: Universe, cfg: Settings | None = None) -> list[dict]:
    """Скільки днів квартира вже продається — однією ознакою.

    Раніше тут був список із двадцяти семи рядків: кімнатність × стан × ринок.
    Прочитати з нього думку неможливо. Одна ознака — одна думка.
    """
    cfg = cfg or load()
    rows = _by(universe,
               lambda o: COND_LABEL.get(o.condition)
               if o.condition in ("renovated", "needs_repair") else None,
               cfg, value=lambda o: o.days_listed)
    return sorted(rows, key=lambda r: -r["typical"])


def headline_days(rows: list[dict]) -> str | None:
    """Заголовок не має вдавати різницю там, де її немає.

    95 днів проти 93 — це не «продаються довше», це однаково. Фраза про
    перевагу з'являється лише тоді, коли перевага справді є.
    """
    gap = _gap(rows)
    if gap is None:
        return None
    if gap < 10:
        return (f"Квартири продаються приблизно однаково довго — "
                f"близько {rows[0]['typical']} дн.")
    return (f"Квартири {rows[0]['label']} продаються довше — "
            f"{rows[0]['typical']} дн. проти {rows[-1]['typical']}")


def headline_rooms(rows: list[dict]) -> str | None:
    """Заголовок графіка — це висновок, а не назва величини.

    «Однокімнатні коштують за метр на 12% більше» замість «Динаміка ціни за м²
    в розрізі кімнатності». Людина має зрозуміти графік, не читаючи його.
    """
    gap = _gap(rows)
    if gap is None:
        return None
    if gap < 3:
        return "Метр коштує приблизно однаково, скільки б не було кімнат"
    return (f"{rows[0]['label']} коштують за метр на {gap}% більше, "
            f"ніж {rows[-1]['label'].lower()}")


def headline_condition(rows: list[dict]) -> str | None:
    gap = _gap(rows)
    if gap is None:
        return None
    if gap < 3:
        return "Ремонт майже не впливає на ціну метра"
    return f"Ремонт додає {gap}% до ціни метра"


def headline_market(rows: list[dict]) -> str | None:
    gap = _gap(rows)
    if gap is None:
        return None
    if gap < 5:
        return "Новобудова і вторинка коштують майже однаково"
    return f"{rows[0]['label'].capitalize()} дорожча на {gap}%"


# --- Пам'ять знімка: частини «Аналітики» й криві виживання ---------------------
# Блок 2 (D48): усе нижче залежить лише від знімка й налаштувань, а не від
# запиту, тож рахується один раз на знімок (`Universe.parts_memo`, `.curves`).
# Ключ включає налаштування (`Settings` — заморожений, хешований): змінили
# пороги — інший ключ, а не старе число.


def _compute_parts(universe: Universe, cfg: Settings) -> dict:
    day_rows = days_by_condition(universe, cfg)
    rooms_rows = by_rooms(universe, cfg)
    condition_rows = by_condition(universe, cfg)
    market_rows = by_market(universe, cfg)
    # Прості зрізи для сторінки: один графік — одна думка. Складені сегменти
    # лишаються для порівняння конкретної квартири, де вони й потрібні.
    cuts = [
        {"key": "rooms", "rows": rooms_rows,
         "title": headline_rooms(rooms_rows),
         "note": "Менша квартира зазвичай дорожча за метр — так на ринку "
                 "буває завжди."},
        {"key": "condition", "rows": condition_rows,
         "title": headline_condition(condition_rows),
         "note": "Порівняні тільки ті квартири, де стан вказано прямо."},
        {"key": "market", "rows": market_rows,
         "title": headline_market(market_rows),
         "note": "Новобудовою вважаємо квартиру, яку так назвав сам продавець "
                 "або майданчик."},
    ]
    return {
        "pairs": primary_vs_secondary(universe, cfg),
        "days": day_rows,
        "days_title": headline_days(day_rows),
        "proxy": liquidity_proxy(universe, cfg),
        "cuts": [c for c in cuts if len(c["rows"]) >= 2],
    }


def analytics_parts(universe: Universe, cfg: Settings) -> dict:
    """Те, що на сторінці «Аналітика» не залежить від фільтра, — раз на знімок.

    Досі рахувалось на кожен запит, а зрізи по кімнатах, стану й ринку — ще й
    двічі (рядки й заголовок). Фільтр застосовується до готових рядків уже в
    маршруті, як і раніше. Значення спільні між запитами — не змінювати.
    """
    # Наборів налаштувань одночасно буває один; 4 — запас на їх зміну.
    return universe.parts_memo.get(("parts", cfg),
                                   lambda: _compute_parts(universe, cfg), capacity=4)


def _curve(observed: list[Item], cfg: Settings) -> dict:
    return estimate([Observation(days=obs[0], event=obs[1], entry=obs[2])
                     for o in observed if (obs := o.observation) is not None], cfg)


_capacity_error: list[str] = []


def _remembered(universe: Universe, key: tuple, compute) -> dict:
    """Крива з пам'яті знімка; якщо config/speed.toml не читається — без пам'яті.

    Сторінки «Аналітика» й квартири до Блоку 2 від speed.toml не залежали, і
    налаштування виміру не має їх валити (500). Без пам'яті крива та сама —
    лише рахується заново; помилка — в журнал (раз на текст помилки, а не на
    кожен запит), як і решта виміру швидкості (perf.py, runner.py).
    """
    try:
        capacity = curve_capacity()
    except configfiles.ConfigError as e:
        text = str(e)
        if text not in _capacity_error:
            _capacity_error[:] = [text]
            log.error("config/speed.toml не читається — криві «Аналітики» без пам'яті: %s",
                      text)
        return compute()
    return universe.curves.get(key, compute, capacity=capacity)


def filter_curve(universe: Universe, cfg: Settings, *, rooms: str = "",
                 condition: str = "", market: str = "") -> dict:
    """Строк продажу (крива виживання) для фільтра сторінки «Аналітика».

    Ліквідність рахуємо тут же, а не заглушкою в шаблоні: блок сам увімкнеться,
    щойно накопичиться достатньо зафіксованих зникнень. Результат — спільний
    між запитами словник: не змінювати.
    """
    def compute() -> dict:
        peers = [o for o in universe.items
                 if (not rooms or str(o.band or "") == rooms)
                 and (not condition or o.condition == condition)
                 and (not market or o.market == market)]
        return _curve(peers, cfg)

    return _remembered(universe, ("filter", rooms, condition, market, cfg), compute)


def segment_curve(universe: Universe, cfg: Settings, item: Item) -> dict:
    """Крива виживання сегмента квартири (кімнатність × стан × ринок).

    Одна на сегмент, а не на квартиру: усі квартири сегмента мають ту саму
    криву. Результат спільний — сторінка квартири копіює його, перш ніж
    дописати підпис сегмента.
    """
    def compute() -> dict:
        peers = [o for o in universe.by_band.get(item.band, ())
                 if o.condition == item.condition and o.market == item.market]
        return _curve(peers, cfg)

    return _remembered(universe, ("segment", item.band, item.condition, item.market, cfg),
                       compute)
