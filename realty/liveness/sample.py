"""Контрольна вибірка Блоку 1: випадкові актуальні + контрольні, без зміни стану (D58).

Рішення власника 08.10 (D55 п. 7) і критерій приймання Блоку 1: раз на тиждень
перевіряти `per_source` (100) випадкових АКТУАЛЬНИХ оголошень кожного джерела, що
перевіряється (актуальне — як на сайті: якість «ok» і чинна актуальність), плюс
контрольні — відомо живі ключі, той самий вибір, що й ярус canary
(`queue.canary_keys`, за ім'ям). «Знято» серед випадкових ≤ max_removed_share (5%) —
гаразд; кожне «знято» — з «бачили в стрічці X год тому, звичайна перевірка Y год
тому», щоб власник бачив, чи це лише запізнення виявлення.

Як перевіряє. ТИМ САМИМ підписом і смугами, що й крок циклу (`engine.run_items`:
послідовно в межах хоста з його паузою, хости паралельно), під ТИМ САМИМ замком циклу
(`dbmigrate.wait_cycle_lock`): ні з циклом, ні з нічним диригентом паралельно не йде;
замок зайнятий довше за lock_wait_minutes — відмова з попередженням. Спершу
контрольні (усі хости разом), потім випадкові: контрольне показало «знято» — для його
джерела (і сайту) решту вибірки не перевіряємо (критична тривога, як спрацювання
запобіжника на контрольному).

Чого НЕ робить. Стану оголошень не змінює ніколи: apply.py не викликається, у
check_events нічого не пишеться (інакше незастосоване «знято» стало б ярусом held, який
запобіжник не рахує). Результати — лише ops.liveness_sample_runs і
ops.liveness_sample_checks. «Знято» серед випадкових — підказка звичайній перевірці:
відкладене завдання «opened» квартири (ops.lookup_checks), яке найближчий цикл бере
ярусом opened — через запобіжник. «Знято» понад fuse.share випадкових — лише критична
тривога, рішення власника; підказок для такого джерела немає.

`cli.py liveness sample [--per-source N] [--dry-run]`, звіт — `--report [--last N]`.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from .. import ops, runner

log = logging.getLogger(__name__)

TIER_RANDOM, TIER_CANARY = "sample", "canary"     # мітки WorkItem.tier (нічого не застосовується)
KIND_RANDOM, KIND_CANARY = "random", "canary"     # liveness_sample_checks.kind
ALIVE, REMOVED, NOT_FOUND, UNKNOWN = "alive", "removed", "not_found", "unknown"
NOT_REACHED, STOPPED = "not_reached", "stopped"
ANSWERED = (ALIVE, REMOVED, NOT_FOUND)

# Замок і прапорець — атрибутами модуля: тести підставляють свої шляхи (як lookup/opened).
LOCK_PATH = runner.LOCK_PATH
DISABLED_FLAG = runner.DISABLED_FLAG


@dataclass
class Pick:
    """Одне вибране оголошення (випадкове) чи контрольний ключ."""

    kind: str
    source: str
    key: str
    host: str
    listing_id: int | None
    property_id: int | None
    last_seen: datetime | None
    last_checked: datetime | None
    last_attempt: datetime | None
    outcome: str = NOT_REACHED
    signature: str | None = None
    code: int | None = None
    url: str | None = None
    detail: dict = field(default_factory=dict)
    checked_at: datetime | None = None
    hinted: bool = False


@dataclass
class Plan:
    random: dict[str, list[Pick]] = field(default_factory=dict)     # джерело → вибрані
    pools: dict[str, int] = field(default_factory=dict)              # джерело → актуальних
    skipped: dict[str, str] = field(default_factory=dict)            # джерело → чому ні
    canaries: list[Pick] = field(default_factory=list)


def _family(lcfg, host: str | None) -> str | None:
    from . import fuse

    return fuse.family_of_host(lcfg, host)


def build_plan(session, lcfg, scfg, *, per_source: int, now: datetime,
               rng: random.Random) -> Plan:
    """Випадкові актуальні (як на сайті) оголошення кожного джерела, що перевіряється, і
    контрольні — `queue.canary_keys` (той самий вибір, що й ярус canary), перші
    canaries_per_host на хост. Лише читання бази."""
    from ..config import enabled_sources
    from ..models import Listing, effective_active, is_clean
    from . import policy as pol, queue

    plan = Plan()
    for source in enabled_sources():
        rows = session.execute(
            select(Listing.id, Listing.site_key, Listing.original_url, Listing.property_id,
                   Listing.last_seen, Listing.last_checked, Listing.last_attempt)
            .where(Listing.source == source, is_clean(), effective_active().is_(True))
            .order_by(Listing.id)).all()
        pool, hosts_off = [], set()
        for lid, site_key, url, prop, seen, checked, attempt in rows:
            key = site_key or pol.row_key(lid)
            host = pol.host_for(lcfg, key, url)
            if host is None or not lcfg.hosts[host].checkable:
                hosts_off.add(host or "?")
                continue
            pool.append(Pick(KIND_RANDOM, source, key, host, lid, prop, seen, checked, attempt))
        plan.pools[source] = len(pool)
        if not pool:
            why = "; ".join(f"{h}: {lcfg.hosts[h].reason}" if h in lcfg.hosts else h
                            for h in sorted(hosts_off)) or "немає актуальних оголошень"
            plan.skipped[source] = why[:200]
            continue
        plan.random[source] = rng.sample(pool, min(per_source, len(pool)))
    if scfg.run.canaries_per_host > 0:
        u = queue.universe(session, lcfg, now=now)
        taken: dict[str, int] = {}
        for key in queue.canary_keys(u, lcfg):
            host = u.key_host.get(key)
            if host is None or taken.get(host, 0) >= scfg.run.canaries_per_host:
                continue
            taken[host] = taken.get(host, 0) + 1
            rows = u.groups[key]
            fresh = max(rows, key=lambda r: (r.last_seen or datetime.min, r.id))
            plan.canaries.append(Pick(
                KIND_CANARY, _family(lcfg, host) or host, key, host, fresh.id,
                fresh.property_id, max((r.last_seen for r in rows if r.last_seen), default=None),
                max((r.last_checked for r in rows if r.last_checked), default=None),
                max((r.last_attempt for r in rows if r.last_attempt), default=None)))
    return plan


def _record(pick: Pick, outcome, *, url: str | None = None) -> None:
    from . import policy as pol

    verdict, at = outcome
    if url:
        pick.url = pol.safe_url(url)
    if verdict is None:
        pick.outcome = NOT_REACHED
        return
    pick.outcome = verdict.kind
    pick.signature, pick.code, pick.checked_at = verdict.signature, int(verdict.code), at
    pick.detail = {k: v for k, v in (verdict.evidence or {}).items()
                   if isinstance(v, (str, int, float, bool, type(None)))}


def _verdict(source: str, picks: list[Pick], canaries: list[Pick], lcfg, scfg,
             stopped: set[str], now: datetime) -> dict:
    answered = [p for p in picks if p.outcome in ANSWERED]
    removed = [p for p in answered if p.outcome == REMOVED]
    n = len(answered)
    share = len(removed) / n if n else None
    c_done = [c for c in canaries if c.outcome != NOT_REACHED]
    c_removed = [c for c in c_done if c.outcome == REMOVED]
    if source in stopped:
        status = "canary"
    elif share is not None and n >= lcfg.fuse.min_checked and share > lcfg.fuse.share:
        status = "fuse_share"
    elif n < scfg.verdict.min_checked:
        status = "too_few"
    elif share > scfg.verdict.max_removed_share:
        status = "fail"
    else:
        status = "pass"
    shown = removed[:scfg.verdict.examples]
    return {
        "status": status, "picked": len(picks), "checked": n, "removed": len(removed),
        "alive": sum(p.outcome == ALIVE for p in picks),
        "not_found": sum(p.outcome == NOT_FOUND for p in picks),
        "unknown": sum(p.outcome == UNKNOWN for p in picks),
        "not_reached": sum(p.outcome == NOT_REACHED for p in picks),
        "stopped": sum(p.outcome == STOPPED for p in picks),
        "share": share, "max_share": scfg.verdict.max_removed_share,
        "fuse_share": lcfg.fuse.share, "fuse_min_checked": lcfg.fuse.min_checked,
        "canary_checked": len(c_done), "canary_removed": len(c_removed),
        "canary_examples": [c.url or c.key for c in c_removed[:5]],
        "examples": [{"listing_id": p.listing_id, "key": p.key, "url": p.url,
                      "signature": p.signature, "last_seen_ago": _h(p.last_seen, now),
                      "last_checked_ago": _h(p.last_checked, now),
                      "last_attempt_ago": _h(p.last_attempt, now), "hinted": p.hinted}
                     for p in shown],
        "examples_more": len(removed) - len(shown),
    }


def _hint(picks: list[Pick], now: datetime) -> int:
    """«Знято» → відкладене завдання «opened» квартири (ops.lookup_checks): найближчий
    цикл перевірить усі її ключі ярусом opened — через запобіжник. Без квартири —
    підказки немає (такий ключ дочекається сліпого обходу)."""
    from .. import configfiles
    from ..lookup import opened, queue as jobs

    timeout_s = configfiles.load("speed").open_check.job_timeout_s
    n = 0
    for p in picks:
        if p.property_id is None:
            continue
        key = jobs.opened_key(p.property_id)
        if jobs.active_for(key, timeout_s=timeout_s,
                           deferred_max_age_s=opened.DEFERRED_MAX_AGE_S) is None:
            ops.init_ops()
            with ops.ops_session() as s:
                s.add(ops.LookupCheck(kind=jobs.KIND_OPENED, key=key, property_id=p.property_id,
                                      state="deferred", created_at=now,
                                      message="контрольна вибірка: «знято» — підказка"))
        p.hinted = True
        n += 1
    return n


def _start(**fields) -> int:
    ops.init_ops()
    with ops.ops_session() as s:
        row = ops.LivenessSampleRun(status="running", started_at=ops._now(), **fields)
        s.add(row)
        s.flush()
        return row.id


def _finish(run_id: int, **fields) -> None:
    with ops.ops_session() as s:
        row = s.get(ops.LivenessSampleRun, run_id)
        row.finished_at = ops._now()
        for k, v in fields.items():
            setattr(row, k, json.dumps(v, ensure_ascii=False, default=str)
                    if isinstance(v, (dict, list)) else v)


def _write_checks(run_id: int, picks: list[Pick]) -> None:
    with ops.ops_session() as s:
        for p in picks:
            s.add(ops.LivenessSampleCheck(
                run_id=run_id, source=p.source, listing_id=p.listing_id,
                property_id=p.property_id, key=p.key[:64], host=p.host[:32], kind=p.kind,
                outcome=p.outcome, signature=p.signature, code=p.code,
                url=(p.url or "")[:300] or None,
                detail=json.dumps(p.detail, ensure_ascii=False, default=str)[:4000]
                if p.detail else None,
                checked_at=p.checked_at, last_seen=p.last_seen, last_checked=p.last_checked,
                last_attempt=p.last_attempt, hinted=p.hinted))


def _print_plan(plan: Plan, lcfg, scfg, out) -> None:
    from . import policy as pol

    out(f"КОНТРОЛЬНА ВИБІРКА — план (без мережі й запису): по {scfg.run.per_source} "
        f"випадкових актуальних + до {scfg.run.canaries_per_host} контрольних на хост")
    for source in sorted(set(plan.pools) | set(plan.skipped)):
        if source in plan.skipped:
            out(f"  {source:<10} пропуск: {plan.skipped[source]}")
            continue
        out(f"  {source:<10} актуальних, що перевіряються: {plan.pools[source]:>6}; вибрано "
            f"{len(plan.random.get(source, [])):>4}")
    keys: dict[str, set] = {}
    for p in [*plan.canaries, *(p for ps in plan.random.values() for p in ps)]:
        keys.setdefault(p.host, set()).add(p.key)
    for host, ks in sorted(keys.items()):
        pace = pol.pace(lcfg, host, scfg.run.pace)
        can = sum(c.host == host for c in plan.canaries)
        out(f"  {host:<20} ключів {len(ks):>4} (контрольних {can}) × {pace:.1f} с ≈ "
            f"{len(ks) * pace / 60:.0f} хв (+ час відповіді)")


def run(*, per_source: int | None = None, dry_run: bool = False, fetcher=None, scope=None,
        now_fn=None, out=print) -> int:
    """Один прогін вибірки. Код 0 — і коли вердикт поганий (тривогу шле сторож), і коли
    замок не дочекались; ненульовий — лише виняток (OnFailure → realty-alert@)."""
    from .. import configfiles
    from ..db import session_scope
    from ..dbmigrate import wait_cycle_lock
    from . import engine, existence, policy as pol, queue

    scfg, shash = configfiles.load_with_hash("sample")
    lcfg, lhash = pol.load_with_hash()
    per_source = per_source or scfg.run.per_source
    now_fn = now_fn or queue._now
    scope = scope or session_scope
    seed = f"{scfg.run.seed}:{now_fn().date().isoformat()}"
    if dry_run:
        with scope() as s:
            plan = build_plan(s, lcfg, scfg, per_source=per_source, now=now_fn(),
                              rng=random.Random(seed))
        _print_plan(plan, lcfg, scfg, out)
        return 0
    common = {"per_source": per_source, "seed": seed, "config_hash": shash[:16],
              "liveness_hash": lhash[:16]}
    if DISABLED_FLAG.exists():
        run_id = _start(**common)
        _finish(run_id, status="disabled", message="data/COLLECTOR_OFF — збір вимкнено")
        out("збір на цій машині вимкнено (COLLECTOR_OFF) — вибірку не запускаю")
        return 0
    run_id = _start(**common)
    try:
        t_wait = time.monotonic()
        lock = wait_cycle_lock(scfg.run.lock_wait_minutes, lock_path=LOCK_PATH)
        waited = round(time.monotonic() - t_wait, 1)
        if lock is None:
            msg = (f"цикл не звільнив замок за {scfg.run.lock_wait_minutes:g} хв — вибірку "
                   f"пропущено (попередження в зведенні)")
            _finish(run_id, status="lock_timeout", lock_waited_s=waited, message=msg)
            out(f"УВАГА: {msg}")
            return 0
        try:
            return _run_locked(run_id, scfg, lcfg, per_source=per_source, seed=seed,
                               waited=waited, fetcher=fetcher, scope=scope, now_fn=now_fn,
                               out=out, engine=engine, existence=existence)
        finally:
            lock.release()
    except BaseException as e:
        _finish(run_id, status="failed", message=f"{type(e).__name__}: {e}"[:500])
        raise


def _run_locked(run_id, scfg, lcfg, *, per_source, seed, waited, fetcher, scope, now_fn, out,
                engine, existence) -> int:
    from ..fetcher import Fetcher
    from . import queue

    t0 = time.monotonic()
    deadline = t0 + scfg.run.max_minutes * 60
    now = now_fn()
    with scope() as s:
        plan = build_plan(s, lcfg, scfg, per_source=per_source, now=now,
                          rng=random.Random(seed))
        canary_items = {i.key: i for i in queue.items_for_keys(
            s, lcfg, [c.key for c in plan.canaries], TIER_CANARY, now=now)}
        random_items = {i.key: i for i in queue.items_for_keys(
            s, lcfg, list(dict.fromkeys(p.key for ps in plan.random.values() for p in ps)),
            TIER_RANDOM, now=now)}
    own = fetcher is None
    fetcher = fetcher or Fetcher(delay=1.0, use_cache=False, label="verify")
    lanes: dict[str, dict] = {}

    def lane_stats(stats) -> None:
        for host, ln in stats.items():
            d = lanes.setdefault(host, {"requests": 0, "blocked": 0, "stopped_early": False,
                                        "skipped": 0, "seconds": 0.0})
            d["requests"] += ln.requests
            d["blocked"] += ln.blocked
            d["stopped_early"] = d["stopped_early"] or ln.stopped_early
            d["skipped"] += ln.skipped
            d["seconds"] = round(d["seconds"] + ln.seconds, 1)

    try:
        snaps = existence.load_snapshots(lcfg, existence.snapshot_sources(lcfg))
        got: dict[str, tuple] = {}
        # 1. Контрольні — усі хости разом; «знято» на відомо живому зупиняє джерело (і сайт).
        items = [canary_items[c.key] for c in plan.canaries if c.key in canary_items]
        outs, stats = engine.run_items(engine.by_host(items), fetcher=fetcher, cfg=lcfg,
                                       hooks=(), deadline=deadline, now_fn=now_fn,
                                       mode=scfg.run.pace, snapshots=snaps)
        lane_stats(stats)
        got.update({o.item.key: (o.verdict, o.at, o.item.url) for o in outs})
        for c in plan.canaries:
            v = got.get(c.key)
            _record(c, (v[0], v[1]) if v else (None, None), url=v[2] if v else None)
        stopped = {c.source for c in plan.canaries if c.outcome == REMOVED}
        # 2. Випадкові — без зупинених джерел і сайтів; ключ, уже перевірений контрольним,
        # не питаємо вдруге.
        todo = []
        for source, picks in plan.random.items():
            for p in picks:
                if source in stopped or _family(lcfg, p.host) in stopped:
                    p.outcome = STOPPED
                elif p.key not in got and p.key in random_items:
                    todo.append(random_items[p.key])
        todo = list({i.key: i for i in todo}.values())
        if todo:
            outs, stats = engine.run_items(engine.by_host(todo), fetcher=fetcher, cfg=lcfg,
                                           hooks=(), deadline=deadline, now_fn=now_fn,
                                           mode=scfg.run.pace, snapshots=snaps)
            lane_stats(stats)
            got.update({o.item.key: (o.verdict, o.at, o.item.url) for o in outs})
        for picks in plan.random.values():
            for p in picks:
                if p.outcome == STOPPED:
                    continue
                v = got.get(p.key)
                _record(p, (v[0], v[1]) if v else (None, None), url=v[2] if v else None)
    finally:
        if own:
            fetcher.close()
    # 3. Вердикти; підказки — лише джерелам без тривоги «рішення власника».
    verdicts: dict[str, dict] = {}
    for source in sorted(set(plan.random) | {c.source for c in plan.canaries}):
        verdicts[source] = _verdict(source, plan.random.get(source, []),
                                    [c for c in plan.canaries if c.source == source],
                                    lcfg, scfg, stopped, now)
    for source, why in plan.skipped.items():
        verdicts.setdefault(source, {"status": "skipped", "reason": why})
    hinted = 0
    if scfg.hints.enabled:
        for source, picks in plan.random.items():
            if verdicts[source]["status"] in ("canary", "fuse_share"):
                continue
            removed = [p for p in picks if p.outcome == REMOVED]
            hinted += _hint(removed, now)
            verdicts[source]["hinted"] = sum(p.hinted for p in removed)
            for e in verdicts[source]["examples"]:
                e["hinted"] = any(p.hinted for p in removed if p.listing_id == e["listing_id"])
    all_picks = [*plan.canaries, *(p for ps in plan.random.values() for p in ps)]
    _write_checks(run_id, all_picks)
    _finish(run_id, status="ok", lock_waited_s=waited,
            requests=sum(d["requests"] for d in lanes.values()), verdicts=verdicts, lanes=lanes,
            message=f"підказок звичайній перевірці: {hinted}" if hinted else None)
    out(render(runs(1, run_id)[0]))
    return 0

STATUS_UA = {"running": "триває", "ok": "гаразд", "lock_timeout":
             "НЕ ВІДБУЛАСЬ — цикл не звільнив замок", "disabled":
             "не запускалась — збір вимкнено (COLLECTOR_OFF)", "failed": "АВАРІЯ"}
VERDICT_UA = {"pass": "✅ гаразд", "fail": "⚠️ понад межу", "fuse_share":
              "🚨 понад межу запобіжника — рішення власника", "canary":
              "🚨 контрольне «знято» — джерело зупинено", "too_few": "замало відповідей",
              "skipped": "не перевіряється"}


def _j(text, empty):
    if not text:
        return empty
    try:
        return json.loads(text)
    except ValueError:
        return empty


def as_dict(row: ops.LivenessSampleRun) -> dict:
    return {"id": row.id, "started_at": row.started_at, "finished_at": row.finished_at,
            "status": row.status, "per_source": row.per_source, "seed": row.seed,
            "config_hash": row.config_hash, "liveness_hash": row.liveness_hash,
            "lock_waited_s": row.lock_waited_s, "requests": row.requests,
            "verdicts": _j(row.verdicts, {}), "lanes": _j(row.lanes, {}),
            "message": row.message}


def runs(limit: int = 1, run_id: int | None = None) -> list[dict]:
    """Останні прогони (новіші першими)."""
    ops.init_ops()
    with ops.ops_session() as s:
        stmt = select(ops.LivenessSampleRun).order_by(ops.LivenessSampleRun.id.desc())
        if run_id is not None:
            stmt = stmt.where(ops.LivenessSampleRun.id == run_id)
        return [as_dict(r) for r in s.scalars(stmt.limit(limit))]


def last_run() -> dict | None:
    got = runs(1)
    return got[0] if got else None


def _h(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "ніколи"
    h = (now - dt).total_seconds() / 3600
    return f"{h:.0f} год тому" if h < 48 else f"{h / 24:.1f} доби тому"


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def _source_line(source: str, v: dict) -> str:
    st = VERDICT_UA.get(v.get("status"), v.get("status"))
    if v.get("status") == "skipped":
        return f"  {source}: {st} ({v.get('reason') or '—'})"
    line = (f"  {source}: {st} — «знято» {v.get('removed', 0)} із {v.get('checked', 0)} "
            f"випадкових ({_pct(v.get('share'))}; межа {_pct(v.get('max_share'))})")
    if v.get("canary_checked"):
        line += (f"; контрольні: «знято» {v.get('canary_removed', 0)} із "
                 f"{v.get('canary_checked')}")
    extra = [f"без висновку {v['unknown']}" if v.get("unknown") else "",
             f"404 {v['not_found']}" if v.get("not_found") else "",
             f"не дійшла черга {v['not_reached']}" if v.get("not_reached") else ""]
    extra = [x for x in extra if x]
    return line + (f"; {', '.join(extra)}" if extra else "")


def _example_line(e: dict) -> str:
    return (f"     • {e.get('url') or e.get('key')} — {e.get('signature')}: бачили в стрічці "
            f"{e.get('last_seen_ago')}, звичайна перевірка — відповідь {e.get('last_checked_ago')}"
            f", спроба {e.get('last_attempt_ago')}"
            + (" → підказка звичайній перевірці" if e.get("hinted") else ""))


def render_short(d: dict, *, local=None) -> list[str]:
    """Кілька рядків для щоденного зведення."""
    when = local(d["started_at"]) if local else f"{d['started_at']:%d.%m %H:%M} UTC"
    out = [f"🎯 Контрольна вибірка №{d['id']} ({when}): "
           f"{STATUS_UA.get(d['status'], d['status'])}"]
    for source, v in sorted((d.get("verdicts") or {}).items()):
        out.append(_source_line(source, v))
    return out


def render(d: dict) -> str:
    """Звіт простою мовою (`cli.py liveness sample --report`)."""
    from ..night.report import local

    started, finished = d["started_at"], d.get("finished_at")
    out = ["", "=" * 96,
           f"КОНТРОЛЬНА ВИБІРКА №{d['id']} — {local(started):%Y-%m-%d %H:%M}"
           + (f"–{local(finished):%H:%M}" if finished else "")
           + f" (місцевий час): {STATUS_UA.get(d['status'], d['status'])}", "=" * 96]
    out.append(f"  по {d.get('per_source') or '—'} випадкових актуальних на джерело + "
               f"контрольні; запитів {d.get('requests') or 0}; замок чекали "
               f"{(d.get('lock_waited_s') or 0) / 60:.0f} хв; зерно {d.get('seed') or '—'}")
    if d.get("message"):
        out.append(f"  {d['message']}")
    for source, v in sorted((d.get("verdicts") or {}).items()):
        out.append(_source_line(source, v))
        for e in v.get("examples") or []:
            out.append(_example_line(e))
        if v.get("examples_more"):
            out.append(f"     … ще {v['examples_more']}")
    lanes = d.get("lanes") or {}
    if lanes:
        out.append("  смуги: " + "; ".join(
            f"{h} {ln.get('requests', 0)} запитів"
            + (" — ЗУПИНЕНО блокуваннями" if ln.get("stopped_early") else "")
            for h, ln in sorted(lanes.items())))
    out.append("  Стан оголошень вибірка не змінює; «знято» — підказка звичайній перевірці "
               "(через запобіжник).")
    out.append("=" * 96)
    return "\n".join(out)
