"""Міжплатформна дедуплікація: одна квартира — один майстер-запис.

Жорсткий складений ключ тут не працює: адресу з вулицею та будинком дають лише
DIM.RIA і LUN, тоді як OLX і flombu вказують саме місто. Тому пари оголошень
оцінюються за сукупністю ознак, і об'єднання відбувається лише за достатньої
кількості доказів.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update

from .identity import distance_m, korpus_code
from .models import (
    Condition, DedupDecision, Listing, MarketType, PriceEvent, Property, PropertyRedirect,
)

log = logging.getLogger(__name__)

# --- Нормалізація адреси ------------------------------------------------------

_PREFIX = re.compile(
    r"\b(?:вул(?:иця|\.)?|вулиці|бульвар|б-р|проспект|просп\.?|пров(?:улок|\.)?|"
    r"площа|майдан|набережна|шосе|м\.|місто|будинок|буд\.?|корпус|корп\.?)\b",
    re.I,
)
_HOUSE = re.compile(r"\b(\d{1,4})\s*([А-ЯІЇЄҐа-яіїєґA-Za-z])?\b")
# Адреса може містити кілька номерів: «Княгинин, 44 корпус 13» — це той самий
# будинок, що й «Будинок 13» в іншого джерела. Тому номер — не одне значення,
# а набір, і збігом вважається непорожній перетин.
_JUNK = re.compile(r"[^\w\s'’\-]", re.U)
_CITY = re.compile(r"івано[\s\-]?франківськ\w*|ивано[\s\-]?франковск\w*", re.I)
# Маркер типу вулиці. LUN пише його ПІСЛЯ назви («Лемківська вул., Будинок,
# Міське озеро»), DIM.RIA — перед нею. У LUN за комою йде ще й район, і його
# слова не мають потрапляти в ключ вулиці: на коротких назвах вони перетягували
# збіг і зливали різні вулиці (Лемківська × Романа Левицького через «Міське озеро»).
_MARKER = re.compile(
    r"\b(?:вул(?:иця|\.)?|бульвар|б-р|проспект|просп\.?|пров(?:улок|\.)?|"
    r"площа|майдан|набережна|шосе)\b", re.I,
)
# Вулиця в тексті опису — слабкий сигнал, тому вимагаємо номер будинку поруч.
_STREET_IN_TEXT = re.compile(
    r"(?:вул(?:иця|\.)?|бульвар|б-р|проспект|просп\.?)\s*"
    r"([А-ЯІЇЄҐ][а-яіїєґ'’\-]+(?:\s+[А-ЯІЇЄҐ][а-яіїєґ'’\-]+){0,2})\s*,\s*(\d{1,4}[А-Яа-я]?)",
    re.U,
)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def normalize_address(text: str | None) -> tuple[str | None, frozenset[str]]:
    """`(ключ вулиці, номер будинку)` з довільного запису адреси.

    LUN пише «Героїв Миколаєва вул., Будинок 3», DIM.RIA — «вул. Героїв
    Миколаєва, 3»; після нормалізації обидва дають однаковий ключ.
    """
    if not text:
        return None, frozenset()
    raw = _CITY.sub(" ", str(text))
    # Номери беремо з усього рядка, а назву вулиці — лише з того сегмента,
    # де стоїть маркер типу вулиці.
    houses = frozenset(
        (m.group(1) + (m.group(2) or "")).lower()
        for m in _HOUSE.finditer(_JUNK.sub(" ", raw))
    )
    segments = [seg for seg in raw.split(",") if seg.strip()]
    street_seg = next((seg for seg in segments if _MARKER.search(seg)),
                      segments[0] if segments else "")
    raw = _JUNK.sub(" ", street_seg)
    words = _PREFIX.sub(" ", raw)
    words = re.sub(r"\d+", " ", words)
    tokens = [_fold(w) for w in words.split() if len(w) > 2]
    # «ЖК» та назви ТЦ у полі району — це орієнтир, а не адреса.
    tokens = [t for t in tokens if t not in ("жк", "тц", "корпус")]
    return (" ".join(sorted(tokens)) or None), houses


def street_from_text(text: str | None) -> tuple[str | None, frozenset[str]]:
    """Вулиця з опису — запасний варіант для джерел без поля адреси."""
    if not text:
        return None, frozenset()
    if m := _STREET_IN_TEXT.search(text):
        return normalize_address(f"{m.group(1)} {m.group(2)}")
    return None, frozenset()


# --- Ознаки оголошення --------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    """Те, за чим порівнюємо оголошення."""

    id: int
    source: str
    url: str | None
    rooms: int | None
    area: float | None
    floor: int | None
    street: str | None
    house: frozenset[str]
    district: str | None
    price: float | None
    # Сильні ознаки з `listings.identity` (D41) і те, що потрібно правилам-вето.
    flat: str | None = None              # id квартири DIM.RIA
    building: str | None = None          # id будинку в межах джерела
    osm: str | None = None
    korpus: str | None = None            # «34к9» — див. identity.korpus_code
    lat: float | None = None
    lon: float | None = None
    geo: str | None = None
    primary: bool = False
    condition: Condition | None = None
    start: datetime | None = None        # коли оголошення з'явилось
    end: datetime | None = None          # до коли висіло (зараз — якщо активне)
    prices: tuple = ()                   # ((коли, ціна $), …) за зростанням часу


def _naive(dt: datetime | None) -> datetime | None:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt and dt.tzinfo else dt


def shape_of(r: Listing, prices: tuple = (), now: datetime | None = None) -> Shape:
    street, house = normalize_address(r.location)
    if not street:
        street, house = street_from_text(r.description)
    district, _ = normalize_address(r.district) if r.district else (None, frozenset())
    ident = r.identity or {}
    flat = ident.get("flat")
    active = r.manual_active if r.manual_active is not None else r.is_active
    start = min(filter(None, (_naive(r.first_seen), _naive(r.published_at))), default=None)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    end = now if active else _naive(r.last_alive_at or r.delisted_at or r.last_seen)
    return Shape(r.id, r.source, r.original_url, r.rooms, r.area_total, r.floor,
                 street, house, district, r.price_usd,
                 flat=flat if flat and not flat.endswith(":?") else None,
                 building=ident.get("building"), osm=ident.get("osm"),
                 korpus=korpus_code(ident.get("korpus")) or korpus_code(r.location),
                 lat=ident.get("lat"), lon=ident.get("lon"), geo=ident.get("geo"),
                 primary=r.market_type == MarketType.PRIMARY, condition=r.condition,
                 start=start, end=end, prices=prices)


def load_shapes(session, listings: list[Listing]) -> list[Shape]:
    """Форми всіх оголошень разом з історією цін (для правила «одночасні»)."""
    history: dict[int, list] = defaultdict(list)
    for lid, at, usd in session.execute(
            select(PriceEvent.listing_id, PriceEvent.observed_at, PriceEvent.price_usd)
            .where(PriceEvent.price_usd.is_not(None))
            .order_by(PriceEvent.listing_id, PriceEvent.observed_at)):
        history[lid].append((_naive(at), usd))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return [shape_of(r, tuple(history.get(r.id, ())), now) for r in listings]


# --- Зіставлення --------------------------------------------------------------

MERGE_THRESHOLD = 6
AREA_TOLERANCE = 0.6
PRICE_REJECT = 0.40
# Один агент пише «2-кімнатна з кухнею-студією», другий рахує ту саму квартиру
# як трикімнатну. Різниця рівно в одну кімнату при однаковій площі, поверсі й
# будинку — це майже завжди різниця в підрахунку, а не різні квартири. Більша
# різниця — таки різні квартири, і вона лишається жорсткою забороною.
ROOMS_TOLERANCE = 1
# Коли кімнатність розходиться, решта доказів має бути бездоганною: та сама
# площа з точністю до округлення, той самий поверх, той самий будинок.
ROOMS_MISMATCH_AREA = 0.35
# Наскільки мають перетинатись набори слів адреси, щоб вважати її тією самою.
STREET_OVERLAP = 0.65


def street_overlap(a: str | None, b: str | None) -> float:
    """Частка спільних слів адреси.

    Точна рівність не годиться: LUN дописує до вулиці район («Героїв
    Миколаєва вул., Будинок 3, Тисменицька»), DIM.RIA — ні.
    """
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def match_score(a: Shape, b: Shape) -> int:
    """Скільки доказів за те, що це одна квартира. Від'ємні — проти."""
    rooms_differ = False
    if a.rooms != b.rooms:
        if a.rooms is None or b.rooms is None:
            return -99
        if abs(a.rooms - b.rooms) > ROOMS_TOLERANCE:
            return -99
        # Різниця в одну кімнату допускається, але дорого: площа має збігтися
        # майже точно, а поверх і будинок — обов'язково (нижче вони й так
        # перевіряються). Інакше сусідні планування в одному будинку почали б
        # зливатися.
        if abs((a.area or 0) - (b.area or 0)) > ROOMS_MISMATCH_AREA:
            return -99
        if a.floor is None or b.floor is None:
            return -99
        rooms_differ = True
    # Забудовники виставляють десятки схожих квартир в одному будинку, тому
    # допуск має покривати лише різне округлення між сайтами (74.2 і 74.0),
    # а не сусідні планування (42 і 43).
    if a.area is None or b.area is None or abs(a.area - b.area) > AREA_TOLERANCE:
        return -99

    score = 0
    # Поверх — найсильніше заперечення: різні поверхи означають різні квартири.
    if a.floor is not None and b.floor is not None:
        score += 3 if a.floor == b.floor else -99

    overlap = street_overlap(a.street, b.street)
    if overlap >= STREET_OVERLAP:
        score += 3
        if a.house and b.house:
            if not (a.house & b.house):
                # Одна вулиця, різні будинки — це різні будівлі, і жодні інші
                # збіги цього не переважують. Аудит знайшов об'єкт із 23
                # оголошень на вул. Хіміків у будинках 2, 24, 28 і 92.
                return -99
            score += 3
    elif a.street and b.street:
        # Різні вулиці — така сама заборона, як різний поверх. Штрафу мало:
        # на реальних даних бонуси за поверх, площу й ціну дотягували пару
        # «Гарбарська» × «Софрона Мудрого» рівно до порога злиття.
        return -99

    if a.district and b.district and a.district == b.district:
        score += 1

    score += 2 if abs(a.area - b.area) <= 0.3 else 1

    if rooms_differ:
        # Різниця в кімнатах допускається лише за додаткового доказу: або той
        # самий будинок, або та сама ціна. Одного збігу площі з поверхом
        # замало — у будинку забудовника таких квартир десятки.
        same_building = bool(a.house and b.house and (a.house & b.house))
        same_price = bool(a.price and b.price and
                          abs(a.price - b.price) / max(a.price, b.price) <= 0.03)
        if not (same_building or same_price):
            return -99
        # Штраф підібраний по реальній парі: LUN без адреси й DOMRIA з
        # адресою, у яких збігаються площа, поверх і ціна до долара, набирають
        # рівно поріг — і жодного бала понад. Пара з самими лише площею й
        # поверхом, без ціни чи будинку, до порога не дотягує (і відсікається
        # ще раніше перевіркою вище).
        score -= 2

    if a.price and b.price:
        diff = abs(a.price - b.price) / max(a.price, b.price)
        # Одна квартира не буває вдвічі дорожчою сама за себе: на реальних
        # даних так зливались «вул. Миру, 100» за $89 000 і «вул. Миру» за
        # $38 000 — площа, поверх і вулиця збігались, і −2 не рятувало.
        if diff > PRICE_REJECT:
            return -99
        score += 3 if diff <= 0.03 else 1 if diff <= 0.10 else -2 if diff > 0.25 else 0
    return score



