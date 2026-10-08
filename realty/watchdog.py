"""Сигнал тиші: повідомлення в Telegram, коли системі погано.

Головне правило — тривога спрацьовує від ВІДСУТНОСТІ події, а не від помилки.
10.09.2026 помилки не було взагалі: процес був живий і чекав. Була тиша, і
9 днів її ніхто не помітив. Тому сторож не читає логи в пошуках винятків, а
питає: «коли востаннє був цикл, що СПРАВДІ щось зібрав?»

Що ще вважається аварією:
  * джерело різко збирає менше, ніж зазвичай (парсер тихо збирає порожнечу
    після зміни розмітки);
  * частка блокувань (401/403/429) різко зросла;
  * бекап не вдався або давно не було успішного.

Щоб не спамити: одне повідомлення на подію, повтор — не частіше ніж раз на
ALERT_REPEAT_HOURS, і одне «відновилось», коли подія зникла. Стан — у
data/alerts.json.

Чого сторож не бачить: машина вимкнена або без мережі — тоді мовчить і він.
Це закриває лише зовнішній «вимикач мерця» (див. implementation-notes.md).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from . import notify, ops
from .config import DATA_DIR, enabled_sources

log = logging.getLogger(__name__)

SILENCE_HOURS = float(os.getenv("ALERT_SILENCE_HOURS", "7"))   # 2 пропущені цикли + запас
REPEAT_HOURS = float(os.getenv("ALERT_REPEAT_HOURS", "6"))
DROP_RATIO = 0.2            # зібрано менше 20% від звичайного…
DROP_MIN_BASELINE = 10      # …при звичайних хоча б 10 оголошеннях
DROP_CONSECUTIVE = 2        # …два прогони поспіль (один — ще випадковість)
BLOCK_SHARE = 0.20          # частка блокувань, яка сама по собі тривожна
BLOCK_RISE = 0.15           # або ріст на 15 п.п. від звичайної
BLOCK_MIN_REQUESTS = 10
# Зібране, але не записане: тривога вже з кількох записів — це завжди помилка
# системи, а не джерела. 20.09 так губились усі нові оголошення й зміни цін.
WRITE_FAIL_MIN = 3
WRITE_FAIL_SHARE = 0.02
BACKUP_MAX_AGE_HOURS = 30
# Самоперевірка зведення: «різко більше» — понад звичайне (медіана останніх
# перевірок) на 30% і ще щонайменше на 20 квартир; одиничні коливання — ні.
DEDUP_RISE = 1.3
DEDUP_RISE_MIN = 20
DEDUP_HISTORY = 8
# Нічне вікно триває ≤ 1 год 50 хв (TimeoutStartSec realty-night.service); запис, що
# «триває» довше, — диригента вбито ззовні (E9, D53).
NIGHT_STUCK_HOURS = 2.5
# Ніч не відбулась зовсім (рецензія E9, D53): за стільки годин жодного запису ночі,
# хоча цикли йдуть (таймер realty-night не ввімкнено — напр., install.sh обірвався між
# вимиканням старого таймера й увімкненням нового, — чи диригент падає до свого
# запису). 26 год = доба + запас на зсув вікон. Тривога — лише коли нічний диригент
# розгорнуто: його запис уже був або юніт таймера встановлено.
NIGHT_MISSING_HOURS = 26
NIGHT_TIMER_UNIT = Path.home() / ".config" / "systemd" / "user" / "realty-night.timer"
COLLECTOR_OFF = DATA_DIR / "COLLECTOR_OFF"            # те саме, що runner.DISABLED_FLAG
STATE_PATH = DATA_DIR / "alerts.json"
PUBLIC_URL_PATH = DATA_DIR / "public_url"


@dataclass
class Alert:
    key: str
    text: str
    # Не повторювати раз на REPEAT_HOURS, а надіслати один раз на стан: новий стан (інше
    # значення `once`) — нове повідомлення; той самий — мовчки (рецензія E10: «змінилось
    # би» без дії власника не зникає, а повтор кожні 6 год — шум).
    once: str | None = None


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _ago(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "ніколи"
    h = _hours(now - ts)
    if h < 1:
        return f"{h * 60:.0f} хв тому"
    if h < 48:
        return f"{h:.0f} год тому"
    return f"{h / 24:.1f} доби тому"


# --- Перевірки -----------------------------------------------------------------------


def check_silence(now: datetime, state: dict) -> Alert | None:
    last = ops.last_success_at()
    if last is None:
        # Свіжа установка: відлік від першого запуску сторожа, щоб не кричати
        # в першу ж хвилину, але й не мовчати вічно, якщо цикл так і не вдався.
        first = datetime.fromisoformat(state.setdefault("_first_seen", now.isoformat()))
        if _hours(now - first) < SILENCE_HOURS:
            return None
        since = first
    else:
        if _hours(now - last) < SILENCE_HOURS:
            return None
        since = last
    cycles = ops.last_cycles(4)
    lines = [f"🔇 Тиша: {_hours(now - since):.0f} год без жодного успішного збору "
             f"(поріг {SILENCE_HOURS:.0f} год).",
             f"Останній успішний цикл: {_ago(last, now)}."]
    if cycles:
        lines.append("Останні цикли:")
        for c in cycles:
            took = f", {(c.finished_at - c.started_at).total_seconds() / 60:.0f} хв" \
                if c.finished_at else ", ще триває"
            lines.append(f"  • {_ago(c.started_at, now)}: {c.status}{took}, зібрано {c.kept}"
                         + (f" — {c.message[:120]}" if c.message else ""))
    else:
        lines.append("Жодного циклу не записано — планувальник, схоже, не запускається.")
    lines.append("Перевірити: systemctl --user status realty-cycle.timer realty-cycle.service")
    return Alert("silence", "\n".join(lines))


def _fresh_runs(session, source: str, limit: int = 22) -> list[ops.RunRecord]:
    return list(session.scalars(
        select(ops.RunRecord)
        .where(ops.RunRecord.source == source, ops.RunRecord.mode == "fresh",
               ops.RunRecord.status != "running")
        .order_by(ops.RunRecord.started_at.desc()).limit(limit)))


def _written(run) -> int:
    """Скільки записів цього прогону дійшло до бази."""
    return (run.inserted or 0) + (run.updated or 0)


def check_sources(now: datetime) -> list[Alert]:
    """Джерело живе, але нічого не приносить.

    Міряємо ЗАПИСАНЕ, а не зібране. DIM.RIA і OLX чесно зупиняються після
    першої сторінки, коли нових оголошень немає: зібрано 20 замість 160 — і це
    норма, а не поломка (перша версія правила саме на цьому й помилилась).
    Поломка — коли не записано НІЧОГО: або картки перестали розбиратись, або
    карантин не прийняв пакет.
    """
    alerts = []
    ops.init_ops()
    with ops.ops_session() as s:
        for name in enabled_sources():
            runs = _fresh_runs(s, name)
            recent, older = runs[:DROP_CONSECUTIVE], runs[DROP_CONSECUTIVE:]
            if len(recent) < DROP_CONSECUTIVE:
                continue
            base = [_written(r) for r in older if r.status == "ok" and _written(r)]
            if len(base) >= 3 and statistics.median(base) >= DROP_MIN_BASELINE \
                    and all(_written(r) == 0 for r in recent):
                why = next((r.message for r in recent if r.message), None)
                alerts.append(Alert(f"drop:{name}", (
                    f"📉 {name}: у {DROP_CONSECUTIVE} останніх прогонах до бази не дійшло "
                    f"жодного оголошення (звичайно ~{statistics.median(base):.0f}). "
                    f"Схоже, сайт змінив розмітку і парсер збирає порожнечу, або пакет "
                    f"не пройшов карантин."
                    + (f"\nОстання помилка: {why[:200]}" if why else ""))))
            last = recent[0]
            if (last.skipped or 0) >= WRITE_FAIL_MIN and \
                    (last.skipped or 0) >= WRITE_FAIL_SHARE * max(last.kept or 0, 1):
                alerts.append(Alert(f"write:{name}", (
                    f"🧱 {name}: зібрано {last.kept}, але {last.skipped} записів не вдалося "
                    f"ЗАПИСАТИ в базу. Це не сайт і не карантин — помилка бази.\n"
                    f"{(last.message or '')[:240]}")))
            # Блокування — по останньому прогону з помітною кількістю запитів.
            def share(r):
                total = (r.requests_ok or 0) + (r.requests_failed or 0)
                return (r.requests_blocked or 0) / total if total >= BLOCK_MIN_REQUESTS else None
            last_share = share(recent[0])
            if last_share is None:
                continue
            usual_shares = [x for x in (share(r) for r in older) if x is not None]
            usual_share = statistics.median(usual_shares) if len(usual_shares) >= 3 else 0.0
            if last_share >= BLOCK_SHARE or last_share - usual_share >= BLOCK_RISE:
                alerts.append(Alert(f"blocks:{name}", (
                    f"🚧 {name}: сайт відмовляє частіше — {last_share:.0%} запитів "
                    f"заблоковано (401/403/429), звичайно {usual_share:.0%}. "
                    f"Правило «знято тільки за 404» не порушується, але збір і перевірка "
                    f"актуальності по цьому сайту сповільняться.")))
    return alerts


def check_verify_blocks(now: datetime) -> list[Alert]:
    """Блокування на перевірці актуальності — найбільшому споживачу запитів.

    Останні 24 год проти попередніх 7 діб, по кожному джерелу окремо.
    """
    from sqlalchemy import case, func

    from .db import SessionLocal
    from .models import CheckEvent, Listing

    day, week = now - timedelta(hours=24), now - timedelta(days=8)
    blocked = case((CheckEvent.code.in_((401, 403, 429)), 1), else_=0)
    alerts = []
    with SessionLocal() as s:
        rows = s.execute(
            select(Listing.source, CheckEvent.checked_at >= day,
                   func.count(), func.sum(blocked))
            .join(Listing, Listing.id == CheckEvent.listing_id)
            .where(CheckEvent.checked_at >= week)
            .group_by(Listing.source, CheckEvent.checked_at >= day)).all()
    stats: dict[str, dict] = {}
    for source, is_recent, total, bad in rows:
        stats.setdefault(source, {})[bool(is_recent)] = (int(total), int(bad or 0))
    for source, st in stats.items():
        recent = st.get(True)
        if not recent or recent[0] < BLOCK_MIN_REQUESTS:
            continue
        share = recent[1] / recent[0]
        prev = st.get(False)
        usual = prev[1] / prev[0] if prev and prev[0] >= BLOCK_MIN_REQUESTS else 0.0
        if share >= BLOCK_SHARE or share - usual >= BLOCK_RISE:
            alerts.append(Alert(f"verify-blocks:{source}", (
                f"🚧 {source}: перевірка актуальності — {share:.0%} запитів заблоковано "
                f"за добу ({recent[1]} із {recent[0]}), тиждень до того {usual:.0%}.")))
    return alerts


def check_backup(now: datetime) -> Alert | None:
    from . import backup

    last_try = backup.last_attempt()
    last_ok = backup.last_success_at()
    if last_try is not None and last_try.status == "failed":
        return Alert("backup", (f"💾 Бекап не вдався ({_ago(last_try.created_at, now)}): "
                                f"{(last_try.message or 'без пояснення')[:300]}\n"
                                f"Останній успішний: {_ago(last_ok, now)}."))
    if last_try is not None and last_try.status == "ok" and last_try.message:
        # Копія поза машиною є, але не всюди, куди мала піти: напр., Drive
        # упав, а Telegram спрацював. Без цієї перевірки така поломка мовчала б.
        return Alert("backup-partial", (
            f"💾 Бекап {_ago(last_try.created_at, now)} ліг не в усі сховища: "
            f"{last_try.message[:300]}\nКопія поза машиною є ({last_try.offsite})."))
    if last_ok is not None and _hours(now - last_ok) > BACKUP_MAX_AGE_HOURS:
        return Alert("backup", f"💾 Останній успішний бекап {_ago(last_ok, now)} — "
                               f"щоденний бекап не відпрацював.")
    return None


def check_dedup(now: datetime) -> list[Alert]:
    """Підозрілих квартир чи пропущених дублів різко більше, ніж звичайно."""
    from statistics import median

    from . import dedup_audit

    rows = dedup_audit.recent(DEDUP_HISTORY + 1)
    if len(rows) < 4:
        return []
    last, before = rows[0], rows[1:]
    alerts = []

    def core(r) -> int:
        # Підозрілих лише за «старими» видами (Блок 4, E10, D57): нові види complex і
        # complex_phase інакше дали б хибну тривогу вже на першій перевірці з ними.
        # Перевірки до E10 (suspicious_core NULL) — повне suspicious, як і досі.
        return r.suspicious if r.suspicious_core is None else r.suspicious_core

    for field, key, what in (("suspicious", "dedup-suspicious", "підозрілих квартир"),
                             ("missed", "dedup-missed", "пропущених дублів")):
        value = core if field == "suspicious" else (lambda r, f=field: getattr(r, f))
        usual = median(value(r) for r in before)
        now_n = value(last)
        if now_n > usual * DEDUP_RISE + DEDUP_RISE_MIN:
            kinds = json.loads(last.by_kind or "{}") if field == "suspicious" else {}
            top = ", ".join(f"{dedup_audit.KINDS.get(k, k)} — {n}"
                            for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:3])
            alerts.append(Alert(key, (
                f"🧩 Зведення квартир: {what} {now_n}, звичайно ~{usual:.0f} "
                f"(перевірка {_ago(last.at, now)}).{' Найчастіше: ' + top + '.' if top else ''}\n"
                f"Черга на перегляд — на /status.")))
    # Окремо — вид «різні ЖК»: порівнюється з медіаною ЦЬОГО Ж виду, коли перевірок із ним
    # набралось config/places/rules.toml audit.complex_min_audits.
    alerts += _complex_spike(last, before, now)
    return alerts


def _complex_spike(last, before, now: datetime) -> list[Alert]:
    from statistics import median

    from . import configfiles

    try:
        need = configfiles.load("places/rules").audit.complex_min_audits
    except configfiles.ConfigError:
        return []
    with_kind = [r for r in before if r.suspicious_core is not None]
    if len(with_kind) < need:
        return []
    n_of = lambda r: json.loads(r.by_kind or "{}").get("complex", 0)  # noqa: E731
    usual = median(n_of(r) for r in with_kind)
    now_n = n_of(last)
    if now_n <= usual * DEDUP_RISE + DEDUP_RISE_MIN:
        return []
    return [Alert("dedup-complex", (
        f"🧩 Зведення квартир: квартир з різними ЖК {now_n}, звичайно ~{usual:.0f} "
        f"(перевірка {_ago(last.at, now)}). Черга на перегляд — на /status."))]


def check_liveness(now: datetime) -> list[Alert]:
    """Перевірка актуальності (Блок 1, E8, D52): запобіжник і здоров'я перевірки.

    Тривога шлеться тим самим шляхом, що й решта (повтор раз на REPEAT_HOURS,
    «відновилось» — коли зникне):
      * liveness-fuse:<джерело> — запобіжник тримає джерело: нічого не знімаємо, доки
        власник не зніме його на /status;
      * liveness-coverage:<хост> — понад alerts.coverage_overdue_share актуальних
        ключів не перевірені новим підписом довше за 2 × recheck_days (не раніше,
        ніж минуло 2 × recheck_days від першого прогону нового коду);
      * liveness-ria-unrecognized — сторінки DOM.RIA не розпізнано (змінилась розмітка);
      * liveness-repeat404 — правило повторного 404 помиляється (частка повернень);
      * liveness-snapshot-stale:<джерело> — повний перелік давно не оновлювався або
        повного (complete) немає зовсім від розгортання.
    Числа — з останнього прогону циклу (ops.liveness_runs), без важких запитів.
    """
    from . import configfiles, snapshot
    from .liveness import fuse

    alerts: list[Alert] = []
    for f in fuse.state():
        if f["state"] != "held":
            continue
        share = f"{100 * f['share']:.0f}%" if f.get("share") is not None else "—"
        ex = "\n".join(f"  • {u}" for u in (f.get("examples") or [])[:5])
        alerts.append(Alert(f"liveness-fuse:{f['source']}", (
            f"🧯 Запобіжник перевірки актуальності: {f['source']} — підпис «знято» "
            f"спрацював на {f['removed']} із {f['checked']} перевірених ({share}; "
            f"{f.get('reason')}, тлумачення «{f.get('mode')}»). Для джерела нічого не "
            f"знімаємо й не повертаємо, доки ви не знімете запобіжник на /status "
            f"(розділ «Зняті оголошення»)." + (f"\nПриклади:\n{ex}" if ex else ""))))
    cfg = configfiles.load("liveness")
    ops.init_ops()
    with ops.ops_session() as s:
        last = s.scalars(select(ops.LivenessRun)
                         .where(ops.LivenessRun.kind == "cycle", ops.LivenessRun.status == "ok",
                                ops.LivenessRun.report.isnot(None))
                         .order_by(ops.LivenessRun.id.desc()).limit(1)).first()
        first = s.scalar(select(ops.LivenessRun.started_at)
                         .order_by(ops.LivenessRun.id).limit(1))
        report = json.loads(last.report) if last is not None else None
        per_host = json.loads(last.per_host or "{}") if last is not None else {}
    if report:
        for host, d in (report.get("coverage_by_host") or {}).items():
            spec = cfg.hosts.get(host)
            if spec is None or not d.get("keys") or d.get("overdue_share") is None:
                continue
            settled = first is not None and now - first >= timedelta(days=2 * spec.recheck_days)
            if settled and d["overdue_share"] > cfg.alerts.coverage_overdue_share:
                alerts.append(Alert(f"liveness-coverage:{host}", (
                    f"🔎 {host}: {100 * d['overdue_share']:.0f}% актуальних ключів не "
                    f"перевірені довше за {2 * spec.recheck_days:g} дн. ({d['overdue']} із "
                    f"{d['keys']}). Черга перевірки не встигає або сайт блокує.")))
        rr = report.get("repeat404_returns") or {}
        if (rr.get("removed") or 0) >= cfg.alerts.repeat404_min_removed and \
                (rr.get("share") or 0) > cfg.alerts.repeat404_return_share:
            alerts.append(Alert("liveness-repeat404", (
                f"↩️ Правило «повторний 404» помиляється: повернулись {rr['returned']} із "
                f"{rr['removed']} знятих за ним ({100 * rr['share']:.0f}%, поріг "
                f"{100 * cfg.alerts.repeat404_return_share:.0f}%). Показати дані власнику.")))
    for host, d in per_host.items():
        spec = cfg.hosts.get(host)
        if spec is None or spec.signature != "ria_page":
            continue
        sigs = d.get("signatures") or {}
        total = sum(sigs.values())
        bad = sigs.get("unrecognized", 0) + sigs.get("conflict", 0)
        if total >= cfg.alerts.unrecognized_min_checked and \
                bad / total > cfg.alerts.unrecognized_share:
            alerts.append(Alert("liveness-ria-unrecognized", (
                f"🧩 {host}: {bad} із {total} сторінок останнього прогону не розпізнано "
                f"(стан чи банер). Схоже, змінилась розмітка — перевірка тихо сліпне.")))
    for source, hours in cfg.alerts.snapshot_stale_hours.items():
        snap = snapshot.load(source)
        if snap is not None and snap.complete:
            if now - snap.taken_at > timedelta(hours=hours):
                alerts.append(Alert(f"liveness-snapshot-stale:{source}", (
                    f"🗂 {source}: повний перелік {_ago(snap.taken_at, now)} (поріг {hours:g} "
                    f"год) — зниклі з пошуку не потрапляють у перевірку.")))
            continue
        # Повного переліку немає зовсім (файл до E8 без `complete`, нові переліки
        # обриваються): вік — від розгортання нового коду (перший прогін перевірки)
        # або від старого файла, що пізніше (рецензія E8, D52). До розгортання — мовчимо.
        if first is None:
            continue
        since = max(first, snap.taken_at) if snap is not None else first
        if now - since > timedelta(hours=hours):
            alerts.append(Alert(f"liveness-snapshot-stale:{source}", (
                f"🗂 {source}: немає повного переліку з {since:%d.%m %H:%M} UTC "
                f"({_ago(since, now)}; поріг {hours:g} год) — перелік обривається чи не "
                f"збирається: зниклі з пошуку не потрапляють у перевірку, перевірка існування "
                f"не відповідає. Причина — у журналі кроку «різниця списків».")))
    return alerts


def check_night(now: datetime) -> list[Alert]:
    """Нічний диригент (`cli.py night`, E9, D53): тривоги за останню добу.

      * night-backup — бекап на старті ночі не вдався: цієї ночі нічого не писали
        (сам бекап ще й показує check_backup);
      * night-blocked:<хост> — смугу зупинили блокування (401/403/429 поспіль чи
        частка): до кінця ночі хост стоїть;
      * night-hold:<хост> — блокування дві ночі поспіль: хост чекає рішення
        (`cli.py night unhold --host …`), поки не знято — щоночі без смуги;
      * night-late — замок звільнено пізніше за release_lock (ризик пропущеного циклу);
      * night-failed — диригент упав;
      * night-missing — за NIGHT_MISSING_HOURS жодного вікна, хоча цикли йдуть;
      * night-skipped — два останні вікна поспіль пропущено (цикл не звільнив замок).
    Запобіжник ночі — та сама тривога liveness-fuse:<джерело> (check_liveness).
    Часи — місцеві (як вікна в config/night.toml), у дужках — UTC.
    """
    from .night.report import hm
    ops.init_ops()
    alerts: list[Alert] = []
    since = now - timedelta(hours=24)
    with ops.ops_session() as s:
        runs = s.scalars(select(ops.NightRun).where(ops.NightRun.started_at >= since)
                         .order_by(ops.NightRun.id)).all()
        holds = s.scalars(select(ops.NightHold).where(ops.NightHold.state == "held")).all()
        rows = [(r.id, r.status, r.window, r.night_date, r.message, r.lanes,
                 r.lock_released_at, r.release_lock_at, r.started_at) for r in runs]
        hold_rows = [(h.host, h.since, h.reason) for h in holds]
        missing_since = now - timedelta(hours=NIGHT_MISSING_HOURS)
        cycles = s.scalar(select(func.count()).select_from(ops.CycleRecord)
                          .where(ops.CycleRecord.started_at >= missing_since))
        recent = s.scalar(select(func.count()).select_from(ops.NightRun)
                          .where(ops.NightRun.started_at >= missing_since))
        ever = s.scalar(select(func.count()).select_from(ops.NightRun))
        last_two = s.scalars(select(ops.NightRun.status).where(ops.NightRun.window.isnot(None))
                             .order_by(ops.NightRun.id.desc()).limit(2)).all()
    if (cycles and not recent and (ever or NIGHT_TIMER_UNIT.exists())
            and not COLLECTOR_OFF.exists()):
        alerts.append(Alert("night-missing", (
            f"🌙❓ за {NIGHT_MISSING_HOURS} год не було жодного нічного вікна, хоча цикли "
            f"йдуть: таймер realty-night вимкнено чи диригент падає до свого запису — "
            f"перевірка актуальності й дозбір identity стоять. Перевірити: systemctl --user "
            f"list-timers 'realty-*'; journalctl --user -u realty-night -n 80")))
    if len(last_two) == 2 and all(st == "lock_timeout" for st in last_two):
        alerts.append(Alert("night-skipped", (
            "🌙⏳ два нічні вікна поспіль пропущено: цикл не звільнив замок до "
            "stop_requests − lock.min_work_minutes. Чому цикл такий довгий — journalctl "
            "--user -u realty-cycle -n 80.")))
    for rid, status, window, night_date, message, lanes, released, release_by, started in rows:
        when = f"ніч {night_date or '—'}, вікно {window or '—'}"
        if status == "backup_failed":
            alerts.append(Alert("night-backup", (
                f"🌙💾 {when}: бекап на старті вікна не вдався — у цьому вікні нічого не "
                f"писали (перевірки актуальності, M2/M3, дозбір не запускались; наступне "
                f"вікно спробує бекап знову). "
                f"{(message or '')[:300]}")))
        if status == "failed":
            alerts.append(Alert("night-failed", (
                f"🌙⚠️ {when}: нічний диригент упав: {(message or '')[:300]}. "
                f"Журнал: journalctl --user -u realty-night")))
        if status == "running" and now - started > timedelta(hours=NIGHT_STUCK_HOURS):
            # Процес убито ззовні (TimeoutStartSec, OOM) — свій запис він уже не закриє.
            alerts.append(Alert("night-failed", (
                f"🌙⚠️ {when}: нічний запис досі «триває» через {_ago(started, now)} — "
                f"диригент, схоже, убито (systemd TimeoutStartSec чи брак пам'яті). "
                f"Журнал: journalctl --user -u realty-night")))
        if released is not None and release_by is not None and released > release_by:
            alerts.append(Alert("night-late", (
                f"🌙⏱ {when}: замок циклу звільнено о {hm(released)}, пізніше за межу "
                f"{hm(release_by)} — наступний цикл міг пропуститись.")))
        try:
            lane_info = json.loads(lanes or "{}")
        except ValueError:
            lane_info = {}
        for host, d in lane_info.items():
            if d.get("stopped") in ("blocks", "block_share"):
                # Запити смуги — перевірки, дозбір identity і рендери доказів (E11, D60).
                blocked = sum(int(d.get(k) or 0) for k in ("blocked", "identity_blocked",
                                                           "evidence_blocked"))
                total = sum(int(d.get(k) or 0) for k in ("requests", "identity_requests",
                                                         "evidence_requests"))
                alerts.append(Alert(f"night-blocked:{host}", (
                    f"🌙🚧 {when}: смугу {host} зупинили блокування ({blocked} із "
                    f"{total} запитів — 401/403/429/капча) — до кінця ночі цей сайт "
                    f"не перевіряємо. Повториться наступної ночі — хост чекатиме рішення.")))
    for host, held_since, reason in hold_rows:
        alerts.append(Alert(f"night-hold:{host}", (
            f"🌙✋ {host}: нічна смуга чекає рішення з {held_since:%d.%m %H:%M} UTC — "
            f"{reason or 'блокування'}. Дозволити знову: cli.py night unhold --host {host}")))
    return alerts


def check_places(now: datetime) -> list[Alert]:
    """Крок «райони й ЖК» (Блок 4, E10, D57): останній прогін упав, або довідник чи нові
    докази змінили б уже визначені ключі (would_change) — їх крок НЕ змінює, рішення за
    власником (інтеграція, конфлікт 13; виправлення — `cli.py places reassign`).

    «Упав» — як звичайна тривога (повтор раз на REPEAT_HOURS). would_change — ОДНЕ
    повідомлення на стан (версія довідника, хеш правил, число): саме собою воно не
    зникає, тож повтор кожні 6 год був би лише шумом (рецензія E10); число — на /status.
    Доказ, що зник (would_change_lost: агент змінив поле, мітку села відкинуто), —
    без тривоги. Читається лише останній рядок ops.places_runs (не пробний)."""
    from sqlalchemy import select

    ops.init_ops()
    with ops.ops_session() as s:
        last = s.scalars(select(ops.PlacesRun).where(ops.PlacesRun.status != "dry_run")
                         .order_by(ops.PlacesRun.id.desc()).limit(1)).first()
    if last is None:
        return []
    alerts = []
    if last.status == "failed":
        alerts.append(Alert("places-failed", (
            f"🗺 Крок «райони й ЖК» не вдався ({_ago(last.at, now)}): "
            f"{(last.message or 'див. журнал циклу')[:200]}. Район і ЖК нових оголошень "
            f"не визначаються, фільтри показують попередній стан.")))
    if last.would_change:
        detail = json.loads(last.would_change_detail or "{}").get("by_field") or {}
        what = ", ".join(f"{k} {v}" for k, v in detail.items()) or str(last.would_change)
        alerts.append(Alert("places-would-change", (
            f"🗺 Райони й ЖК: {last.would_change} вже визначених значень змінилось би ({what}) "
            f"— НЕ змінено, чекає рішення. Список — на /status, «Райони й ЖК»; виправити — "
            f"`cli.py places reassign` (пробний), далі `--apply` зі свіжим бекапом."),
            once=f"{last.directory_ver}:{last.rules_hash}:{last.would_change}"))
    return alerts


# --- Стан і розсилка -----------------------------------------------------------------


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(path)


def public_url() -> str | None:
    try:
        return PUBLIC_URL_PATH.read_text().strip() or None
    except OSError:
        return None


def _header() -> str:
    url = public_url()
    return f"[{socket.gethostname()}]" + (f" {url}" if url else "")


def collect(now: datetime, state: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for check in (lambda: check_silence(now, state), lambda: check_backup(now)):
        try:
            if a := check():
                alerts.append(a)
        except Exception as e:           # сторож не має падати через одну перевірку
            log.exception("перевірка впала")
            alerts.append(Alert("watchdog", f"⚠️ Сторож не зміг виконати перевірку: {e}"))
    for check in (check_sources, check_verify_blocks, check_dedup, check_liveness, check_night,
                  check_places):
        try:
            alerts += check(now)
        except Exception as e:
            log.exception("перевірка %s впала", check.__name__)
            alerts.append(Alert(f"watchdog:{check.__name__}",
                                f"⚠️ Сторож не зміг виконати {check.__name__}: {e}"))
    return alerts


def run(now: datetime | None = None, send=None, state_path: Path = STATE_PATH) -> dict:
    """Одна перевірка. Повертає, що надіслано, що притримано, що відновилось."""
    now = now or ops._now()
    send = send or notify.send_message
    state = load_state(state_path)
    alerts = collect(now, state)
    report = {"active": [a.key for a in alerts], "sent": [], "held": [], "resolved": [],
              "errors": []}

    for a in alerts:
        st = state.setdefault(a.key, {"since": now.isoformat(), "last_sent": None, "sent": 0})
        last = st.get("last_sent")
        due = last is None or _hours(now - datetime.fromisoformat(last)) >= REPEAT_HOURS
        if a.once is not None:
            due = st.get("once") != a.once
        st["text"] = a.text
        if not due:
            report["held"].append(a.key)
            continue
        prefix = "" if st["sent"] == 0 else f"(досі триває, з {st['since'][:16].replace('T', ' ')} UTC) "
        try:
            send(f"{_header()}\n{prefix}{a.text}")
            st["last_sent"] = now.isoformat()
            st["sent"] += 1
            if a.once is not None:
                st["once"] = a.once
            report["sent"].append(a.key)
        except Exception as e:
            # Не відмічаємо як надіслане — наступний запуск спробує ще раз.
            report["errors"].append(f"{a.key}: {e}")
            log.error("не вдалось надіслати %s: %s", a.key, e)

    active = {a.key for a in alerts}
    for key in [k for k in state if not k.startswith("_") and k not in active]:
        entry = state[key]
        if entry.get("sent"):
            try:
                send(f"{_header()}\n✅ Відновилось: {key} "
                     f"(тривога з {entry['since'][:16].replace('T', ' ')} UTC).")
            except Exception as e:
                report["errors"].append(f"{key} (відновлення): {e}")
                continue
        del state[key]
        report["resolved"].append(key)

    save_state(state, state_path)
    return report


def test_message(now: datetime | None = None) -> int:
    """Справжнє повідомлення тим самим шляхом, що й тривога, — перевірка каналу."""
    now = now or ops._now()
    last = ops.last_success_at()
    text = (f"{_header()}\n🧪 ТЕСТ сигналу тиші. Це перевірка каналу, а не аварія.\n"
            f"Останній успішний цикл: {_ago(last, now)}; тривога прийде, якщо тиша "
            f"перевищить {SILENCE_HOURS:.0f} год.")
    return notify.send_message(text)
