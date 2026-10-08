"""Ступені визначення району й ЖК (чисті функції; E10, D57).

Оголошення (`resolve`). Порядок — rules.tiers:
  район: complex_src (ЖК з поля джерела, прив'язаний до району: «район ЖК сильніший за
         район агента», пояснення «за ЖК») → addr (інші квартири того самого будинку:
         ≥ rules.link.min_share при n ≥ rules.link.min_n — places.address; рецензія E10:
         поле агента збігається з будинком лише в ~80%) → src (поле району джерела:
         DOM.RIA district, мікрорайон чи село з geoEntities LUN, мітка LUN, населений
         пункт flombu) → complex_weak (ЖК з координат чи тексту, прив'язаний) → coords (kNN);
         оголошення в ЖК з довідника, НЕ прив'язаному до району, поля агента не бере
         (там збіг з будинком — 70,7%): лише addr або «не визначено»;
         мітка села LUN на будинку, який ≥ rules.addr.village_veto_min_ria оголошень
         DOM.RIA інших квартир кладуть у місто, відкидається (`area_conflict`), а район —
         за містом будинку (addr_area) або «не визначено»; на такому будинку мітки сіл
         не голосують і в доказах будинку для інших оголошень;
  ЖК:    src_id (id ЖК DOM.RIA чи LUN) → src_name (назва з поля джерела; сегмент
         `location` LUN між будинком і міткою і «ЖК …» у полі району — лише збіг) →
         coords → text; «не в ЖК» ('_none') — лише вторинка, джерело справді показувало
         поле ЖК (View.zhk_observed), жодної ознаки ЖК у полях і тексті, і жодна інша
         квартира того самого будинку не має ЖК «з джерела».
Назва, якої немає ні в довіднику, ні серед ігнорованих (POI, орієнтир, вулиця), — у
`unknown` (список на /status), а значення — «не визначено».

Квартира (`property_place`): найсильніший ступінь серед її оголошень, усередині —
більшість; нічия — None і запис у place_conflict. Дитина й батько («Центр» і
«Німецька колонія») — не протиріччя: перемагає конкретніший. Парасолька поступається
ЖК усередині себе.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .directory import NONE

DISTRICT_RANK = {"complex_src": 0, "addr": 1, "addr_area": 1, "src": 2, "complex_weak": 3,
                 "coords": 4}
COMPLEX_RANK = {"src_id": 0, "src_name": 1, "coords": 2, "text": 3, "secondary": 4}
SRC_TIERS = ("src_id", "src_name")


@dataclass
class Resolution:
    district_key: str | None = None
    district_how: str | None = None
    complex_key: str | None = None
    complex_how: str | None = None
    area: str | None = None
    unknown: list = field(default_factory=list)        # [(поле, сира назва)]
    district_src: str | None = None                    # що каже поле району (для точності)
    area_conflict: str | None = None                   # відкинута мітка села (ключ)


def _text_complex(view, d, rules) -> str | None:
    """ЖК із заголовка: точна назва з довідника одразу після маркера («ЖК Senat …»)."""
    text = view.title or ""
    if not text:
        return None
    low = text.casefold()
    for marker in rules.text.markers:
        start = 0
        m = marker.casefold()
        while (i := low.find(m, start)) != -1:
            start = i + len(m)
            before = low[i - 1] if i else " "
            after = low[start] if start < len(low) else " "
            if before.isalnum() or after.isalnum():
                continue
            tail = text[start:start + 60].strip(" «»\"'“”„:-–—")
            words = tail.split()
            for n in range(min(4, len(words)), 0, -1):
                kind, key = d.match_complex(" ".join(words[:n]).strip(",.;:!?)»\""))
                if kind == "complex" and d.complexes[key].kind != "address":
                    return key
    return None


def _complex_names(view, d) -> list[tuple[str, str, bool]]:
    """(поле, назва, чи рахувати нерозпізнаною). Сегмент `location` LUN і «ЖК …» у полі
    району (ignore-вид complex: «ЖК Винагородний» у DOM.RIA) — лише збіг із довідником."""
    out = [(fld, name, fld != "lun.location") for fld, name in view.complex_names]
    for fld, name in view.district_fields:
        if d.match_district(name) == ("ignore", "complex"):
            out.append((fld, name, False))
    return out


def resolve(view, d, rules, *, coords=None, text_enabled: bool = False,
            addr=None) -> Resolution:
    """Район і ЖК одного оголошення. `coords` — (vote_district, vote_complex) або None;
    `addr` — places.address.Evidence будинку (інші квартири) або None."""
    res = Resolution()
    # --- ЖК ---------------------------------------------------------------------------------
    names = _complex_names(view, d)
    complex_found: tuple[str, str] | None = None
    for tier in rules.tiers.complex:
        if complex_found:
            break
        if tier == "src_id":
            for kind, cid in view.complex_ids:
                key = d.complex_by_ria(cid) if kind == "ria" else d.complex_by_lun(cid)
                if key:
                    complex_found = (key, "src_id")
                    break
        elif tier == "src_name":
            for fld, name, report in names:
                kind, key = d.match_complex(name)
                if kind == "complex":
                    complex_found = (key, "src_name")
                    break
                if kind == "unknown" and report:
                    res.unknown.append((fld, name))
        elif tier == "coords" and coords is not None and coords[1] is not None:
            if (view.source in rules.coords.complex_sources
                    and view.market in rules.coords.complex_markets
                    and view.geo in rules.coords.precise_geo and view.lat is not None):
                key = coords[1](view.lat, view.lon)
                if key:
                    complex_found = (key, "coords")
        elif tier == "text" and text_enabled:
            key = _text_complex(view, d, rules)
            if key:
                complex_found = (key, "text")
    if complex_found:
        res.complex_key, res.complex_how = complex_found
    elif (view.market in rules.secondary.markets and view.zhk_observed
          and not view.signal_complex() and not names
          and not (rules.secondary.forbid_zhk_words and view.zhk_words)
          and not (addr is not None and addr.complexes)):
        res.complex_key, res.complex_how = NONE, "secondary"

    # --- Район --------------------------------------------------------------------------------
    veto = (addr is not None and addr.ria_city >= rules.addr.village_veto_min_ria)
    src_district = None
    for kind, did in view.district_ids:
        key = d.district_by_lun(did) if kind == "lun" else d.district_by_ria(did)
        if key:
            src_district = key
            break
    for fld, name in view.district_fields:
        kind, key = d.match_district(name)
        if kind == "district":
            if veto and fld.startswith("lun.") and d.area(key) != "city":
                # Мітка села LUN на будинку, який DOM.RIA (≥3 оголошення інших квартир)
                # кладе в місто: LUN мітить найближчий населений пункт (рецензія E10:
                # Фізкультурна 27 — «Крихівці» проти 176 «Бам»).
                res.area_conflict = res.area_conflict or key
                continue
            src_district = src_district or key
            if src_district:
                break
        elif kind == "unknown":
            res.unknown.append((fld, name))
    res.district_src = src_district
    linked = d.complex_district(res.complex_key) if res.complex_key not in (None, NONE) else None
    # ЖК з довідника без району: поле агента там збігається з будинком лише в 70,7%.
    unlinked = res.complex_key not in (None, NONE) and not linked
    addr_key = None
    if addr is not None:
        # Будинок, який DOM.RIA кладе в місто: мітки сіл LUN не голосують і за інші
        # оголошення будинку (інакше «Крихівці» більшістю LUN переважили б «Бам» навіть
        # для рядка DOM.RIA; рецензія E10, Фізкультурна 27).
        votes = addr.city_votes if veto else addr.votes
        addr_key = addr.consensus(votes, rules.link.min_share, rules.link.min_n)
    for tier in rules.tiers.district:
        if tier == "complex_src" and linked and res.complex_how in SRC_TIERS:
            res.district_key, res.district_how = linked, "complex_src"
        elif tier == "addr" and addr_key:
            if src_district and d.root(src_district) == addr_key:
                # Поле джерела підтверджене будинком — конкретніше (дитина) і «з джерела».
                res.district_key, res.district_how = src_district, "src"
            else:
                res.district_key = addr_key
                res.district_how = "addr_area" if res.area_conflict else "addr"
        elif tier == "src" and src_district and not unlinked:
            res.district_key, res.district_how = src_district, "src"
        elif tier == "complex_weak" and linked and res.complex_how in ("coords", "text"):
            res.district_key, res.district_how = linked, "complex_weak"
        elif (tier == "coords" and coords is not None and coords[0] is not None
              and view.geo in rules.coords.precise_geo and view.lat is not None):
            key = coords[0](view.lat, view.lon)
            if key:
                res.district_key, res.district_how = key, "coords"
        if res.district_key:
            break
    res.area = d.area(res.district_key)
    return res


# --- Квартира -------------------------------------------------------------------------------


def _pick_district(votes: list[str], d) -> tuple[str | None, bool]:
    """(ключ, нічия). Голоси — за коренем; усередині кореня — конкретніший, якщо один."""
    roots = Counter(d.root(k) for k in votes)
    ranked = roots.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, True
    winner = ranked[0][0]
    specific = Counter(k for k in votes if d.root(k) == winner and k != winner)
    if len(specific) == 1:
        return next(iter(specific)), False
    return winner, False


def _family_root(key: str, d) -> str:
    c = d.complexes.get(key)
    return c.within if c is not None and c.within else key


def _pick_complex(votes: list[str], d) -> tuple[str | None, bool]:
    if all(v == NONE for v in votes):
        return NONE, False
    votes = [v for v in votes if v != NONE]
    roots = Counter(_family_root(k, d) for k in votes)
    ranked = roots.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, True
    winner = ranked[0][0]
    specific = Counter(k for k in votes if _family_root(k, d) == winner and k != winner)
    if not specific:
        return winner, False
    top = specific.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return winner, False           # дві черги під однією парасолькою — парасолька
    return top[0][0], False


def property_place(members, d) -> tuple[str | None, str | None, str | None, dict | None]:
    """(district_key, complex_key, place_area, place_conflict) квартири.

    `members` — [(district_key, district_how, complex_key, complex_how)] її оголошень
    (збережені ключі — «лише туди, де порожньо», тож квартира стабільна).
    """
    dist = [(DISTRICT_RANK.get(how, 9), key) for key, how, _, _ in members if key]
    cplx = [(COMPLEX_RANK.get(how, 9), key) for _, _, key, how in members if key]
    conflict: dict = {}
    dk = ck = None
    if dist:
        best = min(r for r, _ in dist)
        dk, tie = _pick_district([k for r, k in dist if r == best], d)
        roots = sorted({d.root(k) for _, k in dist})
        if len(roots) > 1 or tie:
            conflict["district"] = sorted({k for _, k in dist})
    if cplx:
        best = min(r for r, _ in cplx)
        ck, tie = _pick_complex([k for r, k in cplx if r == best], d)
        concrete = sorted({k for _, k in cplx if k != NONE})
        roots = {_family_root(k, d) for k in concrete}
        if len(roots) > 1 or tie or (len(concrete) > 1 and ck not in concrete):
            conflict["complex"] = concrete
    return dk, ck, d.area(dk), (conflict or None)