# --- Правила-вето (D41) -------------------------------------------------------
#
# Вибірка 20 квартир із 8+ оголошеннями (22.09) показала: у 12 із них злито
# кілька різних квартир. Причини — корпус не порівнювався, ціна допускала 40%,
# схожість ланцюжком (A≈B, B≈C ⇒ A=C) і однакові квартири в новобудовах.
# Принцип власника: не впевнений, що це одна квартира, — не зливай.
#
# Кожне правило вмикається окремо — щоб пробний прогін показав, скільки
# квартир розщеплює кожне. Без жодного правила зведення таке саме, як до D41.

RULES = ("ria_flat", "building", "korpus", "geo", "price", "condition", "newbuild", "no_chain")
RULE_LABELS = {
    "ria_flat": "різні id квартири DIM.RIA (однаковий — зводить)",
    "building": "різні будинки в межах джерела",
    "korpus": "різні корпуси (коли відомі в обох)",
    "geo": "точні координати далеко одна від одної",
    "price": "ціна різниться >10% в одночасних оголошень",
    "condition": "«з ремонтом» проти «без ремонту»",
    "newbuild": "новобудова: без id — лише якщо пасує до однієї квартири",
    "no_chain": "без ланцюжків і без нічиїх",
    "manual": "рішення власника",
}
# Далі — за вимірюванням на парах з однаковим id квартири DIM.RIA (D41).
GEO_VETO_M = float(os.environ.get("DEDUP_GEO_VETO_M", 250))
PRICE_CONCURRENT = 0.10
CONCURRENT_MIN = timedelta(days=1)


