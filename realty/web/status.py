"""Сторінка стану системи та ручне управління збором.

Читає телеметрію з окремої бази `data/ops.db` і зведення — з основної.
Сама нічого в основну базу не пише: кнопки лише запускають окремі процеси
`cli.py scrape`, тож помилка збору не може повалити вебсервер.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from .. import ops
from ..ops import as_utc_iso
from ..config import EXPECTED_TOTALS, ROOT, SOURCES
from ..db import SessionLocal
from ..models import Listing, Property

log = logging.getLogger(__name__)
router = APIRouter()

# Запущені вручну процеси: {джерело: Popen}. Тримаємо, щоб не стартувати
# другий збір того самого джерела поверх першого.
_jobs: dict[str, subprocess.Popen] = {}
_jobs_lock = threading.Lock()


def _alive(source: str) -> bool:
    with _jobs_lock:
        proc = _jobs.get(source)
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        _jobs.pop(source, None)
        return False


def running_jobs() -> list[str]:
    return [name for name in list(_jobs) if _alive(name)]


def _data_quality() -> dict:
    with SessionLocal() as s:
        listings = s.scalar(select(func.count()).select_from(Listing)) or 0
        properties = s.scalar(select(func.count()).select_from(Property)) or 0
        multi = s.scalar(
            select(func.count()).select_from(Property).where(Property.sources_count > 1)
        ) or 0
        per_source = dict(
            s.execute(select(Listing.source, func.count()).group_by(Listing.source)).all()
        )
        stale = s.scalar(
            select(func.count()).select_from(Listing).where(Listing.property_id.is_(None))
        ) or 0
    return {
        "listings": listings,
        "properties": properties,
        "merged": listings - properties,      # скільки оголошень виявились дублями
        "multi_source": multi,
        "unassigned": stale,                  # ще не пройшли дедуплікацію
        "per_source": per_source,
    }


def build_status() -> dict:
    quality = _data_quality()
    stats = ops.source_stats(24)
    live = running_jobs()

    sources = []
    for name in SOURCES:
        collected = quality["per_source"].get(name, 0)
        expected = EXPECTED_TOTALS.get(name)
        st = stats.get(name, {})
        # Зібрати можна більше за оцінку: OLX показує максимум 1000 за раз,
        # але за кілька прогонів накопичується більше. Тоді джерело просто
        # вичерпане, а не «зібране на 123%».
        complete = bool(expected) and collected >= expected
        sources.append({
            "name": name,
            "collected": collected,
            "expected": expected,
            "complete": complete,
            "remaining": max(0, expected - collected) if expected else None,
            "coverage": (100.0 if complete else round(100 * collected / expected, 1))
                        if expected else None,
            "success_rate": st.get("success_rate"),
            "requests_ok": st.get("requests_ok", 0),
            "requests_failed": st.get("requests_failed", 0),
            "blocked_24h": st.get("requests_blocked", 0),
            "new_24h": st.get("new", 0),
            "errors_24h": st.get("errors", 0),
            "last_success": as_utc_iso(st.get("last_success")),
            "running": name in live,
        })

    health = ops.worker_health()
    return {
        "generated_at": as_utc_iso(ops._now()),
        "worker": {
            "state": health["state"],
            "beat_at": as_utc_iso(health["beat_at"]),
            "age_min": health["age_min"],
            "heartbeats": health["counter"],
            "pid": health["pid"],
            "running_now": health["running"],
            "last_run": as_utc_iso(health["last_run"]),
            "alert": health["alert"],
            "idle_after_min": ops.IDLE_AFTER_MIN,
            "down_after_min": ops.DOWN_AFTER_MIN,
        },
        "sources": sources,
        "quality": quality,
        "llm": {"all_time": ops.llm_totals(), "last_24h": ops.llm_totals(24)},
        "jobs": live,
        "runs": ops.recent_runs(10),
    }


@router.get("/api/status")
def api_status():
    return JSONResponse(build_status())


@router.get("/api/status/runs")
def api_runs(limit: int = 20):
    return JSONResponse(ops.recent_runs(min(limit, 100)))


@router.post("/api/status/run")
def api_run(payload: dict = Body(default={})):
    """Запускає збір окремим процесом. `source`: ім'я джерела або `all`."""
    source = str(payload.get("source") or "all")
    if source != "all" and source not in SOURCES:
        return JSONResponse({"ok": False, "error": f"невідоме джерело: {source}"},
                            status_code=400)
    if _alive(source):
        return JSONResponse({"ok": False, "error": "цей збір уже виконується"},
                            status_code=409)

    cmd = [sys.executable, "cli.py", "scrape", "--trigger", "manual"]
    if source != "all":
        cmd += ["--sources", source]
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    handle = (logs / f"manual-{source}.log").open("a", encoding="utf-8")
    handle.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} запуск із дашборда ===\n")
    handle.flush()
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=handle,
                            stderr=subprocess.STDOUT, start_new_session=True)
    with _jobs_lock:
        _jobs[source] = proc
    log.info("Дашборд запустив збір: %s (pid %s)", source, proc.pid)
    return JSONResponse({"ok": True, "source": source, "pid": proc.pid})


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    from .app import templates

    return templates.TemplateResponse(request, "status.html",
                                      {"sources": list(SOURCES)})
