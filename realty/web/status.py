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
        by_quality = dict(s.execute(
            select(Listing.quality_status, func.count())
            .group_by(Listing.quality_status)).all())
    return {
        "listings": listings,
        "properties": properties,
        "merged": listings - properties,      # скільки оголошень виявились дублями
        "multi_source": multi,
        "unassigned": stale,                  # ще не пройшли дедуплікацію
        "per_source": per_source,
        "quality": by_quality,
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
        "autonomy": _autonomy(),
        "quality_24h": ops.quality_totals(24),
        "jobs": live,
        "runs": ops.recent_runs(10),
    }


def _autonomy() -> dict:
    """Те, що відповідає на питання «чи живе система без людини»."""
    from .. import backup, watchdog

    cycles = ops.last_cycles(5)
    last_bk = backup.last_attempt()
    alerts = {k: v for k, v in watchdog.load_state().items() if not k.startswith("_")}
    return {
        "last_success": as_utc_iso(ops.last_success_at()),
        "cycles": [{"status": c.status, "started_at": as_utc_iso(c.started_at),
                    "finished_at": as_utc_iso(c.finished_at), "kept": c.kept,
                    "message": c.message} for c in cycles],
        "backup": None if last_bk is None else {
            "status": last_bk.status, "at": as_utc_iso(last_bk.created_at),
            "size_mb": round((last_bk.size or 0) / 1e6, 1), "offsite": last_bk.offsite,
            "message": last_bk.message},
        "backup_last_success": as_utc_iso(backup.last_success_at()),
        "public_url": watchdog.public_url(),
        "alerts": [{"key": k, "since": v.get("since"), "text": v.get("text")}
                   for k, v in alerts.items()],
    }


@router.get("/api/status")
def api_status():
    return JSONResponse(build_status())


@router.get("/api/status/reports")
def api_reports(limit: int = 50):
    """Скарги «дані не збігаються» — накопичений список підтверджених помилок.

    По ньому видно, які саме правила класифікації ламаються найчастіше. Це
    найдешевше джерело для наступних виправлень: показує людина, яка справді
    відкрила оголошення, а не вибіркова перевірка.
    """
    from ..models import DataReport

    with SessionLocal() as s:
        rows = s.scalars(
            select(DataReport).where(DataReport.resolved_at.is_(None))
            .order_by(DataReport.created_at.desc()).limit(min(limit, 200))).all()
        by_field: dict[str, int] = {}
        for row in rows:
            by_field[row.field or "інше"] = by_field.get(row.field or "інше", 0) + 1
        return JSONResponse({
            "total": len(rows),
            "by_field": sorted(by_field.items(), key=lambda kv: -kv[1]),
            "items": [{
                "id": r.id, "listing_id": r.listing_id,
                # Квартиру визначаємо за оголошенням, а не за номером, записаним у
                # момент скарги: до 22.09.2026 номери квартир зсувались при кожній
                # перебудові, і збережений номер міг уже належати чужій квартирі.
                "property_id": (s.get(Listing, r.listing_id).property_id
                                if s.get(Listing, r.listing_id) else r.property_id),
                "property_id_at_report": r.property_id, "field": r.field,
                "created_at": r.created_at.isoformat(),
                "snapshot": r.snapshot or {},
            } for r in rows],
        })


@router.get("/api/status/dedup")
def api_dedup():
    """Зведення квартир: самоперевірка, черга на перегляд, щотижнева вибірка (D41)."""
    import json

    from .. import dedup, dedup_audit, dedup_sample
    from ..models import DedupDecision

    audits = dedup_audit.recent(9)
    last = audits[0] if audits else None
    with SessionLocal() as s:
        decisions = s.scalar(select(func.count()).select_from(DedupDecision)
                             .where(DedupDecision.active.is_(True))) or 0
    return JSONResponse({
        "rules": sorted(dedup.active_rules()),
        "rule_labels": dedup.RULE_LABELS,
        "kinds": dedup_audit.KINDS,
        "missed_kinds": dedup_audit.MISSED,
        "decisions": decisions,
        "last": None if last is None else {
            "at": as_utc_iso(last.at), "properties": last.properties,
            "suspicious": last.suspicious, "missed": last.missed,
            "previous": audits[1].suspicious if len(audits) > 1 else None,
            "by_kind": json.loads(last.by_kind or "{}"),
            "queue": json.loads(last.queue or "[]")[:40],
            "missed_list": json.loads(last.missed_list or "[]")[:20],
        },
        "history": [{"at": as_utc_iso(a.at), "suspicious": a.suspicious, "missed": a.missed}
                    for a in audits],
        "samples": [{"at": as_utc_iso(x.at), "n": x.n, "one": x.one, "several": x.several,
                     "unclear": x.unclear, "error_share": x.error_share,
                     "details": json.loads(x.details or "[]")}
                    for x in dedup_sample.recent(6)],
    })


