"""Один прогін перевірки актуальності: план → смуги → запобіжник → запис (E8, D52).

Викликають: крок циклу «перевірка актуальності» (`cli.py verify` → verify.verify_batch
без ids — яруси черги), процес перевірки при відкритті (verify_batch(ids=…,
reason="opened")), ручний `cli.py liveness run`. Кожен прогін — рядок
ops.liveness_runs; прогін циклу ще й готує зведення для /status (`report`), яке
сайт лише читає (інтеграція, конфлікт 9).
"""
from __future__ import annotations

import json
import logging
import time

from .. import ops
from . import apply, engine, fuse, policy as pol, queue, report

log = logging.getLogger(__name__)


def _start(kind: str, digest: str, mode: str) -> int | None:
    try:
        ops.init_ops()
        with ops.ops_session() as s:
            row = ops.LivenessRun(kind=kind, status="running", started_at=ops._now(),
                                  config_hash=digest[:16], fuse_mode=mode)
            s.add(row)
            s.flush()
            return row.id
    except Exception as e:                           # noqa: BLE001 — телеметрія не зупиняє перевірку
        log.warning("запис прогону перевірки не створено: %s", e)
        return None


def _finish(run_id: int | None, **fields) -> None:
    if run_id is None:
        return
    try:
        with ops.ops_session() as s:
            row = s.get(ops.LivenessRun, run_id)
            if row is None:
                return
            row.finished_at = ops._now()
            for k, v in fields.items():
                setattr(row, k, json.dumps(v, ensure_ascii=False, default=str)
                        if isinstance(v, (dict, list)) else v)
    except Exception as e:                           # noqa: BLE001
        log.warning("запис прогону перевірки %s не закрито: %s", run_id, e)


def deferred_opened_jobs() -> dict[int, int]:
    """{завдання: квартира} відкладених перевірок при відкритті (чекають кінця циклу)."""
    from ..lookup import opened, queue as jobs

    from .. import configfiles

    try:
        cfg = configfiles.get("speed").open_check
        pairs = jobs.unfinished(jobs.KIND_OPENED, timeout_s=cfg.job_timeout_s,
                                deferred_max_age_s=opened.DEFERRED_MAX_AGE_S)
    except Exception as e:                           # noqa: BLE001 — запасний шлях ярусу opened
        log.warning("черга перевірок при відкритті не прочитана: %s", e)
        return {}
    out = {}
    for job_id, prop in pairs:
        job = jobs.get(job_id)
        if job is not None and job.state == "deferred" and prop is not None:
            out[job_id] = int(prop)
    return out


def _close_jobs(jobs: dict[int, int], job_keys: dict[int, set[str]], outcomes,
                rep: apply.ApplyReport) -> list[int]:
    """Відкладені завдання, чиї ключі цей прогін перевірив УСІ, — «done» з результатом
    квартири; решту (стеля ярусу, стеля часу) після циклу виконає процес перевірки."""
    from ..lookup import queue as jq

    checked = {oc.item.key for oc in outcomes if oc.verdict is not None}
    closed = []
    for job_id, prop in jobs.items():
        keys = job_keys.get(job_id)
        if not keys or not keys <= checked:
            continue
        got = rep.by_property.get(prop, {})
        result = {k: int(got.get(k, 0)) for k in ("checked", "alive", "delisted", "restored",
                                                  "unknown")}
        result.update(requests=0, blocked=0)
        if jq.finish(job_id, "done", result=result, only_if=("deferred",),
                     message="перевірено кроком циклу"):
            closed.append(job_id)
    return closed


class ApplyDeferred(Exception):
    """`before_apply` сказав «не зараз» (напр., почався цикл збору): вердикти мережевої
    фази не застосовано, нічого не записано; прогін закрито станом «deferred»."""


