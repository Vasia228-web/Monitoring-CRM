"""Позначка «актуальність не підтверджена» на сайті (рішення власника 3, D46; E8, D52).

Благо не перевіряється (будь-яка адреса планування веде на каталог), тож його
оголошення ніколи не знімаються автоматично — і сайт має це показувати. Текст і
підказка — один ключ config/liveness.toml [ui] для списку, картки квартири й /find
(інтеграція, конфлікт 17). Обчислюється з хоста оголошення (policy), без колонки
в базі; видно обом ролям.

  * рядок списку (згорнутого в квартири) — якщо ЖОДНЕ актуальне оголошення
    квартири не на хості, що перевіряється; незгорнутого — за хостом рядка;
  * картка квартири — біля кожного оголошення з хоста, що не перевіряється;
  * /find (Блок 5) — `marker_for_url(url)`.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from .. import configfiles
from ..models import Listing
from . import policy as pol

log = logging.getLogger(__name__)
_logged: set[str] = set()


def _cfg():
    try:
        return pol.current()
    except configfiles.ConfigError as e:
        text = str(e)
        if text not in _logged:
            _logged.add(text)
            log.error("config/liveness.toml не читається — позначку «не підтверджено» "
                      "не показуємо: %s", text)
        return None


def marker() -> dict | None:
    """{"label", "hint"} для шаблонів; None — конфіг зламаний (тоді без позначки)."""
    cfg = _cfg()
    if cfg is None:
        return None
    return {"label": cfg.ui.unconfirmed_label, "hint": cfg.ui.unconfirmed_hint}


def unconfirmed_url(url: str | None, cfg=None, site_key: str | None = None) -> bool:
    """Оголошення з хоста, який не перевіряється (відомий хост із checkable = false)."""
    cfg = cfg or _cfg()
    if cfg is None or not url:
        return False
    host = pol.host_for(cfg, site_key, url)
    return host is not None and not cfg.hosts[host].checkable


def marker_for_url(url: str | None) -> dict | None:
    """Для /find (Блок 5): позначка, якщо адреса — з хоста, що не перевіряється."""
    return marker() if unconfirmed_url(url) else None


def list_rows(session, rows, *, collapse: bool) -> set[int]:
    """id рядків сторінки списку, біля яких показати позначку."""
    cfg = _cfg()
    if cfg is None or not rows:
        return set()
    out: set[int] = set()
    props = {r.property_id for r in rows if collapse and r.property_id is not None}
    unconfirmed_props: set[int] = set()
    if props:
        state: dict[int, bool] = {}
        for pid, site_key, url, active, manual in session.execute(
                select(Listing.property_id, Listing.site_key, Listing.original_url,
                       Listing.is_active, Listing.manual_active)
                .where(Listing.property_id.in_(props))):
            effective = manual if manual is not None else active
            if not effective:
                continue
            ok = not unconfirmed_url(url, cfg, site_key)
            state[pid] = state.get(pid, False) or ok
        unconfirmed_props = {pid for pid, any_checkable in state.items() if not any_checkable}
    for r in rows:
        if collapse and r.property_id is not None:
            if r.property_id in unconfirmed_props:
                out.add(r.id)
        elif unconfirmed_url(r.original_url, cfg, r.site_key):
            out.add(r.id)
    return out