def active_rules() -> frozenset[str]:
    """Правила для робочої перебудови. До рішення власника — жодного (D41)."""
    raw = os.environ.get("DEDUP_RULES", "").strip()
    if raw == "all":
        return frozenset(RULES)
    return frozenset(r.strip() for r in raw.split(",") if r.strip() in RULES)


def _url_key(url: str) -> str:
    return url.split("?")[0].rstrip("/")


def price_at(sh: Shape, t: datetime) -> float | None:
    """Ціна оголошення на момент t — за історією змін."""
    if not sh.prices:
        return sh.price
    i = bisect_right(sh.prices, (t, float("inf")))
    return sh.prices[i - 1][1] if i else sh.prices[0][1]


def concurrent_gap(a: Shape, b: Shape) -> float | None:
    """Розрив цін, якщо оголошення висіли одночасно; інакше None.

    Якщо одне зняли, а пізніше з'явилось дешевше, — це може бути та сама
    квартира зі зниженою ціною, тож розрив між ними правилом не є."""
    if None in (a.start, a.end, b.start, b.end):
        return None
    hi = min(a.end, b.end)
    if hi - max(a.start, b.start) < CONCURRENT_MIN:
        return None
    pa, pb = price_at(a, hi), price_at(b, hi)
    if not pa or not pb:
        return None
    return abs(pa - pb) / max(pa, pb)


def distance(a: Shape, b: Shape) -> float | None:
    return distance_m({"lat": a.lat, "lon": a.lon, "geo": a.geo},
                      {"lat": b.lat, "lon": b.lon, "geo": b.geo})


def uses_flat_id(rules) -> bool:
    return "ria_flat" in rules or "newbuild" in rules


def strong(a: Shape, b: Shape, rules) -> bool:
    """Прямий доказ «це одне й те саме»: те саме посилання або id квартири DIM.RIA.

    id квартири DIM.RIA — справжній: на вибірці 22.09 одна квартира мала той
    самий id в оголошеннях 8–11 різних агентів."""
    if a.url and b.url and _url_key(a.url) == _url_key(b.url):
        return True
    return uses_flat_id(rules) and a.flat is not None and a.flat == b.flat


