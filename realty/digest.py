"""Щоденне зведення в Telegram — попередження одним повідомленням (хвиля W3, D58).

Рішення власника 08.10 (D55 п. 6): критичне — одразу (watchdog.run), попередження й
результати нічних робіт — одним зведенням раз на добу, і воно ВИГЛЯДАЄ інакше:
перший рядок «📋 Щоденне зведення · 09.10 (чт)», а не «🚨 КРИТИЧНО».

Коли. Сторож (`cli.py watchdog`, таймер кожні 30 хв) на першому запуску після
config/alerts.toml digest.at (місцевий час digest.timezone) шле зведення за цю місцеву
дату, якщо його ще не надіслано (`state["_digest"]["date"]`). Не вдалось — дата не
ставиться, наступний запуск спробує знову. Місцева дата, а не UTC: 08:40 за Києвом —
05:40 UTC, і північ UTC не дає другого зведення.

Що. Розділи — функції в реєстрі SECTIONS (інші хвилі додають свої через `register`):
розділ повертає рядки (або None — пропустити); розділ, що впав, стає одним рядком
«розділ X не зібрано: …» і зведення не вбиває. Вікно — від попереднього зведення
(щонайменше 24 год, щонайбільше digest.max_window_hours). Довжина — до
digest.max_chars (Telegram обрізає на 4096): решта — хвостом «… ще N рядків».

Ручне: `cli.py alert digest --dry-run` друкує зведення без надсилання й без запису
стану; `--send` надсилає зараз (розклад щоденного не змінює).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import ops

log = logging.getLogger(__name__)

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
DIGEST_HEAD = "📋 Щоденне зведення"


@dataclass
class Ctx:
    now: datetime            # наївний UTC, як в ops.db
    since: datetime          # початок вікна зведення (наївний UTC)
    state: dict              # стан сторожа (data/alerts.json)
    cfg: object              # AlertsConfig
    units_path: Path | None = None

    @property
    def tz(self):
        from zoneinfo import ZoneInfo

        return ZoneInfo(self.cfg.digest.timezone)

    def local(self, dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc).astimezone(self.tz).replace(tzinfo=None)

    def hm(self, dt: datetime | None) -> str:
        """«09.10 07:12» — місцевий час; сьогоднішнє — лише «07:12»."""
        lt = self.local(dt)
        if lt is None:
            return "—"
        today = self.local(self.now).date()
        return f"{lt:%H:%M}" if lt.date() == today else f"{lt:%d.%m %H:%M}"

    def ago(self, dt: datetime | None) -> str:
        from .watchdog import _ago

        return _ago(dt, self.now)


def _iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def _first_line(text: str, n: int = 170) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= n else line[:n - 1] + "…"


# --- Розділи --------------------------------------------------------------------------------


def section_warnings(ctx: Ctx) -> list[str] | None:
    """(1) Попередження за вікно: активні й зниклі; критичні, що досі тривають чи минули;
    служби, що падали (`alert unit-failed`)."""
    from .watchdog import CRITICAL, WARNING, action_for

    active_w, active_c = [], []
    for key, st in sorted(ctx.state.items()):
        if key.startswith("_") or not isinstance(st, dict):
            continue
        (active_w if st.get("level") == WARNING else active_c).append((key, st))
    resolved = [r for r in ctx.state.get("_resolved", [])
                if (_iso(r.get("resolved")) or datetime.min) >= ctx.since]
    gone_w = [r for r in resolved if r.get("level") == WARNING]
    gone_c = [r for r in resolved if r.get("level", CRITICAL) != WARNING and r.get("sent")]
    out: list[str] = []
    if active_w or gone_w:
        out.append(f"⚠️ Попередження: активних {len(active_w)}, зникло {len(gone_w)}")
        for key, st in active_w:
            out.append(f"  • {key} (з {ctx.hm(_iso(st.get('since')))}): "
                       f"{_first_line(st.get('text', ''))}")
            act = action_for(key, ctx.cfg)
            if act:
                out.append(f"    👉 {act}")
        for r in gone_w:
            out.append(f"  ✔ {r['key']} ({ctx.hm(_iso(r.get('since')))}–"
                       f"{ctx.hm(_iso(r.get('resolved')))})")
    if active_c:
        out.append("🚨 Критичні, що досі тривають: " + "; ".join(
            f"{k} (з {ctx.hm(_iso(st.get('since')))})" for k, st in active_c))
    if gone_c:
        out.append("✅ Критичні за добу, що минули: " + "; ".join(
            f"{r['key']} ({ctx.hm(_iso(r.get('since')))}–{ctx.hm(_iso(r.get('resolved')))})"
            for r in gone_c))
    units = _unit_failures(ctx)
    if units:
        out.append("⛔ Служби, що падали: " + "; ".join(units))
    return out or None


def _unit_failures(ctx: Ctx) -> list[str]:
    from . import watchdog

    path = ctx.units_path or watchdog.UNITS_PATH
    try:
        book = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for unit, rec in sorted(book.items()):
        last = _iso(rec.get("last"))
        if last is not None and last >= ctx.since:
            out.append(f"{unit} ×{rec.get('count', 1)} (востаннє {ctx.hm(last)})")
    return out


def section_all_clear(ctx: Ctx) -> list[str] | None:
    """(6) «Усе гаразд», коли попереджень немає (і критичних теж)."""
    for key, st in ctx.state.items():
        if not key.startswith("_") and isinstance(st, dict):
            return None
    if any((_iso(r.get("resolved")) or datetime.min) >= ctx.since
           and r.get("level") == "warning" for r in ctx.state.get("_resolved", [])):
        return None
    return ["✅ Усе гаразд: попереджень за добу немає."]


ONETIME = ("onetime_reseen", "legacy_404", "onetime_hinted", "onetime_blind")
STOP_SHORT = {"blocks": "зупинено блокуваннями", "block_share": "зупинено блокуваннями",
              "deadline": "дедлайн", "sigterm": "зупинено", "killed": "зупинено примусово"}


def section_night(ctx: Ctx) -> list[str] | None:
    """(2) Нічні вікна за вікно зведення: по хостах — перевірено ключів, знято, повернуто,
    полагоджено, без висновку, зупинка; M2/M3; запобіжник по джерелах; докази нічних
    робіт (night.report.evidence_summary — якщо вже є, хвиля E11)."""
    from sqlalchemy import select

    from .liveness import fuse
    from .night import report as night_report

    ops.init_ops()
    with ops.ops_session() as s:
        runs = [night_report.as_dict(r) for r in s.scalars(
            select(ops.NightRun).where(ops.NightRun.started_at >= ctx.since)
            .order_by(ops.NightRun.id))]
    out: list[str] = []
    if runs:
        heads = [f"{d.get('window') or '—'} — "
                 f"{night_report.STATUS_UA.get(d['status'], d['status'])}" for d in runs]
        out.append(f"🌙 Ніч {runs[-1].get('night_date') or '—'}: " + "; ".join(heads))
        hosts: dict[str, dict] = {}
        stops: dict[str, set] = {}
        for d in runs:
            for host, ph in (d.get("per_host") or {}).items():
                acc = hosts.setdefault(host, dict.fromkeys(
                    ("keys", "delisted", "restored", "repaired", "unknown", "not_found"), 0))
                for k in acc:
                    acc[k] += int((ph or {}).get(k) or 0)
            for host, ln in (d.get("lanes") or {}).items():
                if (ln or {}).get("stopped") in STOP_SHORT:
                    stops.setdefault(host, set()).add(STOP_SHORT[ln["stopped"]])
        for host in sorted(set(hosts) | set(stops)):
            a = hosts.get(host) or {}
            line = (f"  {host}: перевірено {a.get('keys', 0)}, знято {a.get('delisted', 0)}, "
                    f"повернуто {a.get('restored', 0)}, полагоджено {a.get('repaired', 0)}, "
                    f"без висновку {a.get('unknown', 0)}")
            if host in stops:
                line += f" — {', '.join(sorted(stops[host]))}"
            out.append(line)
        last = runs[-1]
        planned = int(((last.get("plan") or {}).get("onetime_keys")) or 0)
        verdicts = ((last.get("per_tier") or {}).get("verdicts")
                    if isinstance((last.get("per_tier") or {}).get("verdicts"), dict)
                    else last.get("per_tier")) or {}
        done = sum(int(n or 0) for t in ONETIME for n in (verdicts.get(t) or {}).values())
        if planned or done:
            out.append(f"  M2/M3 (останнє вікно): у плані {planned} ключів, перевірено {done}, "
                       f"лишилось ≈ {max(0, planned - done)}")
    held = [f for f in fuse.state() if f["state"] == "held"]
    if held:
        out.append("🧯 Запобіжник тримає: " + "; ".join(
            f"{f['source']} (з {ctx.hm(_iso(f.get('tripped_at')))}, {f.get('reason')})"
            for f in held))
    elif runs:
        out.append("🧯 Запобіжник: жодне джерело не тримається")
    evidence = getattr(night_report, "evidence_summary", None)
    if callable(evidence):
        from .db import SessionLocal

        with SessionLocal() as s:
            lines = evidence(s) or []
        out += [f"  {x}" if not str(x).startswith(" ") else str(x) for x in lines]
    return out or None


def section_cycles(ctx: Ctx) -> list[str] | None:
    """(3) Цикли за вікно: скільки, з яким статусом; нових оголошень по джерелах."""
    from sqlalchemy import func, select

    from .config import enabled_sources

    ops.init_ops()
    with ops.ops_session() as s:
        cycles = s.execute(select(ops.CycleRecord.status, func.count())
                           .where(ops.CycleRecord.started_at >= ctx.since)
                           .group_by(ops.CycleRecord.status)).all()
        last = s.scalar(select(func.max(ops.CycleRecord.finished_at))
                        .where(ops.CycleRecord.started_at >= ctx.since))
        new = dict(s.execute(select(ops.RunRecord.source, func.sum(ops.RunRecord.inserted))
                             .where(ops.RunRecord.started_at >= ctx.since,
                                    ops.RunRecord.mode == "fresh")
                             .group_by(ops.RunRecord.source)).all())
    by = {st: int(n) for st, n in cycles}
    total = sum(by.values())
    names = {"ok": "гаразд", "partial": "частково", "failed": "не вдалось", "running": "триває"}
    parts = ", ".join(f"{names.get(k, k)} {v}" for k, v in sorted(by.items()))
    out = [f"🔄 Цикли: {total}" + (f" ({parts})" if parts else "")
           + (f"; останній закінчився {ctx.hm(last)}" if last else "")]
    sources = list(dict.fromkeys([*enabled_sources(), *new]))
    if sources:
        out.append("  нових оголошень: " + " · ".join(f"{s} {int(new.get(s) or 0)}"
                                                      for s in sources))
    return out


def _storages(offsite: str | None) -> set[str]:
    return {part.split(":", 1)[0].strip() for part in (offsite or "").split(",") if part.strip()}


def _failed_storages(message: str | None) -> dict[str, str]:
    out = {}
    for part in (message or "").split(";"):
        name, sep, why = part.strip().partition(":")
        if sep and name.strip() in ("rclone", "telegram", "scp"):
            out[name.strip()] = why.strip()
    return out


STORAGE_UA = {"rclone": "Google Drive (rclone)", "telegram": "Telegram", "scp": "scp"}


def section_backups(ctx: Ctx) -> list[str] | None:
    """(4) Бекапи: останній — статус, розмір, відновлення; по сховищах — гаразд чи ні й
    коли востаннє вдалось."""
    from sqlalchemy import select

    from . import backup, notify

    ops.init_ops()
    with ops.ops_session() as s:
        rows = s.scalars(select(backup.BackupRecord)
                         .order_by(backup.BackupRecord.id.desc()).limit(30)).all()
    if not rows:
        return ["💾 Бекапів ще не було"]
    last = rows[0]
    status = {"ok": "гаразд", "failed": "НЕ ВДАВСЯ", "local": "лише локально",
              "running": "триває"}.get(last.status, last.status)
    out = [f"💾 Бекап {ctx.hm(last.created_at)} — {status} ({(last.size or 0) / 1e6:.1f} МБ), "
           f"відновлення {'збіглось' if last.restored_ok else 'НЕ перевірено'}"]
    expected = set()
    if backup.RCLONE_REMOTE:
        expected.add("rclone")
    if backup.TELEGRAM and notify.configured():
        expected.add("telegram")
    if backup.REMOTE:
        expected.add("scp")
    seen = expected | set().union(*(_storages(r.offsite) | set(_failed_storages(r.message))
                                    for r in rows))
    for name in sorted(seen):
        ok_at = next((r.created_at for r in rows if name in _storages(r.offsite)), None)
        if name in _storages(last.offsite):
            out.append(f"  {STORAGE_UA.get(name, name)} — ✅ {ctx.ago(ok_at)}")
        else:
            why = _failed_storages(last.message).get(name, "не вивантажено")
            out.append(f"  {STORAGE_UA.get(name, name)} — ❌ {why[:90]}; останній успіх: "
                       f"{ctx.ago(ok_at) if ok_at else 'не було (з останніх 30)'}")
    return out


def section_sample(ctx: Ctx) -> list[str] | None:
    """(5) Контрольна вибірка, якщо прогін був у вікні зведення."""
    from .liveness import sample

    last = sample.last_run()
    if last is None or (last.get("finished_at") or last["started_at"]) < ctx.since:
        return None
    return sample.render_short(last, local=ctx.hm)


def section_places(ctx: Ctx) -> list[str] | None:
    """Блок 4: останній прогін кроку «райони й ЖК» — нерозпізнані назви, would_change
    (одне читання ops.places_runs, як GET /api/status/places)."""
    from .places import commands

    run = commands.last_run()
    if run is None:
        return None
    unknown = run.get("unknown") or []
    top = ", ".join(f"«{e.get('name')}» {e.get('active', e.get('all'))}" for e in unknown[:3])
    line = (f"🗺 Райони й ЖК ({ctx.hm(_iso(run.get('at')))}, {run.get('status')}): змінилось би "
            f"{run.get('would_change') or 0}, нерозпізнаних назв {len(unknown)}"
            + (f" — {top}" if top else ""))
    return [line]


# Реєстр розділів: (назва, функція). Порядок — порядок у зведенні.
Section = Callable[[Ctx], "list[str] | None"]
SECTIONS: list[tuple[str, Section]] = [
    ("попередження", section_warnings),
    ("усе гаразд", section_all_clear),
    ("ніч", section_night),
    ("цикли", section_cycles),
    ("бекапи", section_backups),
    ("контрольна вибірка", section_sample),
    ("райони й ЖК", section_places),
]


def register(name: str, fn: Section, *, before: str | None = None) -> None:
    """Додати розділ (інші хвилі): у кінець або перед розділом `before`."""
    names = [n for n, _ in SECTIONS]
    if name in names:
        SECTIONS[names.index(name)] = (name, fn)
        return
    at = names.index(before) if before in names else len(SECTIONS)
    SECTIONS.insert(at, (name, fn))


# --- Збирання й надсилання ------------------------------------------------------------------


def window_start(now: datetime, state: dict, cfg) -> datetime:
    """Від попереднього зведення: щонайменше 24 год, щонайбільше max_window_hours."""
    last = _iso((state.get("_digest") or {}).get("sent_at"))
    start = now - timedelta(hours=24)
    if last is not None and last < start:
        start = max(last, now - timedelta(hours=cfg.digest.max_window_hours))
    return start


def build(now: datetime, state: dict, cfg, *, units_path: Path | None = None) -> str:
    from .watchdog import _header

    ctx = Ctx(now=now, since=window_start(now, state, cfg), state=state, cfg=cfg,
              units_path=units_path)
    local = ctx.local(now)
    lines = [f"{DIGEST_HEAD} · {local:%d.%m} ({WEEKDAYS[local.weekday()]})", _header()]
    for name, fn in SECTIONS:
        try:
            got = fn(ctx)
        except Exception as e:                                   # noqa: BLE001
            log.exception("розділ зведення %s упав", name)
            got = [f"• розділ «{name}» не зібрано: {type(e).__name__}: {str(e)[:120]}"]
        if got:
            lines += [str(x) for x in got]
    return fit(lines, cfg.digest.max_chars)


def fit(lines: list[str], limit: int) -> str:
    """Не довше за `limit`: зайві рядки — хвостом «… ще N рядків»."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for i, line in enumerate(lines):
        tail = (f"… ще {len(lines) - i} рядк(и/ів) — повністю: "
                f"cli.py alert digest --dry-run")
        if len("\n".join([*kept, line, tail])) > limit:
            return "\n".join([*kept, tail])
        kept.append(line)
    return "\n".join(kept)


