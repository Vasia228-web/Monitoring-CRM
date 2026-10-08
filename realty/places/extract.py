"""Докази місця оголошення: з полів, які вже є в рядку, і з place_raw (E10, D57).

Два шари:
  * `view(row)` — для кроку «райони й ЖК»: зводить у вигляд, однаковий для всіх джерел,
    те, що джерело вже сказало про місце, — поле району DOM.RIA, мітку місцевості LUN у
    `location`, назву ЖК і id ЖК DOM.RIA (`identity.complex`), докази з place_raw
    (geoEntities LUN, «Назва ЖК» OLX, блок ЖК rieltor, населений пункт flombu, стан
    сторінки DOM.RIA з перевірки актуальності). Нічого не пише: докази зі збережених
    полів НЕ копіюються в place_raw (відхилення від плану, D57: 29 тис. записів JSON
    задля того самого, що вже лежить у полях);
  * `from_lun_item`, `from_olx_params`, `from_rieltor_soup`, `from_flombu_location`,
    `from_domria_card` — для збирачів: сирі докази для place_raw (pipeline зливає їх
    ЛИШЕ туди, де порожньо — FILL_ONLY_JSON). Без імен і телефонів.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import address

CITY_NAME = "Івано-Франківськ"


@dataclass
class View:
    id: int
    source: str
    market: str
    property_id: int | None = None
    district_fields: list = field(default_factory=list)     # [(поле, сира назва)]
    district_ids: list = field(default_factory=list)        # [("lun"|"ria", id)]
    complex_ids: list = field(default_factory=list)         # [("ria"|"lun", id)]
    complex_names: list = field(default_factory=list)       # [(поле, сира назва)]
    lat: float | None = None
    lon: float | None = None
    geo: str | None = None
    title: str | None = None
    zhk_words: bool | None = None      # ознака ЖК у тексті (None — не перевіряли)
    # Чи джерело справді показувало поле ЖК для цього оголошення (рецензія E10, D57):
    # «не в ЖК» — лише тоді. DOM.RIA — завжди (поле ЖК у картці); OLX — після сторінки
    # деталей (olx_checked_at); LUN — після проходу стрічки з geoEntities
    # (lun_geo_checked_at); rieltor — після картки (rieltor_checked_at).
    zhk_observed: bool = False
    addr: tuple | None = None          # (слова вулиці, номер будинку) — places.address

    def signal_complex(self) -> bool:
        """Чи є хоч якась ознака ЖК у ПОЛЯХ (для «не в ЖК»)."""
        return bool(self.complex_ids or self.complex_names)

    def inputs(self) -> list:
        """Входи для відбитка place_sig (інкрементальний крок)."""
        return [self.source, self.market, self.district_fields, self.district_ids,
                self.complex_ids, self.complex_names,
                None if self.lat is None else round(self.lat, 6),
                None if self.lon is None else round(self.lon, 6), self.geo,
                self.title, self.zhk_words, self.zhk_observed]


def lun_label(location: str | None) -> str | None:
    """Мітка місцевості LUN — останній сегмент `location` (прототип Етапу 0).

    «Вулиця, Будинок, Мітка»; «Вулиця, Будинок, корпус/орієнтир, Мітка»; «Село» —
    без вулиці; «Вулиця, Будинок» — без мітки; «Івано-Франківськ» — без мітки.
    """
    if not location:
        return None
    segs = [s.strip() for s in location.split(",") if s.strip()]
    if not segs:
        return None
    if len(segs) == 1:
        return None if segs[0] == CITY_NAME else segs[0]
    if len(segs) == 2:
        last = segs[1]
        if re.search(r"\d", last) or last.lower().startswith("будинок"):
            return None
        return last
    return segs[-1]


def lun_middle(location: str | None) -> list[str]:
    """Сегменти `location` LUN між будинком і міткою, що не є корпусом чи номером:
    «Слобідська вул., 46, Калинова Слобода, Крихівці» → ["Калинова Слобода"] (назва ЖК
    тут — 72 рядки LUN; рецензія E10)."""
    if not location:
        return []
    segs = [s.strip() for s in location.split(",") if s.strip()]
    out = []
    for seg in segs[2:-1]:
        low = seg.casefold()
        if re.search(r"\d", seg) or low.startswith(("к.", "корпус", "корп")):
            continue
        out.append(seg)
    return out


def _int(v):
    try:
        if isinstance(v, bool):
            return None
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _ria_id(value) -> int | None:
    if isinstance(value, str) and value.startswith("ria:"):
        return _int(value[4:])
    return _int(value)


def _market(value) -> str:
    v = getattr(value, "value", value)
    return str(v or "unknown").lower()


def view(row, *, zhk_words: bool | None = None) -> View:
    """Вигляд доказів рядка listings (ORM-об'єкт або обʼєкт з тими самими атрибутами)."""
    src = row.source
    ident = row.identity or {}
    raw = row.place_raw or {}
    v = View(id=row.id, source=src, market=_market(row.market_type),
             property_id=row.property_id, title=row.title, zhk_words=zhk_words,
             addr=address.key(src, row.location))
    geo = ident.get("geo")
    lat, lon = ident.get("lat"), ident.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and geo:
        v.lat, v.lon, v.geo = float(lat), float(lon), str(geo)

    if src == "domria":
        if row.district and str(row.district).strip():
            v.district_fields.append(("domria.district", str(row.district).strip()))
        elif raw.get("ria_district"):
            v.district_fields.append(("domria.district", str(raw["ria_district"])))
        if (rid := _int(raw.get("ria_district_id"))):
            v.district_ids.append(("ria", rid))
        for value in (ident.get("complex"), raw.get("ria_newbuild_id")):
            rid = _ria_id(value)
            if rid and ("ria", rid) not in v.complex_ids:
                v.complex_ids.append(("ria", rid))
        for name in (row.complex_name, raw.get("ria_newbuild_name")):
            if name and str(name).strip():
                v.complex_names.append(("domria.complex_name", str(name).strip()))
    elif src == "lun":
        for item in raw.get("lun_geo") or ():
            if not isinstance(item, dict):
                continue
            kind, name, gid = item.get("type"), item.get("name"), _int(item.get("id"))
            if kind == "microdistrict" and name:
                v.district_fields.append(("lun.microdistrict", str(name)))
                if gid:
                    v.district_ids.append(("lun", gid))
            elif kind in ("village", "settlement") and name:
                v.district_fields.append(("lun.village", str(name)))
            elif kind == "residential_complex":
                if gid:
                    v.complex_ids.append(("lun", gid))
                if name:
                    v.complex_names.append(("lun.complex", str(name)))
        if (label := lun_label(row.location)):
            v.district_fields.append(("lun.label", label))
        # Назва між будинком і міткою — лише збіг із довідником ЖК (не «нерозпізнана»).
        for seg in lun_middle(row.location):
            v.complex_names.append(("lun.location", seg))
    elif src == "flombu":
        for key in ("flombu_sublocality", "flombu_locality"):
            if raw.get(key):
                v.district_fields.append(("flombu.locality", str(raw[key])))
    elif src == "blago":
        if row.complex_name and str(row.complex_name).strip():
            v.complex_names.append(("blago.complex_name", str(row.complex_name).strip()))
    # Докази зі сторінок деталей (OLX — у самого OLX і в LUN→olx; rieltor — у LUN).
    for key, fld in (("olx_zhk", "olx.zhk"), ("rieltor_zhk", "rieltor.zhk")):
        if raw.get(key):
            v.complex_names.append((fld, str(raw[key]).strip()))
    v.zhk_observed = (src == "domria"
                      or (src == "olx" and bool(raw.get("olx_checked_at")))
                      or (src == "lun" and bool(raw.get("lun_geo_checked_at")))
                      or (src == "rieltor" and bool(raw.get("rieltor_checked_at"))))
    return v


# --- Для збирачів -----------------------------------------------------------------------------


def from_lun_item(d: dict, types, deref=None, today: str | None = None) -> dict:
    """place_raw з об'єкта стрічки LUN: geoEntities потрібних типів [{type, id, name}] і
    позначка «прохід стрічки з geoEntities був» (lun_geo_checked_at) — навіть без них:
    лише з нею порожній ЖК означає «LUN ЖК не показав» (рецензія E10, «не в ЖК»)."""
    from datetime import date

    out = []
    for g in d.get("geoEntities") or ():
        if not isinstance(g, dict) or g.get("type") not in types:
            continue
        name = g.get("name")
        if deref is not None:
            name = deref(name)
        if not isinstance(name, str) or not name.strip():
            continue
        out.append({"type": g.get("type"), "id": g.get("geoId"), "name": name.strip()[:120]})
    res = {"lun_geo_checked_at": today or date.today().isoformat()}
    if out:
        res["lun_geo"] = out
    return res


def from_olx_params(params: dict, today: str) -> dict:
    """place_raw зі сторінки OLX: параметр «Назва ЖК» (вільний текст продавця)."""
    name = (params.get("назва жк") or "").strip()
    out = {"olx_checked_at": today}
    if name:
        out["olx_zhk"] = name[:120]
    return out


def from_rieltor_soup(soup, today: str) -> dict:
    """place_raw з картки rieltor.ua: блок ЖК (.ldb__complex-name; Етап 0 — 31% сторінок)."""
    out = {"rieltor_checked_at": today}
    el = soup.select_one(".ldb__complex-name")
    if el is not None:
        name = el.get_text(" ", strip=True)
        if name:
            out["rieltor_zhk"] = name[:120]
    return out


def from_flombu_location(g: dict) -> dict:
    """place_raw з estateRecordLocation flombu: населений пункт (район flombu не дає)."""
    out = {}
    for src, dst in (("locality", "flombu_locality"), ("sublocality1", "flombu_sublocality")):
        value = (g.get(src) or "").strip() if isinstance(g.get(src), str) else ""
        if value:
            out[dst] = value[:120]
    return out


def from_domria_card(d: dict) -> dict:
    """place_raw з картки DOM.RIA: id і назва району (ті самі ключі, що й у гачку перевірки)."""
    out = {}
    did = _int(d.get("district_id"))
    if did:
        out["ria_district_id"] = did
    name = d.get("district_name_uk") or d.get("district_name")
    if isinstance(name, str) and name.strip():
        out["ria_district"] = name.strip()[:120]
    nid = _int(d.get("newbuild_id") or d.get("user_newbuild_id"))
    if nid:
        out["ria_newbuild_id"] = nid
    return out