def run(*, kind: str = "cycle", ids=None, keys=None, reason: str = "sweep", hosts=None,
        limit_per_host: int | None = None, fetcher=None, scope=None, hooks=None,
        now_fn=None, budget_s: float | None = None, started: float | None = None,
        before_apply=None) -> dict:
    """Прогін і застосування; повертає статистику у форматі verify_batch.

    `started` — time.monotonic() старту процесу кроку (cli.py): стеля мережевої
    фази `run.budget_minutes` рахується від нього, а не від кінця плану (рецензія E8,
    D52). Малий прогін (не `cycle`) рахує запобіжник разом із перевірками за вікно
    `fuse.window_hours` (fuse.prior_counts); у tiered пул випадкових і контрольних, що
    сам не набрав min_checked, — за `fuse.random_window_hours`, і в кроці циклу (D56).
    """
    from ..db import session_scope
    from ..fetcher import Fetcher

    t0 = started if started is not None else time.monotonic()
    now_fn = now_fn or queue._now
    cfg, digest = pol.load_with_hash()
    scope = scope or session_scope
    hooks = list(hooks) if hooks is not None else default_hooks(cfg)
    run_id = _start(kind, digest, cfg.fuse.mode)
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "requests": 0, "blocked": 0, "blocked_sources": [], "by_source": {},
             "by_host": {}, "by_signature": {}, "by_tier": {}, "tiers": {}, "trips": [],
             "held_sources": [], "repaired": 0, "not_found": 0, "run_id": run_id}
    own = fetcher is None
    try:
        held = fuse.held_sources()
        jobs: dict[int, int] = {}
        job_keys: dict[int, set[str]] = {}
        now = now_fn()
        with scope() as s:
            if ids is not None:
                items = queue.items_for_rows(s, cfg, ids, reason, now=now)
            elif keys is not None:
                items = queue.items_for_keys(s, cfg, keys, reason, now=now)
            else:
                if kind == "cycle":
                    jobs = deferred_opened_jobs()
                plan = queue.plan_run(s, cfg, now=now, hosts=hosts,
                                      limit_per_host=limit_per_host, jobs=jobs,
                                      held_sources=held)
                items = plan.items
                job_keys = plan.job_keys
                stats["tiers"] = plan.tiers
        if hosts:
            items = [i for i in items if i.host in hosts]
        if not items:
            _finish(run_id, status="ok", message="нічого перевіряти")
            return stats
        fetcher = fetcher or Fetcher(delay=1.0, use_cache=False, label="verify")
        budget = budget_s if budget_s is not None else cfg.run.budget_minutes * 60
        outcomes, lanes = engine.run_items(engine.by_host(items), fetcher=fetcher, cfg=cfg,
                                           hooks=hooks, deadline=t0 + budget, now_fn=now_fn)
        if before_apply is not None and not before_apply():
            # «Перевірити зараз» за посиланням: цикл почався, поки йшли запити, — запис
            # під час циклу не робиться ніколи (рецензія E14, 08.10).
            raise ApplyDeferred("застосування відкладено (почався цикл)")
        # Вікно для пулів, що самі не набрали min_checked: малий прогін — усі пули; крок
        # циклу — лише пул випадкових і контрольних у tiered (D56: rieltor, lun, flombu
        # набирають min_checked лише за кілька циклів).
        with scope() as s:
            prior = fuse.prior_counts(s, cfg, now_fn(), cycle=kind == "cycle")
        rep = apply.apply_outcomes(outcomes, cfg=cfg, scope=scope, run_id=run_id, prior=prior)
        closed = _close_jobs(jobs, job_keys, outcomes, rep) if jobs else []
        for host, ln in lanes.items():
            stats["requests"] += ln.requests
            stats["blocked"] += ln.blocked
            stats["by_host"][host] = {"requests": ln.requests, "blocked": ln.blocked,
                                      "stopped_early": ln.stopped_early,
                                      "skipped": ln.skipped, "seconds": ln.seconds,
                                      "signatures": ln.signatures}
        stats.update(checked=rep.checked, alive=rep.alive, delisted=rep.delisted,
                     restored=rep.restored, unknown=rep.unknown, by_source=rep.by_source,
                     by_signature=rep.by_signature, by_tier=rep.by_tier, trips=rep.trips,
                     held_sources=rep.held_sources, repaired=rep.repaired,
                     not_found=rep.not_found, stale=rep.stale, jobs_closed=closed,
                     longest_batch_s=rep.longest_batch_s, batches=rep.batches,
                     fuse_pools=rep.fuse_pools, canary_genuine=rep.canary_genuine)
        stats["blocked_sources"] = sorted(h for h, ln in lanes.items() if ln.stopped_early)
        status_report = None
        if kind == "cycle":
            try:
                with scope() as s:
                    status_report = report.status_block(s, cfg, now=now_fn())
            except Exception as e:                   # noqa: BLE001 — зведення не скасовує прогону
                log.warning("зведення для /status не пораховано: %s", e)
        _finish(run_id, status="ok", requests=stats["requests"], checked=rep.checked,
                removed=rep.delisted, returned=rep.restored, per_host=stats["by_host"],
                per_source=rep.by_source, per_tier={"plan": stats["tiers"],
                                                    "verdicts": rep.by_tier},
                fuse={"trips": rep.trips, "held": rep.held_sources, "mode": cfg.fuse.mode,
                      "pools": rep.fuse_pools, "canary_genuine": rep.canary_genuine},
                report=status_report)
        return stats
    except ApplyDeferred as e:
        _finish(run_id, status="deferred", message=str(e)[:500])
        raise
    except BaseException as e:
        _finish(run_id, status="failed", message=f"{type(e).__name__}: {e}"[:500])
        raise
    finally:
        if own and fetcher is not None:
            fetcher.close()


def default_hooks(cfg) -> list:
    """Гачки доказів Блоків 3/4 на тілах GET (capture.py)."""
    from . import capture

    return capture.default_hooks(cfg)