def due(now: datetime, state: dict, cfg) -> str | None:
    """Місцева дата, за яку зведення пора надіслати, або None."""
    from zoneinfo import ZoneInfo

    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(cfg.digest.timezone))
    h, m = (int(x) for x in cfg.digest.at.split(":"))
    if (local.hour, local.minute) < (h, m):
        return None
    day = local.date().isoformat()
    if (state.get("_digest") or {}).get("date") == day:
        return None
    return day


def maybe_send(now: datetime, state: dict, cfg, send) -> dict | None:
    """Надіслати зведення, якщо настав час і за цю місцеву дату його ще не було.

    Успіх — `state["_digest"]` = {date, sent_at, chars}; невдача — дата НЕ ставиться
    (наступний запуск сторожа повторить), помилка й кількість спроб — у стані."""
    from . import notify

    day = due(now, state, cfg)
    if day is None:
        return None
    rec = state.setdefault("_digest", {})
    text = build(now, state, cfg)
    try:
        send(text)
    except Exception as e:                                       # noqa: BLE001
        rec["error"] = notify._mask(f"{type(e).__name__}: {e}")[:200]
        rec["tries"] = int(rec.get("tries") or 0) + 1
        rec["last_try"] = now.isoformat()
        log.error("зведення не надіслано: %s", rec["error"])
        return {"date": day, "sent": False, "error": rec["error"]}
    state["_digest"] = {"date": day, "sent_at": now.isoformat(), "chars": len(text)}
    return {"date": day, "sent": True, "chars": len(text)}