@router.get("/api/status/liveness")
def api_liveness():
    """Панель «Зняті оголошення» (Блок 1, E8, D52) — лише власник (префікс /api/status).

    Схема /api/status не змінюється (інтеграція, конфлікт 9): тут готове зведення
    останнього прогону циклу з ops.liveness_runs (його рахує сам крок) і поточний
    стан запобіжника — два короткі читання ops.db, без агрегацій на запит.
    """
    import json

    from .. import configfiles
    from ..liveness import fuse

    ops.init_ops()
    with ops.ops_session() as s:
        last = s.scalars(select(ops.LivenessRun)
                         .where(ops.LivenessRun.report.isnot(None))
                         .order_by(ops.LivenessRun.id.desc()).limit(1)).first()
        latest = s.scalars(select(ops.LivenessRun)
                           .order_by(ops.LivenessRun.id.desc()).limit(1)).first()

        def run_of(r):
            if r is None:
                return None
            return {"id": r.id, "kind": r.kind, "status": r.status,
                    "started_at": as_utc_iso(r.started_at),
                    "finished_at": as_utc_iso(r.finished_at), "requests": r.requests,
                    "checked": r.checked, "removed": r.removed, "returned": r.returned,
                    "config_hash": r.config_hash, "fuse_mode": r.fuse_mode,
                    "per_host": json.loads(r.per_host or "{}"),
                    "fuse": json.loads(r.fuse or "{}"), "message": r.message}

        body = {"report": json.loads(last.report) if last is not None else None,
                "report_run": run_of(last), "latest_run": run_of(latest)}
    try:
        cfg = configfiles.get("liveness")
        body["fuse_mode"] = cfg.fuse.mode
        body["fuse_rule"] = {"share": cfg.fuse.share, "min_checked": cfg.fuse.min_checked,
                             "share_test": cfg.fuse.share_test,
                             "hinted_share": cfg.fuse.hinted_share,
                             "canary_trip_min": cfg.fuse.canary_trip_min,
                             "sweep_min_checked": cfg.fuse.sweep_min_checked,
                             "sweep_share": {h: s.sweep_share for h, s in cfg.hosts.items()
                                             if s.checkable},
                             "random_window_hours": cfg.fuse.random_window_hours}
    except configfiles.ConfigError as e:
        log.error("config/liveness.toml не читається: %s", e)
        body["fuse_mode"] = None
    body["fuse"] = fuse.state()
    return JSONResponse(body)


@router.get("/api/status/places")
def api_places():
    """Панель «Райони й ЖК» (Блок 4, E10, D57) — лише власник (префікс /api/status).

    Готове зведення останнього прогону кроку «райони й ЖК» з ops.places_runs (його
    рахує сам крок): охоплення до/після, ступені, точність, would_change (зміни
    непорожніх ключів, НЕ застосовані), нерозпізнані назви, ЖК без району. Одне
    читання ops.db, без агрегацій на запит (інтеграція, конфлікт 10).
    """
    from ..places import commands

    return JSONResponse({"last": commands.last_run()})


@router.get("/api/status/night")
def api_night():
    """Нічні вікна й дозбір доказів Блоків 3/4 (E11, D60) — лише власник (префікс /api/status).

    Читає готові рядки ops.night_runs (їх пише диригент; покриття доказами рахується в
    кінці вікна) — без агрегацій по listings на запит (інтеграція, конфлікт 10).
    """
    from ..night import report as night_report

    def brief(d: dict) -> dict:
        ev = d.get("evidence") or {}
        lanes = {h: {k: x.get(k) for k in ("requests", "blocked", "stopped", "evidence_requests",
                                           "evidence_blocked", "identity_requests", "not_reached")}
                 for h, x in (d.get("lanes") or {}).items()}
        return {"id": d["id"], "night_date": d.get("night_date"), "window": d.get("window"),
                "status": d["status"], "started_at": as_utc_iso(d.get("started_at")),
                "finished_at": as_utc_iso(d.get("finished_at")), "message": d.get("message"),
                "lanes": lanes, "evidence": {k: v for k, v in ev.items() if k != "coverage"}}

    try:
        rows = night_report.runs(limit=6)
    except Exception as e:                               # noqa: BLE001 — панель не валить сайт
        log.error("ops.night_runs не читається: %s", e)
        return JSONResponse({"nights": [], "coverage": None, "error": str(e)[:200]})
    last = next((d for d in rows if (d.get("evidence") or {}).get("coverage")), None)
    return JSONResponse({
        "nights": [brief(d) for d in rows],
        "coverage": last["evidence"]["coverage"] if last else None,
        "coverage_at": as_utc_iso(last.get("finished_at")) if last else None,
        "coverage_window": last["id"] if last else None})


@router.post("/api/status/liveness-fuse")
def api_liveness_fuse(payload: dict = Body(default={})):
    """Зняти запобіжник джерела (лише власник; same-origin — як для будь-якого POST).

    Після зняття наступний прогін перевіряє «знято», не застосоване під
    запобіжником (ярус held), і застосовує вже за звичайними правилами.
    """
    from ..liveness import fuse

    source = str(payload.get("source") or "").strip()
    if payload.get("action") != "clear" or not source:
        return JSONResponse({"ok": False, "error": "потрібно source і action=clear"},
                            status_code=400)
    released = fuse.clear(source, by="owner:/status")
    log.warning("Запобіжник перевірки актуальності: %s — %s", source,
                "знято власником" if released else "не тримався")
    return JSONResponse({"ok": True, "source": source, "released": released})


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
    from .. import configfiles
    from .app import templates

    # Інтервали опитування й пауза у фоновій вкладці — config/speed.toml
    # [status_poll] (Блок 2, крок E5, D50). Зламаний конфіг не валить сторінку:
    # тоді — інтервали, що стояли в шаблоні до Блоку 2, без паузи.
    try:
        poll = configfiles.get("speed").status_poll
    except configfiles.ConfigError as e:
        log.error("config/speed.toml не читається — /status з інтервалами до Блоку 2: %s", e)
        poll = None
    return templates.TemplateResponse(request, "status.html",
                                      {"sources": list(SOURCES), "poll": poll})
