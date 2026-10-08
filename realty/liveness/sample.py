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
from datetime import datetime

from sqlalchemy import select

from .. import ops

log = logging.getLogger(__name__)

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