def veto(a: Shape, b: Shape, rules) -> str | None:
    """Чому ці два оголошення НЕ можуть бути однією квартирою (або None)."""
    if "ria_flat" in rules and a.flat and b.flat and a.flat != b.flat:
        return "ria_flat"
    if strong(a, b, rules):
        return None
    if ("building" in rules and a.building and b.building and a.building != b.building
            and a.building.split(":")[0] == b.building.split(":")[0]
            and not (a.osm and b.osm and a.osm == b.osm)):
        return "building"
    if "korpus" in rules and a.korpus and b.korpus and a.korpus != b.korpus:
        return "korpus"
    if "geo" in rules:
        d = distance(a, b)
        if d is not None and d > GEO_VETO_M:
            return "geo"
    if "condition" in rules and {a.condition, b.condition} == {Condition.RENOVATED,
                                                                Condition.NEEDS_REPAIR}:
        return "condition"
    if "price" in rules:
        gap = concurrent_gap(a, b)
        if gap is not None and gap > PRICE_CONCURRENT:
            return "price"
    # Без ланцюжків: A і C, несумісні самі по собі (інший поверх, вулиця,
    # будинок), не зводяться через B, схоже на обидва.
    if "no_chain" in rules and structural_mismatch(a, b):
        return "no_chain"
    return None


def structural_mismatch(a: Shape, b: Shape) -> bool:
    """Несумісність, яку не пояснити різним записом тієї самої квартири.

    Площі тут немає: агенти пишуть її по-різному — в одній квартирі DIM.RIA
    (той самий id у 12 агентів, вибірка 22.09) від 40,45 до 41,3 м²."""
    if a.rooms is not None and b.rooms is not None and abs(a.rooms - b.rooms) > ROOMS_TOLERANCE:
        return True
    if a.floor is not None and b.floor is not None and a.floor != b.floor:
        return True
    if a.street and b.street:
        if street_overlap(a.street, b.street) < STREET_OVERLAP:
            return True
        if a.house and b.house and not (a.house & b.house):
            return True
    return bool(a.price and b.price
                and abs(a.price - b.price) / max(a.price, b.price) > PRICE_REJECT)


class Manual:
    """Рішення власника: «це різні квартири» / «це одна квартира».

    Сильніші за будь-яке правило: «одна» знімає всі вето між цими
    оголошеннями, «різні» забороняє їм опинитись разом за будь-яких доказів."""

    def __init__(self, decisions=()) -> None:
        self.sides: dict[int, list[tuple[int, int]]] = defaultdict(list)
        self.same_of: dict[int, set[int]] = defaultdict(set)
        self.same_sets: list[list[int]] = []
        for n, (kind, left, right) in enumerate(decisions):
            if kind == "different":
                for i in left:
                    self.sides[i].append((n, 0))
                for i in right:
                    self.sides[i].append((n, 1))
            elif kind == "same":
                members = sorted({*left, *right})
                self.same_sets.append(members)
                for i in members:
                    self.same_of[i].add(n)

    def __bool__(self) -> bool:
        return bool(self.sides or self.same_sets)

    def relation(self, x: int, y: int) -> str | None:
        if x in self.sides and y in self.sides:
            ys = dict(self.sides[y])
            if any(n in ys and ys[n] != side for n, side in self.sides[x]):
                return "different"
        if self.same_of.get(x) and self.same_of[x] & self.same_of.get(y, set()):
            return "same"
        return None

    @classmethod
    def load(cls, session) -> "Manual":
        rows = session.scalars(select(DedupDecision).where(DedupDecision.active.is_(True))
                               .order_by(DedupDecision.id))
        return cls([(d.kind, d.left or [], d.right or []) for d in rows])


@dataclass
class Judge:
    """Чи можна звести дві групи: жодного вето між будь-якою парою (повний зв'язок)."""

    rules: frozenset
    by_id: dict
    manual: Manual
    blocked: Counter = field(default_factory=Counter)
    _veto: dict = field(default_factory=dict)

    @property
    def trivial(self) -> bool:
        return not self.rules and not self.manual

    def pair_veto(self, x: int, y: int) -> str | None:
        key = (x, y) if x < y else (y, x)
        if key not in self._veto:
            rel = self.manual.relation(x, y) if self.manual else None
            self._veto[key] = ("manual" if rel == "different" else None if rel == "same"
                               else veto(self.by_id[x], self.by_id[y], self.rules))
        return self._veto[key]

    def can_merge(self, left: list[int], right: list[int], count: bool = True) -> bool:
        if self.trivial:
            return True
        for x in left:
            for y in right:
                why = self.pair_veto(x, y)
                if why:
                    if count:
                        self.blocked[why] += 1
                    return False
        return True


