"""Сильні ознаки «що це за квартира й будинок» з кожного джерела.

Інвентаризація (22.09.2026, на реальних сторінках):

  поле             DIM.RIA                    LUN                     OLX                 flombu            Благо
  id квартири      flat_entity_id (зводить    — (див. нижче)          —                   —                 план, не квартира
                   різних агентів)
  id будинку       building_entity_id,        geoEntities: house      —                   —                 —
                   osm_building_id (іноді)
  ЖК               newbuild_id + назва        —                       лише в тексті       —                 назва ЖК
  корпус (текст)   building_number_str        «Будинок N» у geo       —                   —                 —
                   («34 корпус 9», «34/7»)
  секція, під'їзд, номер квартири — жодне джерело окремим полем не дає
  координати       точні, але іноді центр ЖК  до будинку              розмиті (radius у   точка             —
                                                                      км) — непридатні
  продавець        user_id                    контакт (НЕ зберігаємо  user.id,            ownerPhoneId      забудовник
                                              — приватність)          isBusiness
  головне фото     main_photo                 images[0]               og:image            —                 —

LUN `groupId` — НЕ квартира: на живій стрічці 22.09 один groupId мали 10
оголошень на п'яти різних вулицях (Ленкавського 2-А к3, Княгинин 25,
Вовчинецька 223-А, …). `similarPageIds` — «схожі», не дублі; `sectionId`
завжди 1. Тому з LUN для зведення беремо лише будинок, корпус і координати.

Ідентичність зберігається в `listings.identity` (JSON). Ключі з префіксом
джерела там, де значення має сенс лише в межах джерела: `ria:…`, `lun:…`.
"""
from __future__ import annotations

import math
import re

# Координати, що точні до будинку, а не до району.
PRECISE = {"building", "point"}

RIA_PHOTO = "https://cdn.riastatic.com/photos/"
LUN_PHOTO = "https://market-images.lunstatic.net/lun-ua/360/480/images/{}.webp"


def from_domria(card: dict) -> dict:
    ident = {
        "flat": _pref("ria", card.get("flat_entity_id")),
        "building": _pref("ria", card.get("building_entity_id")),
        "osm": str(card["osm_building_id"]) if card.get("osm_building_id") else None,
        "complex": _pref("ria", card.get("newbuild_id") or card.get("user_newbuild_id")),
        "complex_name": card.get("user_newbuild_name_uk") or card.get("user_newbuild_name"),
        "korpus": (card.get("building_number_str") or "").strip() or None,
        "lat": _num(card.get("latitude")), "lon": _num(card.get("longitude")),
        "geo": "point",
        "seller": _pref("ria", card.get("user_id")),
        "photo": RIA_PHOTO + card["main_photo"].replace(".jpg", "fl.jpg")
        if card.get("main_photo") else None,
    }
    return _clean(ident)


def from_lun(item: dict) -> dict:
    loc = item.get("location") or []
    lat, lon = (loc[1], loc[0]) if len(loc) == 2 else (None, None)
    house = next((g for g in item.get("geoEntities") or [] if g.get("type") == "house"), None)
    images = item.get("images") or []
    image = images[0].get("imageId") if images and isinstance(images[0], dict) else None
    ident = {
        "building": _pref("lun", house.get("geoId")) if house else None,
        "korpus": (house or {}).get("name"),
        "lat": _num(lat), "lon": _num(lon),
        "geo": "building" if house else "street",
        "photo": LUN_PHOTO.format(image) if image else None,
    }
    return _clean(ident)


_OLX_LAT = re.compile(r'\\?"lat\\?"\s*:\s*(-?[\d.]+)')
_OLX_LON = re.compile(r'\\?"lon\\?"\s*:\s*(-?[\d.]+)')
_OLX_USER = re.compile(r'\\?"user\\?"\s*:\s*\{\s*\\?"id\\?"\s*:\s*(\d+)')
_OLX_BUSINESS = re.compile(r'\\?"isBusiness\\?"\s*:\s*(true|false)')
_OG_IMAGE = re.compile(r'<meta(?=[^>]*property="og:image")[^>]*content="([^"]+)"')


def from_olx_page(html: str) -> dict:
    """OLX навмисно розмиває точку (radius у кілометрах) — координати лише
    для довідки, як доказ «різні будинки» не використовуються."""
    lat, lon = _OLX_LAT.search(html), _OLX_LON.search(html)
    user, biz = _OLX_USER.search(html), _OLX_BUSINESS.search(html)
    photo = _OG_IMAGE.search(html)
    return _clean({
        "lat": _num(lat.group(1)) if lat else None,
        "lon": _num(lon.group(1)) if lon else None,
        "geo": "approx",
        "seller": _pref("olx", user.group(1)) if user else None,
        "business": (biz.group(1) == "true") if biz else None,
        "photo": photo.group(1) if photo else None,
    })


def from_flombu(attrs: dict, location: dict) -> dict:
    return _clean({
        "lat": _num(location.get("latitude")), "lon": _num(location.get("longitude")),
        "geo": "point" if location.get("latitude") else None,
        "seller": _pref("flombu", attrs.get("ownerPhoneId")),
    })


# --- Порівняння -------------------------------------------------------------------------

_NUM = r"(\d+(?:\s*-?\s*[а-яіїєґa-z](?![а-яіїєґa-z]))?)"
# «к.1-6» у LUN — діапазон корпусів, а не корпус 1: такий запис корпус не визначає.
_KORPUS_WORD = re.compile(_NUM + r"\s*,?\s*(?:корпус|корп\.?|к\.?)\s*" + _NUM
                          + r"(?!\d|\s*[-–]\s*\d)", re.I)
_SLASH = re.compile(r"^" + _NUM + r"\s*/\s*" + _NUM + r"$", re.I)


def korpus_code(text: str | None) -> str | None:
    """Код будинку з корпусом: «34 корпус 9» → «34к9»; «34/7» → «34к7».

    LUN пише корпус коротко: «2-А к3» → «2ак3».

    «34/7» на реальних даних DIM.RIA (квартири 24, 1783, 2529) щоразу був
    ОКРЕМИМ будинком — інший будинок в OpenStreetMap, ніж «34» чи «34 корпус
    9», — а не номером квартири. Тому дріб трактуємо як корпус. Без корпусу
    («34», «Будинок 3») — None: сам номер будинку порівнює окреме правило.
    """
    if not text:
        return None
    t = " ".join(text.lower().split())
    m = _KORPUS_WORD.search(t) or _SLASH.match(t)
    if not m:
        return None
    def part(x: str) -> str:
        return re.sub(r"[\s-]", "", x)
    return f"{part(m.group(1))}к{part(m.group(2))}"


def distance_m(a: dict, b: dict) -> float | None:
    """Відстань між точками, якщо обидві точні до будинку; інакше None."""
    if a.get("geo") not in PRECISE or b.get("geo") not in PRECISE:
        return None
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _pref(source: str, value) -> str | None:
    return f"{source}:{value}" if value not in (None, "", 0) else None


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}
