"""Пошук за посиланням у базі (Блок 5, крок E14, D59): розібране посилання → квартири.

Лише читання бази, без мережі й без журналу вхідного тексту. Розбір самого
посилання (мобільні версії, параметри відстеження, якорі, слеш у кінці) —
`realty/links.py` (крок E6, D51); тут — що з ним знайдено:

  * ключ «сайт:id» шукається в `listings.site_key` (рядки LUN зберігають адресу
    сайту, на який ведуть, тож оголошення OLX, яке ми бачили лише через LUN,
    знаходиться за посиланням OLX) і в (source, external_id) — для власних id
    LUN, flombu й Благо;
  * наше посилання на квартиру (/property/123, «№123») — через переадресації
    злитих квартир (dedup.resolve_property_id);
  * голе число — у всіх публічних просторах id (номер квартири, id оголошень);
    listings.id ніде не показуються й не шукаються;
  * адреса OLX у нижньому регістрі (регістр id втрачено) — усі id OLX, що
    збігаються без урахування регістру, на вибір.

Підсумок: одна квартира → перехід на неї з підсвіченим оголошенням; кілька →
сторінка вибору; нічого → код причини (тексти — config/lookup.toml [messages]).
Знайдене, але зняте чи в карантині, теж веде на квартиру: причину показує плашка.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, func, or_, select

from .. import links
from ..dedup import resolve_property_id
from ..models import Listing, Property

# Стан знайденого оголошення (плашка й сторінка вибору).
ACTIVE, REMOVED, MANUAL_OFF = "active", "removed", "manual_off"
QUALITY_OK, QUARANTINE, PENDING = "ok", "quarantine", "pending"


@dataclass(frozen=True)
class Found:
    """Одне знайдене оголошення (рядок listings) і його стан простими словами."""

    listing_id: int
    property_id: int | None
    source: str
    site_key: str | None
    url: str | None
    status: str                      # active | removed | manual_off
    removed_at: datetime | None
    quality: str                     # ok | quarantine | pending
    quality_reason: str | None
    unconfirmed: bool                # з хоста, що не перевіряється (Благо)


@dataclass
class Option:
    """Квартира на сторінці вибору."""

    property_id: int
    key: str | None = None           # ключ для підсвічування (?hl=)
    via: tuple[str, str] | None = None   # (сімейство, id) — як прочитали голе число
    found: list[Found] = field(default_factory=list)
    info: dict = field(default_factory=dict)


@dataclass
class Resolution:
    kind: str                        # property | choice | not_found | invalid
    code: str | None = None          # код тексту config/lookup.toml [messages]
    link: object = None              # links.Link | links.NotALink
    key: str | None = None           # ключ «сайт:id» для ?hl= (None — наше посилання)
    family: str | None = None
    property_id: int | None = None
    options: list[Option] = field(default_factory=list)
    found: list[Found] = field(default_factory=list)


def found_of(row, *, unconfirmed: bool = False) -> Found:
    """Рядок listings (ORM-об'єкт чи рядок SELECT із тими самими іменами) → Found."""
    if row.manual_active is False:
        status = MANUAL_OFF
    elif row.manual_active is True or row.is_active:
        status = ACTIVE
    else:
        status = REMOVED
    q = row.quality_status or "pending"
    quality = QUALITY_OK if q == "ok" else PENDING if q == "pending" else QUARANTINE
    return Found(listing_id=row.id, property_id=row.property_id, source=row.source,
                 site_key=row.site_key, url=row.original_url, status=status,
                 removed_at=row.delisted_at if status == REMOVED else None,
                 quality=quality, quality_reason=row.quality_reason if quality != QUALITY_OK
                 else None, unconfirmed=unconfirmed)


def _unconfirmed(row) -> bool:
    try:
        from ..liveness import ui
        return ui.unconfirmed_url(row.original_url, site_key=row.site_key)
    except Exception:                               # noqa: BLE001 — позначка, не пошук
        return False


_COLS = (Listing.id, Listing.property_id, Listing.source, Listing.site_key,
         Listing.external_id, Listing.original_url, Listing.is_active, Listing.manual_active,
         Listing.delisted_at, Listing.quality_status, Listing.quality_reason)


def find_rows(session, keys) -> list[Found]:
    """Оголошення ключів «сайт:id»: за site_key і за (source, external_id), у порядку id."""
    keys = [k for k in dict.fromkeys(keys) if k and ":" in k]
    if not keys:
        return []
    conds = [Listing.site_key.in_(keys)]
    by_family: dict[str, list[str]] = {}
    for k in keys:
        fam, ident = k.split(":", 1)
        by_family.setdefault(fam, []).append(ident)
    for fam, ids in by_family.items():
        conds.append(and_(Listing.source == fam, Listing.external_id.in_(ids)))
    rows = session.execute(select(*_COLS).where(or_(*conds)).order_by(Listing.id)).all()
    return [found_of(r, unconfirmed=_unconfirmed(r)) for r in rows]


def _property_info(session, pids) -> dict[int, dict]:
    """Підпис квартири для сторінки вибору: ціна, кімнати, площа, адреса, район."""
    pids = sorted(set(pids))
    if not pids:
        return {}
    labels = None
    try:
        from ..places import directory
        labels = directory.current()
    except Exception:                               # noqa: BLE001 — лише підпис
        labels = None
    counts = dict(session.execute(
        select(Listing.property_id, func.count()).where(Listing.property_id.in_(pids))
        .group_by(Listing.property_id)).all())
    out = {}
    for p in session.scalars(select(Property).where(Property.id.in_(pids))):
        district = (labels.label(p.district_key) if labels is not None and p.district_key
                    else None) or p.district
        address = ", ".join(x for x in (p.street, p.house) if x) or p.location
        out[p.id] = {"price": p.price_usd_min, "rooms": p.rooms, "area": p.area_total,
                     "floor": p.floor, "floors_total": p.floors_total, "address": address,
                     "district": district, "listings": counts.get(p.id, 0)}
    return out


def _by_property(found: list[Found]) -> dict[int, list[Found]]:
    out: dict[int, list[Found]] = {}
    for f in found:
        if f.property_id is not None:
            out.setdefault(f.property_id, []).append(f)
    return out


def _from_found(session, link, key: str, found: list[Found], *, missing: str) -> Resolution:
    family = key.split(":", 1)[0]
    groups = _by_property(found)
    if not found:
        return Resolution("not_found", missing, link, key, family)
    if not groups:
        return Resolution("not_found", "not_placed", link, key, family, found=found)
    if len(groups) == 1:
        (pid, rows), = groups.items()
        return Resolution("property", None, link, key, family, pid, found=rows)
    info = _property_info(session, groups)
    return Resolution("choice", "multi_property", link, key, family, found=found,
                      options=[Option(pid, key, None, rows, info.get(pid, {}))
                               for pid, rows in sorted(groups.items())])


def _own(session, link) -> Resolution:
    pid = resolve_property_id(session, int(link.id))
    if pid is None:
        return Resolution("not_found", "own_missing", link, None, "own")
    return Resolution("property", None, link, None, "own", pid)


def _candidates(session, link, *, missing: str) -> Resolution:
    """Голе число чи токен із кількома прочитаннями: кожен простір id окремо."""
    options: list[Option] = []
    found_all: list[Found] = []
    for cand in link.candidates:
        fam, ident = cand.split(":", 1)
        if fam == "own":
            pid = resolve_property_id(session, int(ident))
            if pid is not None:
                options.append(Option(pid, None, (fam, ident)))
            continue
        found = find_rows(session, [cand])
        found_all += found
        for pid, rows in sorted(_by_property(found).items()):
            options.append(Option(pid, cand, (fam, ident), rows))
    pids = {o.property_id for o in options}
    if not pids:
        unplaced = [f for f in found_all if f.property_id is None]
        if unplaced:
            return Resolution("not_found", "not_placed", link, None, link.family, found=unplaced)
        return Resolution("not_found", missing, link, None, link.family)
    if len(pids) == 1:
        keyed = next((o for o in options if o.key), None)
        return Resolution("property", None, link, keyed.key if keyed else None,
                          keyed.via[0] if keyed else "own", options[0].property_id,
                          found=keyed.found if keyed else [])
    info = _property_info(session, pids)
    for o in options:
        o.info = info.get(o.property_id, {})
    return Resolution("choice", "number_choice", link, None, link.family, options=options,
                      found=found_all)


def _case_lost(session, link) -> Resolution:
    """Адреса OLX у нижньому регістрі: усі id OLX, що збігаються без урахування регістру."""
    low = f"olx:{link.id.lower()}"
    keys = list(session.scalars(
        select(Listing.site_key).where(Listing.site_key >= "olx:", Listing.site_key < "olx;",
                                       func.lower(Listing.site_key) == low)
        .distinct().order_by(Listing.site_key)))
    if not keys:
        return Resolution("not_found", "case_lost", link, None, "olx")
    if len(keys) == 1:
        res = _from_found(session, link, keys[0], find_rows(session, keys), missing="case_lost")
        return res
    options: list[Option] = []
    found_all: list[Found] = []
    for k in keys:
        found = find_rows(session, [k])
        found_all += found
        for pid, rows in sorted(_by_property(found).items()):
            options.append(Option(pid, k, ("olx", k.split(":", 1)[1]), rows))
    info = _property_info(session, {o.property_id for o in options})
    for o in options:
        o.info = info.get(o.property_id, {})
    return Resolution("choice", "case_lost", link, None, "olx", options=options, found=found_all)


def resolve(session, link) -> Resolution:
    """`links.parse(...)` → що знайдено в базі (див. докстрінг модуля)."""
    if not isinstance(link, links.Link):
        return Resolution("invalid", getattr(link, "reason", None) or "unrecognized", link,
                          family=getattr(link, "family", None))
    if link.case_lost:
        return _case_lost(session, link)
    if link.family == "own":
        return _own(session, link)
    if link.key is None or len(link.candidates) > 1:
        return _candidates(session, link,
                           missing="number_not_found" if link.family == "number" else "not_in_db")
    return _from_found(session, link, link.key, find_rows(session, [link.key]),
                       missing="not_in_db")


def found_on_page(rows, hl: str | None) -> list[Found]:
    """Оголошення сторінки квартири, які підсвітити за ?hl=<ключ>.

    `rows` — ORM-рядки квартири (вже завантажені сторінкою: жодного нового
    запиту). Ключ збігається з site_key або з (source:external_id) — власні id
    LUN, flombu, Благо.
    """
    if not hl:
        return []
    return [found_of(r, unconfirmed=_unconfirmed(r)) for r in rows
            if r.site_key == hl or f"{r.source}:{r.external_id}" == hl]


def valid_key(text: str | None) -> str | None:
    """?hl= / ключ із запиту → ключ «сайт:id», якщо це саме він (інакше None).

    Той самий вираз, що й явна форма «сімейство:id» розбору (links.toml
    bare.family_key): будь-що інше (розмітка, лапки, задовгий рядок) — None, і
    сторінка відкривається як звичайно.
    """
    if not text or len(text) > 64:
        return None
    got = links.parse(text)
    if isinstance(got, links.Link) and got.via == "family_key" and got.key == text \
            and got.family != "own":
        return got.key
    return None
