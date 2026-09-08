"""Міжплатформна дедуплікація: одна квартира — один майстер-запис.

Жорсткий складений ключ тут не працює: адресу з вулицею та будинком дають лише
DIM.RIA і LUN, тоді як OLX і flombu вказують саме місто. Тому пари оголошень
оцінюються за сукупністю ознак, і об'єднання відбувається лише за достатньої
кількості доказів.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace

from sqlalchemy import select

from .models import Condition, Listing, MarketType, Property

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


def shape_of(r: Listing) -> Shape:
    street, house = normalize_address(r.location)
    if not street:
        street, house = street_from_text(r.description)
    district, _ = normalize_address(r.district) if r.district else (None, frozenset())
    return Shape(r.id, r.source, r.original_url, r.rooms, r.area_total, r.floor,
                 street, house, district, r.price_usd)


# --- Зіставлення --------------------------------------------------------------

MERGE_THRESHOLD = 6
AREA_TOLERANCE = 0.6
PRICE_REJECT = 0.40
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
    if a.rooms != b.rooms:
        return -99
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

    if a.price and b.price:
        diff = abs(a.price - b.price) / max(a.price, b.price)
        # Одна квартира не буває вдвічі дорожчою сама за себе: на реальних
        # даних так зливались «вул. Миру, 100» за $89 000 і «вул. Миру» за
        # $38 000 — площа, поверх і вулиця збігались, і −2 не рятувало.
        if diff > PRICE_REJECT:
            return -99
        score += 3 if diff <= 0.03 else 1 if diff <= 0.10 else -2 if diff > 0.25 else 0
    return score


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster(shapes: list[Shape]) -> list[list[int]]:
    """Групує оголошення в кластери «одна квартира».

    Наївне об'єднання всіх схожих пар помилкове: оголошення без адреси
    (OLX, flombu) збігається одночасно з двома квартирами на різних вулицях і
    транзитивно склеює їх в одну групу — перевірено на реальних даних, де так
    злилися Івасюка 86 і Височана 18.

    Тому спершу будуємо якорі з оголошень, у яких адреса відома, і лише потім
    приєднуємо безадресні — кожне щонайбільше до одного якоря. Місток між
    двома будинками стає неможливим за побудовою.
    """
    buckets: dict[tuple, list[Shape]] = defaultdict(list)
    for sh in shapes:
        if sh.rooms is None or sh.area is None:
            continue
        base = int(round(sh.area))
        for delta in (-1, 0, 1):        # допуск на округлення між джерелами
            buckets[(sh.rooms, base + delta)].append(sh)

    uf = _Union()
    for sh in shapes:
        uf.find(sh.id)

    # --- фаза 0: однакове посилання — це напевно одне й те саме оголошення ----
    # LUN агрегує інші сайти, тож той самий URL приходить і від LUN, і від OLX.
    by_url: dict[str, list[Shape]] = defaultdict(list)
    for sh in shapes:
        if sh.url:
            by_url[sh.url.split("?")[0].rstrip("/")].append(sh)

    # Адреса поширюється по групі спільного URL. Без цього безадресний запис
    # OLX ставав містком: за посиланням він зливався з якорем LUN, а за формою
    # чіплявся до якоря DIM.RIA — і два різні будинки опинялись разом.
    patched: dict[int, Shape] = {}
    for group in by_url.values():
        if len(group) < 2:
            continue
        for other in group[1:]:
            uf.union(group[0].id, other.id)
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

    # --- фаза 1: якорі з оголошень, де адреса відома -------------------------
    seen: set[tuple[int, int]] = set()
    for group in buckets.values():
        anchored = [sh for sh in group if sh.street]
        for i, a in enumerate(anchored):
            for b in anchored[i + 1:]:
                pair = (min(a.id, b.id), max(a.id, b.id))
                if pair in seen:
                    continue
                seen.add(pair)
                if match_score(a, b) >= MERGE_THRESHOLD:
                    uf.union(a.id, b.id)

    # --- фаза 2: безадресні приєднуємо до найкращого якоря --------------------
    anchors = {sh.id for sh in shapes if sh.street}
    homeless = [sh for sh in shapes if not sh.street and sh.rooms and sh.area]
    by_id = {sh.id: sh for sh in shapes}
    for sh in homeless:
        best_root, best_score = None, MERGE_THRESHOLD - 1
        base = int(round(sh.area))
        candidates = {c.id for d in (-1, 0, 1) for c in buckets[(sh.rooms, base + d)]}
        for cid in candidates:
            if cid == sh.id or cid not in anchors:
                continue
            score = match_score(sh, by_id[cid])
            if score > best_score:
                best_root, best_score = uf.find(cid), score
        if best_root is not None:
            uf.union(best_root, sh.id)

    # --- фаза 3: безадресні між собою, якщо жодна не пристала до якоря --------
    unattached = [sh for sh in homeless if uf.find(sh.id) not in
                  {uf.find(a) for a in anchors}]
    for i, a in enumerate(unattached):
        for b in unattached[i + 1:]:
            if uf.find(a.id) == uf.find(b.id):
                continue
            if match_score(a, b) >= MERGE_THRESHOLD:
                uf.union(a.id, b.id)

    clusters: dict[int, list[int]] = defaultdict(list)
    for sh in shapes:
        clusters[uf.find(sh.id)].append(sh.id)

    by_id = {sh.id: sh for sh in shapes}
    result: list[list[int]] = []
    for members in clusters.values():
        result.extend(_split_conflicted(members, by_id))
    return result


def _split_conflicted(members: list[int], by_id: dict[int, Shape]) -> list[list[int]]:
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
    return [ids for _, _, ids in groups]


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


def rebuild(session, dry_run: bool = False) -> dict:
    """Перебудовує майстер-записи з поточних оголошень."""
    listings = list(session.scalars(select(Listing)))
    shapes = [shape_of(r) for r in listings]
    by_id = {r.id: r for r in listings}

    groups = cluster(shapes)
    multi = [g for g in groups if len(g) > 1]
    stats = {"listings": len(listings), "properties": len(groups),
             "merged_groups": len(multi),
             "merged_listings": sum(len(g) for g in multi),
             "cross_source": 0}

    if dry_run:
        for g in multi:
            if len({by_id[i].source for i in g}) > 1:
                stats["cross_source"] += 1
        return stats

    session.query(Property).delete()
    session.flush()

    for group in groups:
        members = [by_id[i] for i in group]
        sources = {m.source for m in members}
        if len(sources) > 1:
            stats["cross_source"] += 1
        prices = [m.price_usd for m in members if m.price_usd]
        street, houses = next(
            ((s, h) for s, h in (normalize_address(m.location) for m in members) if s),
            (None, frozenset()),
        )
        prop = Property(
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
            sources_count=len(sources),
            first_seen=min(m.first_seen for m in members),
            last_seen=max(m.last_seen for m in members),
        )
        session.add(prop)
        session.flush()
        for m in members:
            m.property_id = prop.id
    return stats
