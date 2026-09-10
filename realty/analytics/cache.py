"""Кеш агрегатів: сторінка не перераховує базу на кожне відкриття.

Знімок будується один раз і живе, доки база не змінилась. Ознака зміни —
кількість оголошень і час останнього оновлення: обидва беруться одним дешевим
запитом, тож перевірка коштує міліcекунди, а перерахунок — лише коли треба.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from ..models import Listing, Property
from .segments import Universe, build_universe, below_threshold, segment_table
from .settings import load
from .sources import composition, matched

# Стеля життя знімка. Потрібна на випадок, коли база змінилась так, що
# лічильники лишились ті самі — краще зайвий перерахунок за 0.1 с, ніж
# застарілі цифри.
TTL = 600.0

_lock = threading.Lock()


@dataclass
class Snapshot:
    version: tuple
    built_at: float
    universe: Universe
    segments: list[dict]
    below: dict
    sources_matched: dict
    sources_composition: list[dict]

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.built_at


_current: Snapshot | None = None


def _version(session) -> tuple:
    """Ознака того, що знімок застарів.

    Кількість майстер-записів тут не для краси: дедуплікація перебудовує
    `properties`, не чіпаючи `listings.last_seen`, тож без цього поля знімок
    після перезведення лишався б старим до кінця TTL.
    """
    total, updated = session.execute(
        select(func.count(Listing.id), func.max(Listing.last_seen))).one()
    props = session.scalar(select(func.max(Property.id))) or 0
    count = session.scalar(select(func.count(Property.id))) or 0
    return (total, updated.isoformat() if isinstance(updated, datetime) else None,
            props, count)


def _build(session) -> Snapshot:
    cfg = load()
    universe = build_universe(session)
    return Snapshot(
        version=_version(session), built_at=time.monotonic(), universe=universe,
        segments=segment_table(universe, cfg), below=below_threshold(universe, cfg),
        sources_matched=matched(session, cfg), sources_composition=composition(session),
    )


def get(session, *, force: bool = False) -> Snapshot:
    """Актуальний знімок — з кешу або перебудований."""
    global _current
    with _lock:
        if not force and _current is not None and _current.age_seconds < TTL:
            if _current.version == _version(session):
                return _current
        _current = _build(session)
        return _current


def invalidate() -> None:
    """Скидає кеш — після збору, дедуплікації чи зміни налаштувань."""
    global _current
    with _lock:
        _current = None
