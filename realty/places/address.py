"""Докази «за адресою»: інші оголошення того самого будинку (рецензія E10, D57).

Навіщо. Поле району агента DOM.RIA і мітка LUN збігаються з більшістю інших оголошень
того самого будинку лише в ~80% (рецензія E10: 25/31 на вибірці 50; у ЖК без району —
70,7%), а план Блоку 4 вимагає ≥90% для ступеня «з джерела». Інші оголошення будинку —
незалежний доказ: якщо ≥ rules.link.min_share з них (n ≥ rules.link.min_n, лише ІНШІ
квартири) кажуть один район, будинок у цьому районі (ступінь «addr»).

Ключ будинку — нормалізована вулиця + номер будинку:
  * вулиця — набір слів без типу («вул.», «вулиця», «просп.» …), без старої назви в
    дужках і без ініціалів: DOM.RIA пише «вул. Левицького Романа, 4», LUN — «Романа
    Левицького вул., 4, Набережна» — той самий набір. Вулиці збігаються, якщо набір
    однієї входить у набір іншої («Отця Блавацького» ⊆ «Отця Івана Блавацького»);
  * номер — перше число з літерою («223-А», «223А», «Будинок 223а» → «223а»); корпус
    («34 к6», «3 корпус 5», «34/10») — той самий будинок для району (не для зведення).
Лише DOM.RIA і LUN (у них вулиця й номер — окремі сегменти `location`); без номера —
без ключа (вулиця сама — надто грубо).

Нічого не пише: індекс будується в пам'яті кроку з полів, які вже є в рядках.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

_TYPE_WORDS = frozenset({
    "вул", "вулиця", "вулиці", "просп", "проспект", "пров", "провулок", "бульв", "бульвар",
    "б-р", "бр", "пл", "площа", "шосе", "майдан", "узвіз", "тупик", "проїзд", "алея", "ім",
    "імені", "м", "місто", "мкр", "мікрорайон", "будинок", "буд",
})
_CITY = re.compile(r"івано[\s\-]?франківськ\w*", re.I)
_PAREN = re.compile(r"\([^)]*\)")
_APOS = str.maketrans({c: "" for c in "'’ʼ`‘"})
_SPLIT = re.compile(r"[^\w]+", re.U)
_LAT_TO_CYR = str.maketrans("aceiopxykmhtb", "асеіорхукмнтв")
_HOUSE = re.compile(r"^\s*(?:будинок|буд\.?|б\.)?\s*(\d{1,4})\s*-?\s*([а-яієїґa-z])?(?![\w])",
                    re.I)
_STREET_MARK = re.compile(
    r"\b(?:вул|вулиця|просп|проспект|пров|провулок|бульв|бульвар|б-р|пл|площа|шосе|"
    r"майдан|узвіз|тупик|проїзд|алея|набережна)\b", re.I)
SOURCES = ("domria", "lun")


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).casefold().translate(_APOS)

    def fix(m: re.Match) -> str:
        w = m.group(0)
        if re.search(r"[а-яієїґ]", w) and re.search(r"[a-z]", w):
            return w.translate(_LAT_TO_CYR)
        return w
    return re.sub(r"\w+", fix, s)


def street_tokens(segment: str | None) -> frozenset[str]:
    """Набір слів вулиці («вул. Левицького Романа» → {левицького, романа})."""
    if not segment:
        return frozenset()
    s = _PAREN.sub(" ", _fold(_CITY.sub(" ", segment)))
    return frozenset(t for t in _SPLIT.split(s)
                     if len(t) > 1 and t not in _TYPE_WORDS and not t.isdigit())


def house_number(segment: str | None) -> str | None:
    """Номер будинку з сегмента: «223-А» → «223а»; «34 к6», «34/10» → «34»; «Будинок»
    без номера, «3 черга (будинок 6)» — None."""
    if not segment:
        return None
    s = _fold(segment)
    if "черг" in s:
        return None
    m = _HOUSE.match(s)
    if not m:
        return None
    letter = (m.group(2) or "").translate(_LAT_TO_CYR)
    return m.group(1).lstrip("0") + letter or None


def key(source: str, location: str | None) -> tuple[frozenset, str] | None:
    """(слова вулиці, номер) будинку оголошення або None."""
    if source not in SOURCES or not location:
        return None
    segs = [s.strip() for s in str(location).split(",") if s.strip()]
    if len(segs) < 2:
        return None
    if source == "lun" and not _STREET_MARK.search(segs[0]):
        return None                       # «Микитинці, …» — населений пункт, не вулиця
    street = street_tokens(segs[0])
    house = house_number(segs[1])
    if not street or not house:
        return None
    return street, house


@dataclass
class Evidence:
    """Що кажуть ІНШІ квартири того самого будинку (лише вони — без самого оголошення й
    оголошень його квартири)."""

    votes: Counter = field(default_factory=Counter)       # корінь району → голосів (DOM.RIA + LUN)
    city_votes: Counter = field(default_factory=Counter)  # те саме, лише міські райони
    ria_city: int = 0                                     # голосів DOM.RIA за міський район
    complexes: Counter = field(default_factory=Counter)   # ЖК «з джерела» → оголошень

    def consensus(self, votes: Counter, min_share: float, min_n: int) -> str | None:
        total = sum(votes.values())
        if total < min_n:
            return None
        top, n = votes.most_common(1)[0]
        return top if n / total >= min_share else None


class Index:
    """Будинки → голоси. `add` для кожного рядка, потім `evidence(id)` чи `others(id, вид)`.

    Види голосів: v — корінь району (DOM.RIA + LUN), c — те саме, лише міські райони,
    r — міські голоси DOM.RIA, x — ЖК «з джерела»; довільні інші (`extra`) — для
    вибірки на перевірку (`places sample`: сирі назви інших квартир будинку)."""

    def __init__(self) -> None:
        self._rows: dict[int, tuple] = {}                 # id → (група, квартира)
        self._groups: dict[tuple, int] = {}               # (вулиця, номер) → група
        self._by_tok: dict[tuple, list[int]] = defaultdict(list)
        self._group_keys: list[tuple] = []
        self._total: list[dict] = []                      # група → {вид: Counter}
        self._by_owner: list[dict] = []                   # група → {власник: {вид: Counter}}
        self._matches_of: dict[int, list[int]] = {}
        self._merged: dict[tuple, Counter] = {}

    def add(self, lid: int, owner, addr, *, vote=None, area_city=None, ria=False,
            complex_key=None, extra: dict | None = None) -> None:
        """`owner` — квартира (або ('l', id) без неї); `vote` — корінь району з поля рядка."""
        if addr is None:
            return
        g = self._groups.get(addr)
        if g is None:
            g = self._groups[addr] = len(self._group_keys)
            self._group_keys.append(addr)
            self._total.append(defaultdict(Counter))
            self._by_owner.append(defaultdict(lambda: defaultdict(Counter)))
            for tok in addr[0]:
                self._by_tok[(tok, addr[1])].append(g)
        self._rows[lid] = (g, owner)
        parts = []
        if vote:
            parts.append(("v", vote))
            if area_city:
                parts.append(("c", vote))
                if ria:
                    parts.append(("r", vote))
        if complex_key:
            parts.append(("x", complex_key))
        for kind, value in (extra or {}).items():
            if value:
                parts.append((kind, value))
        for kind, value in parts:
            self._total[g][kind][value] += 1
            self._by_owner[g][owner][kind][value] += 1

    def _matches(self, g: int) -> list[int]:
        got = self._matches_of.get(g)
        if got is not None:
            return got
        street, house = self._group_keys[g]
        seen: set[int] = set()
        out = []
        for tok in street:
            for g2 in self._by_tok.get((tok, house), ()):
                if g2 in seen:
                    continue
                seen.add(g2)
                s2 = self._group_keys[g2][0]
                if s2 <= street or street <= s2:
                    out.append(g2)
        self._matches_of[g] = out
        return out

    def others(self, lid: int, kind: str) -> Counter:
        """Голоси виду `kind` ІНШИХ квартир того самого будинку (без квартири рядка)."""
        got = self._rows.get(lid)
        if got is None:
            return Counter()
        g, owner = got
        total = self._merged.get((g, kind))
        if total is None:
            total = Counter()
            for g2 in self._matches(g):
                total.update(self._total[g2].get(kind) or {})
            self._merged[(g, kind)] = total
        own = Counter()
        for g2 in self._matches(g):
            mine = self._by_owner[g2].get(owner)
            if mine and kind in mine:
                own.update(mine[kind])
        return total - own

    def evidence(self, lid: int) -> Evidence | None:
        if lid not in self._rows:
            return None
        return Evidence(votes=self.others(lid, "v"), city_votes=self.others(lid, "c"),
                        ria_city=sum(self.others(lid, "r").values()),
                        complexes=self.others(lid, "x"))