class _Groups:
    """Об'єднання груп, що пам'ятає склад кожної — для перевірки «кожен з кожним»."""

    def __init__(self, ids) -> None:
        self.parent = {i: i for i in ids}
        self.members = {i: [i] for i in ids}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> int:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra].extend(self.members.pop(rb))
        return ra

    def merge(self, judge: Judge, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if not judge.can_merge(self.members[ra], self.members[rb]):
            return False
        self.union(ra, rb)
        return True

def cluster(shapes: list[Shape], rules=frozenset(), manual: Manual | None = None,
            stats: dict | None = None) -> list[list[int]]:
    """Групує оголошення в кластери «одна квартира».

    Наївне об'єднання всіх схожих пар помилкове: оголошення без адреси
    (OLX, flombu) збігається одночасно з двома квартирами на різних вулицях і
    транзитивно склеює їх в одну групу — перевірено на реальних даних, де так
    злилися Івасюка 86 і Височана 18.

    Тому спершу будуємо якорі з оголошень, у яких адреса відома, і лише потім
    приєднуємо безадресні — кожне щонайбільше до одного якоря. Місток між
    двома будинками стає неможливим за побудовою.

    `rules` — правила-вето D41 (див. `RULES`); без них зведення — як до D41.
    Вето діє на всю групу: дві групи зводяться, лише якщо між БУДЬ-ЯКОЮ парою
    їхніх оголошень немає вето (повний зв'язок), тож «34 корпус 9» і «34/7» не
    зійдуться через оголошення «34» без корпусу.
    """
    rules = frozenset(rules)
    manual = manual or Manual()
    buckets: dict[tuple, list[Shape]] = defaultdict(list)
    for sh in shapes:
        if sh.rooms is None or sh.area is None:
            continue
        base = int(round(sh.area))
        for delta in (-1, 0, 1):        # допуск на округлення між джерелами
            buckets[(sh.rooms, base + delta)].append(sh)

    # --- фаза 0: однакове посилання — це напевно одне й те саме оголошення ----
    # LUN агрегує інші сайти, тож той самий URL приходить і від LUN, і від OLX.
    by_url: dict[str, list[Shape]] = defaultdict(list)
    for sh in shapes:
        if sh.url:
            by_url[_url_key(sh.url)].append(sh)

    # Адреса поширюється по групі спільного URL. Без цього безадресний запис
    # OLX ставав містком: за посиланням він зливався з якорем LUN, а за формою
    # чіплявся до якоря DIM.RIA — і два різні будинки опинялись разом.
    patched: dict[int, Shape] = {}
    for group in by_url.values():
        if len(group) < 2:
            continue
        known = next((sh for sh in group if sh.street), None)
        if known is None:
            continue
        for sh in group:
            if not sh.street:
                patched[sh.id] = replace(sh, street=known.street, house=known.house)
    if patched:
        shapes = [patched.get(sh.id, sh) for sh in shapes]
        buckets.clear()
        for sh in shapes:
            if sh.rooms is None or sh.area is None:
                continue
            base = int(round(sh.area))
            for delta in (-1, 0, 1):
                buckets[(sh.rooms, base + delta)].append(sh)

    by_id = {sh.id: sh for sh in shapes}
    judge = Judge(rules, by_id, manual)
    g = _Groups(by_id)

    # Рішення власника — першими й без жодних вето.
    for members in manual.same_sets:
        present = [i for i in members if i in by_id]
        for other in present[1:]:
            g.union(present[0], other)
    # Прямі докази: те саме посилання, той самий id квартири DIM.RIA.
    strong_pairs = []
    for group in by_url.values():
        strong_pairs += [(group[0].id, other.id) for other in group[1:]]
    if uses_flat_id(rules):
        by_flat: dict[str, list[int]] = defaultdict(list)
        for sh in shapes:
            if sh.flat:
                by_flat[sh.flat].append(sh.id)
        strong_pairs += [(ids[0], other) for ids in by_flat.values() for other in ids[1:]]
    for a, b in sorted(strong_pairs):
        g.merge(judge, a, b)

    def nearby(rooms: int | None, area_base: int) -> list:
        """Кандидати з сусідніх кошиків по площі Й по кімнатності.

        Кімнатність входить у ключ кошика заради швидкості, але через це пара
        «2 кімнати» × «3 кімнати» ніколи навіть не порівнювалась — хоч би як
        збігалися площа, поверх і ціна. Допуск у кімнатах без цього кроку не
        працює взагалі: він зашитий у `match_score`, до якого справа не
        доходила.
        """
        out = []
        for dr in range(-ROOMS_TOLERANCE, ROOMS_TOLERANCE + 1):
            key_rooms = None if rooms is None else rooms + dr
            if key_rooms is not None and key_rooms < 1:
                continue
            for da in (-1, 0, 1):
                out.extend(buckets.get((key_rooms, area_base + da), ()))
        return out

    # --- фаза 1: якорі з оголошень, де адреса відома -------------------------
    # Найпевніші пари — першими: коли вето не дає звести все, разом лишаються
    # найсхожіші.
    seen: set[tuple[int, int]] = set()
    edges = []
    for (rooms, area_base), group in list(buckets.items()):
        anchored = [sh for sh in group if sh.street]
        pool = [sh for sh in nearby(rooms, area_base) if sh.street]
        for a in anchored:
            for b in pool:
                if a.id == b.id:
                    continue
                pair = (min(a.id, b.id), max(a.id, b.id))
                if pair in seen:
                    continue
                seen.add(pair)
                score = match_score(a, b)
                if score >= MERGE_THRESHOLD:
                    edges.append((-score, *pair))
    for _, a, b in sorted(edges):
        g.merge(judge, a, b)

    # --- фаза 2: безадресні приєднуємо до найкращого якоря --------------------
    anchors = {sh.id for sh in shapes if sh.street}
    homeless = [sh for sh in shapes if not sh.street and sh.rooms and sh.area]
    for sh in homeless:
        base = int(round(sh.area))
        # Порядок кандидатів — як до D41 (від нього залежить, хто виграє нічию),
        # сортування стабільне: серед рівних лишається той самий порядок.
        cands = [(match_score(sh, by_id[cid]), cid)
                 for cid in {c.id for c in nearby(sh.rooms, base)}
                 if cid != sh.id and cid in anchors]
        cands = sorted((c for c in cands if c[0] >= MERGE_THRESHOLD), key=lambda c: -c[0])
        for _, cid in cands:
            if g.merge(judge, cid, sh.id):
                break

    # --- фаза 3: безадресні між собою, якщо жодна не пристала до якоря --------
    anchor_roots = {g.find(a) for a in anchors}
    unattached = [sh for sh in homeless if g.find(sh.id) not in anchor_roots]
    edges = []
    for i, a in enumerate(unattached):
        for b in unattached[i + 1:]:
            score = match_score(a, b)
            if score >= MERGE_THRESHOLD:
                edges.append((-score, min(a.id, b.id), max(a.id, b.id)))
    for _, a, b in sorted(edges):
        g.merge(judge, a, b)

    ambiguous = _detach_ambiguous(g, judge, by_id, buckets, nearby) if rules else []

    result: list[list[int]] = []
    for members in g.members.values():
        result.extend(_split_conflicted(sorted(members), by_id, manual))
    if stats is not None:
        stats["blocked"] = dict(judge.blocked)
        stats["ambiguous"] = len(ambiguous)
    return result


def _detach_ambiguous(g: _Groups, judge: Judge, by_id: dict, buckets, nearby) -> list[int]:
    """Не впевнений — не зливай: оголошення, що пасує до двох різних квартир,
    лишається окремо.

    * новобудова (newbuild): оголошення без id квартири DIM.RIA, яке без жодного
      вето пасує ще до ІНШОЇ квартири, — адже в новобудовах бувають однакові
      квартири на тому самому поверсі в різних секціях;
    * без нічиїх (no_chain): безадресне оголошення, яке до іншої квартири пасує
      не гірше, ніж до своєї, — вибір між ними був би навмання.
    Рішення власника «це одна квартира» такі оголошення не відокремлює.
    """
    rules, manual = judge.rules, judge.manual
    out = []
    for root, members in list(g.members.items()):
        if len(members) < 2:
            continue
        for x in members:
            sh = by_id[x]
            newbuild = "newbuild" in rules and sh.primary and not sh.flat
            tie = "no_chain" in rules and not sh.street
            if not (newbuild or tie) or sh.rooms is None or sh.area is None:
                continue
            if manual and any(manual.relation(x, y) == "same" for y in members if y != x):
                continue
            if any(strong(sh, by_id[y], rules) for y in members if y != x):
                continue                            # те саме посилання — не нічия
            own = max((match_score(sh, by_id[y]) for y in members if y != x), default=-99)
            rivals: dict[int, int] = {}
            for c in nearby(sh.rooms, int(round(sh.area))):
                r = g.find(c.id)
                if r == root or c.id == x:
                    continue
                sc = match_score(sh, c)
                if sc >= MERGE_THRESHOLD and sc > rivals.get(r, -999):
                    rivals[r] = sc
            for r, sc in sorted(rivals.items(), key=lambda kv: -kv[1]):
                if not (newbuild or sc >= own):
                    break
                if judge.can_merge([x], g.members[r], count=False):
                    out.append(x)
                    break
    # Відокремлюємо після огляду, щоб він від цього не залежав.
    detached = set(out)
    members = {}
    for group in g.members.values():
        rest = [m for m in group if m not in detached]
        if rest:
            members[rest[0]] = rest
    members.update({x: [x] for x in out})
    g.members = members
    return out


def _split_conflicted(members: list[int], by_id: dict[int, Shape],
                      manual: Manual | None = None) -> list[list[int]]:
    """Розбиває кластер, у якому опинились несумісні адреси.

    Об'єднання йде трьома шляхами (посилання, адреса, форма), і кожен новий
    шлях — це нова нагода створити місток між двома будинками. Замість того
    щоб латати кожен окремо, інваріант «в одному об'єкті лише одна адреса»
    перевіряється тут наприкінці й відновлюється розділенням.
    """
    if len(members) < 2 or not conflicts([by_id[i] for i in members]):
        return [members]

    groups: list[tuple[str, frozenset[str], list[int]]] = []
    homeless: list[int] = []
    for i in members:
        sh = by_id[i]
        if not sh.street:
            homeless.append(i)
            continue
        for n, (street, houses, ids) in enumerate(groups):
            if street_overlap(street, sh.street) >= STREET_OVERLAP and (
                not (houses and sh.house) or (houses & sh.house)
            ):
                ids.append(i)
                groups[n] = (street, houses | sh.house, ids)
                break
        else:
            groups.append((sh.street, sh.house, [i]))

    if not groups:
        return [members]
    # Безадресні лишаються з найбільшою підгрупою — там найбільше підтверджень.
    groups.sort(key=lambda g: len(g[2]), reverse=True)
    groups[0][2].extend(homeless)
    parts = [ids for _, _, ids in groups]
    if manual:
        # Власник сказав «це одна квартира» — адреси, записані по-різному,
        # цього не скасовують: частини з його рішенням зводимо назад.
        merged: list[list[int]] = []
        for part in parts:
            home = next((m for m in merged if any(manual.relation(x, y) == "same"
                                                  for x in part for y in m)), None)
            if home is None:
                merged.append(part)
            else:
                home.extend(part)
        parts = merged
    return parts


def conflicts(shapes: list[Shape]) -> list[tuple[str, str]]:
    """Пари несумісних адрес усередині одного кластера — має бути порожньо."""
    known = [(sh.street, sh.house) for sh in shapes if sh.street]

    # Номери будинків зв'язуємо транзитивно: запис «Княгинин, 44 корпус 13»
    # доводить, що 44 і 13 — той самий будинок, навіть якщо інші джерела
    # згадують лише одне з чисел. Без цього одна квартира виглядала б як
    # конфлікт «13 проти 44».
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, houses in known:
        tokens = sorted(houses)
        for other in tokens[1:]:
            parent[find(other)] = find(tokens[0])

    bad = []
    for i, (sa, ha) in enumerate(known):
        for sb, hb in known[i + 1:]:
            if street_overlap(sa, sb) < STREET_OVERLAP:
                bad.append((f"{sa} {sorted(ha)}".strip(), f"{sb} {sorted(hb)}".strip()))
            elif ha and hb and not ({find(x) for x in ha} & {find(x) for x in hb}):
                # Та сама вулиця, номери з різних будівель. Записи без номера
                # («вул. Хіміків» без цифри) працювали містком і зводили в
                # один об'єкт будинки 2, 24, 28 і 92.
                bad.append((f"{sa} {sorted(ha)}".strip(), f"{sb} {sorted(hb)}".strip()))
    return bad


# --- Побудова майстер-записів -------------------------------------------------


def _pick(values: list, default=None):
    """Найчастіше непорожнє значення."""
    counts: dict = defaultdict(int)
    for v in values:
        if v not in (None, "", MarketType.UNKNOWN, Condition.UNKNOWN):
            counts[v] += 1
    return max(counts, key=counts.get) if counts else default


def assign_ids(groups: list[list[int]], old_pid: dict[int, int | None]) -> list[int | None]:
    """Кожній новій групі — id квартири, у якій була більшість її оголошень.

    Жадібно за розміром перетину: пара (група, старий id) з найбільшою кількістю
    спільних оголошень отримує цей id першою. Старий id дістається лише одній
    групі (тій, куди перейшла більшість його оголошень); група, якій жоден
    старий id не дістався, отримує новий (None). Порядок детермінований —
    дві перебудови поспіль на тих самих даних дають ті самі id.
    """
    pairs = []
    for gi, group in enumerate(groups):
        for pid, n in Counter(old_pid.get(i) for i in group).items():
            if pid is not None:
                pairs.append((-n, pid, gi))
    pairs.sort()
    out: list[int | None] = [None] * len(groups)
    taken: set[int] = set()
    for _, pid, gi in pairs:
        if out[gi] is None and pid not in taken:
            out[gi] = pid
            taken.add(pid)
    return out


def resolve_property_id(session, pid: int, hops: int = 10) -> int | None:
    """Чинний id квартири: сам, якщо є; інакше — куди вона злилась."""
    for _ in range(hops):
        if session.get(Property, pid) is not None:
            return pid
        red = session.get(PropertyRedirect, pid)
        if red is None:
            return None
        pid = red.new_id
    return None


def _attrs(members: list[Listing]) -> dict:
    prices = [m.price_usd for m in members if m.price_usd]
    street, houses = next(
        ((s, h) for s, h in (normalize_address(m.location) for m in members) if s),
        (None, frozenset()),
    )
    return dict(
        fingerprint="|".join(sorted(f"{m.source}:{m.external_id}" for m in members))[:128],
        rooms=_pick([m.rooms for m in members]),
        area_total=_pick([m.area_total for m in members]),
        floor=_pick([m.floor for m in members]),
        floors_total=_pick([m.floors_total for m in members]),
        street=street, house=(sorted(houses)[0] if houses else None),
        district=_pick([m.district for m in members]),
        location=_pick([m.location for m in members]),
        price_usd_min=min(prices) if prices else None,
        price_usd_max=max(prices) if prices else None,
        price_per_sqm=_pick([m.price_per_sqm for m in members]),
        market_type=_pick([m.market_type for m in members], MarketType.UNKNOWN),
        condition=_pick([m.condition for m in members], Condition.UNKNOWN),
        sources_count=len({m.source for m in members}),
        first_seen=min(m.first_seen for m in members),
        last_seen=max(m.last_seen for m in members),
    )


def rebuild(session, dry_run: bool = False, rules=None) -> dict:
    """Перебудовує майстер-записи з поточних оголошень, ЗБЕРІГАЮЧИ їхні id.

    Раніше квартири видалялись усі й створювались заново, і SQLite нумерував їх
    з 1: за півтори доби в 89% оголошень змінився id квартири, збережені
    посилання показували чужі квартири, скарги вказували не туди. Тепер:
      * група отримує id квартири, де була більшість її оголошень (`assign_ids`);
      * нова група — новий id, вищий за всі, що будь-коли існували (id
        зниклої квартири ніколи не дістається іншій);
      * квартира, що злилась з іншою, зникає, а її старий id переадресовується
        на ту, куди перейшла більшість її оголошень (`property_redirects`).
    Позначка «в обробці» живе на оголошеннях і перебудови не торкається.

    `rules` — правила-вето D41; None — ті, що ввімкнені в `DEDUP_RULES`.
    Рішення власника (`dedup_decisions`) діють завжди й сильніші за правила.
    """
    listings = list(session.scalars(select(Listing)))
    shapes = load_shapes(session, listings)
    by_id = {r.id: r for r in listings}
    old_pid = {r.id: r.property_id for r in listings}
    rules = active_rules() if rules is None else frozenset(rules)

    cstats: dict = {}
    groups = cluster(shapes, rules, Manual.load(session), cstats)
    multi = [g for g in groups if len(g) > 1]
    stats = {"listings": len(listings), "properties": len(groups),
             "merged_groups": len(multi),
             "merged_listings": sum(len(g) for g in multi),
             "cross_source": sum(1 for g in multi if len({by_id[i].source for i in g}) > 1),
             "rules": sorted(rules), "blocked": cstats.get("blocked", {}),
             "ambiguous": cstats.get("ambiguous", 0),
             "kept_ids": 0, "new_ids": 0, "redirected": 0, "removed": 0}
    if dry_run:
        return stats

    # Порядок кроків такий, що посилання оголошень на квартири цілі в КОЖНУ мить
    # (перевірка зовнішніх ключів увімкнена): нові й оновлені квартири → нові
    # номери оголошенням → лише потім видалення квартир, на які вже ніщо не
    # посилається. Відкладена перевірка ключів (D36) тут не потрібна: драйвер
    # відкриває транзакцію лише перед першою зміною даних, і PRAGMA до того
    # моменту встигала скинутись — покладатись на таке не можна.
    ids = assign_ids(groups, old_pid)
    existing = {p.id: p for p in session.scalars(select(Property))}
    high = max([0, *existing, *session.scalars(select(PropertyRedirect.old_id)),
                *session.scalars(select(PropertyRedirect.new_id))])
    retired = [pid for pid in existing if pid not in set(ids)]

    # Відбиток квартири унікальний: щоб нові й оновлені не зіткнулись зі
    # старими значеннями, спершу всім наявним — тимчасові.
    for pid, prop in existing.items():
        prop.fingerprint = f"tmp:{pid}"
    session.flush()

    final: list[int] = []
    for gi, group in enumerate(groups):
        members = [by_id[i] for i in group]
        attrs = _attrs(members)
        pid = ids[gi]
        if pid is None:
            high += 1
            pid = high
            session.add(Property(id=pid, **attrs))
            stats["new_ids"] += 1
        else:
            for k, v in attrs.items():
                setattr(existing[pid], k, v)
            stats["kept_ids"] += 1
        final.append(pid)
    session.flush()

    # Номер квартири пишемо лише тим оголошенням, у кого він справді змінився,
    # і ПРЯМИМ оновленням зі збереженням last_seen: у моделі last_seen має
    # onupdate — будь-який UPDATE рядка оголошення ставив би «бачили щойно»,
    # хоча оголошення ніхто не бачив. Стара перебудова так «освіжала» всі
    # оголошення, яким перенумерувала квартиру.
    moves: dict[int, list[int]] = defaultdict(list)
    for gi, group in enumerate(groups):
        for i in group:
            if old_pid.get(i) != final[gi]:
                moves[final[gi]].append(i)
    for pid, lids in moves.items():
        for start in range(0, len(lids), 500):
            session.execute(update(Listing)
                            .where(Listing.id.in_(lids[start:start + 500]))
                            .values(property_id=pid, last_seen=Listing.last_seen)
                            .execution_options(synchronize_session=False))
    stats["listings_moved"] = sum(len(v) for v in moves.values())
    for m in listings:                          # у пам'яті — теж актуальні значення
        session.expire(m, ["property_id"])

    # Тепер на зниклі квартири ніщо не посилається — видаляємо масово (ORM-
    # каскад обнулив би property_id оголошень окремими UPDATE).
    if retired:
        for pid in retired:
            session.expunge(existing.pop(pid))
        session.execute(delete(Property).where(Property.id.in_(retired)))

    # Зниклі квартири: їхні оголошення перейшли в інші — переадресовуємо туди,
    # куди перейшла більшість.
    new_pid = {i: final[gi] for gi, g in enumerate(groups) for i in g}
    for pid in retired:
        moved = Counter(new_pid[i] for i, old in old_pid.items() if old == pid and i in new_pid)
        if moved:
            target = sorted(moved.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            red = session.get(PropertyRedirect, pid)
            if red is None:
                session.add(PropertyRedirect(old_id=pid, new_id=target))
            else:
                red.new_id = target
            # Старі переадресації, що вели сюди, — одразу на кінцеву квартиру.
            for chained in session.scalars(select(PropertyRedirect)
                                           .where(PropertyRedirect.new_id == pid)):
                chained.new_id = target
            stats["redirected"] += 1
        stats["removed"] += 1
    return stats
