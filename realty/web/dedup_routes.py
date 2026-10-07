"""Ручне виправлення зведення (D41) — лише власник (префікс /api/dedup в OWNER_ONLY).

Рішення зберігається в `dedup_decisions`, має пріоритет над правилами й
застосовується одразу; наступні перебудови його не скасовують.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..analytics import cache
from ..db import session_scope
from ..dedup import merge_into, split_off
from ..models import DedupDecision, Listing
from . import speedcache


def _refresh(property_ids) -> None:
    """Дію власника видно одразу (Блок 2, крок E5, D50).

    Знімок «Аналітики» оновлюється лише для зачеплених квартир (нова квартира
    після «розділити» відкривається одразу, а не 404 до перебудови; та, що
    зникла після «злити», — прибрана), зведення перераховуються фоном. Кеш
    списку — за версією даних процесу й явною позначкою власника.
    """
    with session_scope() as s:
        cache.patch_properties(s, [p for p in property_ids if p is not None])
    speedcache.owner_changed("dedup")

router = APIRouter()


def _fail(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


@router.post("/api/dedup/split")
def api_split(payload: dict = Body(default={})):
    try:
        pid = int(payload.get("property_id"))
        ids = [int(x) for x in payload.get("listing_ids") or []]
    except (TypeError, ValueError):
        return _fail("незрозумілий запит")
    try:
        with session_scope() as s:
            new_pid = split_off(s, pid, ids)
    except ValueError as e:
        return _fail(str(e))
    _refresh([pid, new_pid])
    return {"ok": True, "property_id": pid, "new_property_id": new_pid}


@router.post("/api/dedup/merge")
def api_merge(payload: dict = Body(default={})):
    # Приймаємо і номер, і вставлене посилання на сторінку квартири.
    m = re.search(r"(\d+)\D*$", str(payload.get("other") or ""))
    try:
        pid = int(payload.get("property_id"))
    except (TypeError, ValueError):
        return _fail("незрозумілий запит")
    if not m:
        return _fail("вкажіть номер квартири або посилання на неї")
    other = int(m.group(1))
    try:
        with session_scope() as s:
            kept = merge_into(s, pid, other)
    except ValueError as e:
        return _fail(str(e))
    _refresh([pid, other, kept])
    return {"ok": True, "property_id": kept}


@router.post("/api/dedup/decisions/{decision_id}/undo")
def api_undo(decision_id: int):
    with session_scope() as s:
        d = s.get(DedupDecision, decision_id)
        if d is None or not d.active:
            return _fail("такого чинного рішення немає", 404)
        d.active = False
    speedcache.owner_changed("dedup_undo")
    return {"ok": True, "note": "поділ квартир повернеться до правил на найближчій перебудові"}


def decisions_for(session, listing_ids: set[int]) -> list[dict]:
    """Чинні рішення власника, що стосуються цих оголошень — для сторінки квартири."""
    out = []
    for d in session.scalars(select(DedupDecision).where(DedupDecision.active.is_(True))
                             .order_by(DedupDecision.id.desc())):
        touched = (set(d.left or []) | set(d.right or [])) & listing_ids
        if touched:
            out.append({"id": d.id, "kind": d.kind, "at": d.created_at,
                        "other_property_id": d.other_property_id,
                        "property_id": d.property_id, "listings": len(touched)})
    return out
