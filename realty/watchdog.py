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
import re
import socket
import statistics
import sys
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


def check_backup(now: datetime) -> list[Alert]:
    """Бекап на два рівні (D55 п. 6, D58).

      * backup-none (критичне) — копії поза машиною немає в ЖОДНОМУ сховищі: остання
        спроба «failed» (backup.py так і рахує: успіх — лише з копією поза машиною;
        локальна перевірена копія на тому ж диску від смерті диска не захищає), або
        успішного бекапу немає понад BACKUP_MAX_AGE_HOURS. Колишній ключ «backup».
      * backup-partial (попередження) — копія поза машиною є, але не всюди (Drive упав,
        Telegram спрацював).
      * db-integrity:backup (критичне) — копія бази не пройшла integrity_check: бекап
        копіює сторінки як є, тож пошкоджена саме база (запасна перевірка цілісності
        до щоденного quick_check, D58).
    """
    from . import backup

    last_try = backup.last_attempt()
    last_ok = backup.last_success_at()
    alerts: list[Alert] = []
    if last_try is not None and last_try.status == "failed":
        local = ("локальна копія є й відновлюється, але поза машиною — ніде"
                 if last_try.restored_ok else "навіть локальної перевіреної копії немає")
        alerts.append(Alert("backup-none", (
            f"💾 Бекап не вдався ({_ago(last_try.created_at, now)}): "
            f"{(last_try.message or 'без пояснення')[:300]}\n{local}.\n"
            f"Останній успішний: {_ago(last_ok, now)}.")))
        if "integrity_check" in (last_try.message or ""):
            alerts.append(Alert("db-integrity:backup", (
                f"🧨 Копія бази для бекапу не пройшла integrity_check "
                f"({_ago(last_try.created_at, now)}): база, найімовірніше, пошкоджена. "
                f"{(last_try.message or '')[:200]}")))
    elif last_try is not None and last_try.status == "ok" and last_try.message:
        # Копія поза машиною є, але не всюди, куди мала піти: напр., Drive
        # упав, а Telegram спрацював. Без цієї перевірки така поломка мовчала б.
        alerts.append(Alert("backup-partial", (
            f"💾 Бекап {_ago(last_try.created_at, now)} ліг не в усі сховища: "
            f"{last_try.message[:300]}\nКопія поза машиною є ({last_try.offsite}).")))
    if not any(a.key == "backup-none" for a in alerts) and last_ok is not None \
            and _hours(now - last_ok) > BACKUP_MAX_AGE_HOURS:
        alerts.append(Alert("backup-none", f"💾 Останній успішний бекап {_ago(last_ok, now)} — "
                                           f"щоденний бекап не відпрацював."))
    return alerts


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
                alerts.append(Alert(f"night-blocked:{host}", (
                    f"🌙🚧 {when}: смугу {host} зупинили блокування ({d.get('blocked')} із "
                    f"{d.get('requests')} запитів — 401/403/429) — до кінця ночі цей сайт "
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


# --- Хвиля W3 (D58): нові перевірки ------------------------------------------------------
# Пороги й часи — config/alerts.toml (окрім LOW_* нижче: вони — ті самі DROP_*, що й у
# check_sources). Мережа й читання всієї бази — через атрибути модуля SITE_PROBE і
# INTEGRITY_CHECK: тести підставляють свої (conftest), сторож у тестах не ходить на
# 127.0.0.1:8000 і не читає всю копію бази на кожному прогоні.

LOW_DAYS = 7                 # «звичайно» — медіана нових за добу за стільки попередніх діб


def check_low_sources(now: datetime) -> list[Alert]:
    """Джерело приносить різко менше НОВИХ оголошень, але не нуль (попередження, D58).

    Не за прогоном, а за добу: DIM.RIA й OLX чесно зупиняються після першої сторінки,
    коли новинок немає (test_early_stop_is_not_a_drop), — прогін із 20 записаними замість
    160 — норма. Доба згладжує це: нових за останні 24 год < DROP_RATIO × медіани нових за
    добу попередніх LOW_DAYS діб (медіана ≥ DROP_MIN_BASELINE). Нуль записаних — уже
    критичне drop:<джерело> (check_sources), тут — лише «записує, але мало нового».
    """
    ops.init_ops()
    since = now - timedelta(days=LOW_DAYS + 1)
    with ops.ops_session() as s:
        rows = s.execute(select(ops.RunRecord.source, ops.RunRecord.started_at,
                                ops.RunRecord.inserted, ops.RunRecord.updated)
                         .where(ops.RunRecord.mode == "fresh",
                                ops.RunRecord.status != "running",
                                ops.RunRecord.started_at >= since)).all()
    by_source: dict[str, list[float]] = {}
    written: dict[str, int] = {}
    for source, started, inserted, updated in rows:
        day = int(_hours(now - started) // 24)        # 0 — останні 24 год
        days = by_source.setdefault(source, [0.0] * (LOW_DAYS + 1))
        if day <= LOW_DAYS:
            days[day] += inserted or 0
        if day == 0:
            written[source] = written.get(source, 0) + (inserted or 0) + (updated or 0)
    alerts = []
    for name in enabled_sources():
        days = by_source.get(name)
        if not days or not written.get(name):
            continue                                   # нуль записаних — check_sources
        usual = statistics.median(days[1:])
        if usual >= DROP_MIN_BASELINE and days[0] < DROP_RATIO * usual:
            alerts.append(Alert(f"low:{name}", (
                f"📉 {name}: нових оголошень за добу {days[0]:.0f}, звичайно ~{usual:.0f} на "
                f"добу (медіана {LOW_DAYS} діб). Записи йдуть, але нового майже немає — "
                f"парсер міг частково зламатись.")))
    return alerts


def _alerts_cfg():
    from . import configfiles

    return configfiles.load("alerts")


def _probe_site(url: str, timeout: float) -> tuple[bool, str]:
    """GET /healthz: (живий?, пояснення). Без пароля — /healthz відкритий (auth.OPEN_PATHS)."""
    import httpx

    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
    except Exception as e:                                       # noqa: BLE001
        return False, type(e).__name__
    return (r.status_code == 200), f"HTTP {r.status_code}"


SITE_PROBE = _probe_site


def check_site(now: datetime, state: dict, cfg=None) -> list[Alert]:
    """Сайт не відкривається (критичне, D55 п. 6): локально (127.0.0.1:8000/healthz) і
    за публічною адресою (data/public_url + /healthz). Лише після `site.fail_runs` невдалих
    запусків сторожа поспіль: перезапуск realty-web триває 13–15 с (D54)."""
    cfg = cfg or _alerts_cfg()
    st = state.setdefault("_site", {})
    targets = [("site-local", cfg.site.local_url)]
    url = public_url() if cfg.site.check_public else None
    if url:
        targets.append(("site-public", url.rstrip("/") + cfg.site.public_path))
    else:
        st.pop("site-public", None)
    alerts = []
    for key, target in targets:
        ok, why = SITE_PROBE(target, cfg.site.timeout_s)
        rec = st.setdefault(key, {"fails": 0})
        if ok:
            st[key] = {"fails": 0, "last_ok": now.isoformat()}
            continue
        rec["fails"] = int(rec.get("fails") or 0) + 1
        rec.setdefault("first_fail", now.isoformat())
        rec["last_error"] = why
        if rec["fails"] >= cfg.site.fail_runs:
            first = datetime.fromisoformat(rec["first_fail"])
            where = "на машині (realty-web)" if key == "site-local" else "ззовні (тунель)"
            alerts.append(Alert(key, (
                f"🌐 Сайт не відкривається {where}: {target} — {why}; {rec['fails']} перевірок "
                f"поспіль, з {_ago(first, now)}. Останній успіх: "
                f"{_ago(datetime.fromisoformat(rec['last_ok']), now) if rec.get('last_ok') else 'ще не було'}.")))
    return alerts


def _local(now: datetime, tz: str) -> datetime:
    """Наївний UTC (як в ops.db) → місцевий час поясу `tz` (наївний)."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    return now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def _db_file(name: str) -> Path | None:
    from .config import DB_URL

    url = DB_URL if name == "realty" else ops.OPS_DB_URL
    return Path(url[len("sqlite:///"):]) if url.startswith("sqlite:///") else None


def _quick_check(path: Path | None, deadline: float) -> str:
    """PRAGMA quick_check через з'єднання лише для читання; стеля — progress handler.

    «ok» | «interrupted» (не встигли до `deadline`) | «missing» | текст проблеми."""
    import sqlite3
    import time

    if path is None:
        return "не SQLite"
    if not path.exists():
        return "missing"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"[:300]
    try:
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        rows = con.execute("PRAGMA quick_check").fetchall()
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e).lower():
            return "interrupted"
        return f"{type(e).__name__}: {e}"[:300]
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"[:300]
    finally:
        con.close()
    values = [str(r[0]) for r in rows]
    return "ok" if values == ["ok"] else "; ".join(values)[:300]


INTEGRITY_CHECK = _quick_check


def check_db_integrity(now: datetime, state: dict, cfg=None) -> list[Alert]:
    """База пошкоджена (критичне, D55 п. 6): раз на місцеву добу після integrity.at —
    PRAGMA quick_check realty.db і ops.db (лише читання, спільна стеля max_seconds).
    Тривога тримається, доки наступна щоденна перевірка не скаже «ok». «Перервано»
    (не встигли) і відсутній ops.db — не тривога, а рядок у зведенні."""
    import time

    cfg = cfg or _alerts_cfg()
    st = state.setdefault("_integrity", {})
    if cfg.integrity.enabled:
        local = _local(now, cfg.digest.timezone)
        h, m = (int(x) for x in cfg.integrity.at.split(":"))
        today = local.date().isoformat()
        if (local.hour, local.minute) >= (h, m) and st.get("date") != today:
            deadline = time.monotonic() + cfg.integrity.max_seconds
            results = {}
            for name in cfg.integrity.databases:
                t0 = time.monotonic()
                res = INTEGRITY_CHECK(_db_file(name), deadline)
                results[name] = {"result": res, "seconds": round(time.monotonic() - t0, 2)}
            st.update(date=today, at=now.isoformat(), results=results)
    alerts = []
    for name, r in (st.get("results") or {}).items():
        res = r.get("result")
        if res in ("ok", "interrupted") or (res == "missing" and name == "ops"):
            continue
        alerts.append(Alert(f"db-integrity:{name}", (
            f"🧨 База {name}: PRAGMA quick_check — {res} (перевірка "
            f"{_ago(datetime.fromisoformat(st['at']), now)}). Нічого не виправляти "
            f"автоматично: спершу копія й рішення власника.")))
    return alerts


def check_sample(now: datetime) -> list[Alert]:
    """Контрольна вибірка Блоку 1 (`cli.py liveness sample`, D58): останній прогін.

    Критичні — контрольне «знято» (як спрацювання запобіжника на canary) і «знято» понад
    fuse.share випадкових; попередження — понад max_removed_share і пропущений прогін
    (замок). Одне повідомлення на прогін (`once`), тривога тримається до наступного
    прогону, але не довше за sample.toml verdict.alert_days."""
    from . import configfiles
    from .liveness import sample

    last = sample.last_run()
    if last is None or last["status"] == "running":
        return []
    alert_days = configfiles.load("sample").verdict.alert_days
    if now - (last["finished_at"] or last["started_at"]) > timedelta(days=alert_days):
        return []
    once = str(last["id"])
    alerts = []
    if last["status"] == "lock_timeout":
        alerts.append(Alert("liveness-sample-skipped", (
            f"🎯 Контрольна вибірка №{last['id']} не відбулась: цикл не звільнив замок за "
            f"{(last['lock_waited_s'] or 0) / 60:.0f} хв. Наступна — за розкладом; вручну — "
            f"cli.py liveness sample."), once=once))
    for source, v in sorted((last.get("verdicts") or {}).items()):
        head = f"🎯 Контрольна вибірка №{last['id']}, {source}: "
        if v.get("status") == "canary":
            alerts.append(Alert(f"liveness-sample-canary:{source}", head + (
                f"відомо живе оголошення показало «знято» ({v.get('canary_removed')} із "
                f"{v.get('canary_checked')} контрольних) — підпис, схоже, зламався; решту "
                f"вибірки для джерела не перевіряли."), once=once))
        elif v.get("status") == "fuse_share":
            alerts.append(Alert(f"liveness-sample-share:{source}", head + (
                f"підпис зняття спрацював на >{100 * v.get('fuse_share', 0):.0f}% випадкової "
                f"вибірки — {v.get('removed')} із {v.get('checked')} — рішення власника; "
                f"нічого автоматично не зроблено."), once=once))
        elif v.get("status") == "fail":
            alerts.append(Alert(f"liveness-sample-removed:{source}", head + (
                f"«знято» {v.get('removed')} із {v.get('checked')} випадкових "
                f"({100 * (v.get('share') or 0):.1f}% > {100 * v.get('max_share', 0):.0f}%) — "
                f"див. cli.py liveness sample --report (запізнення виявлення?)."), once=once))
    return alerts


# --- Рівні й нагальне надсилання (D58) ------------------------------------------------------

CRITICAL, WARNING = "critical", "warning"
CRITICAL_HEAD = "🚨 КРИТИЧНО — потрібна дія"
# Усі префікси ключів, які сторож може видати (і `alert unit-failed`). Тест
# tests/test_alerts.py звіряє цей перелік із кодом (літерали ключів у цьому файлі) і
# вимагає ЯВНИЙ рівень кожного в config/alerts.toml — без запасного «critical».
ALERT_KEYS = (
    "silence", "drop", "write", "low", "blocks", "verify-blocks",
    "backup-none", "backup-partial", "db-integrity", "site-local", "site-public",
    "unit-failed",
    "dedup-suspicious", "dedup-missed", "dedup-complex",
    "liveness-fuse", "liveness-coverage", "liveness-ria-unrecognized", "liveness-repeat404",
    "liveness-snapshot-stale", "liveness-sample-canary", "liveness-sample-share",
    "liveness-sample-removed", "liveness-sample-skipped",
    "night-missing", "night-skipped", "night-backup", "night-failed", "night-late",
    "night-blocked", "night-hold",
    "places-failed", "places-would-change",
    "watchdog",
)


def level_of(key: str, levels) -> str:
    """Рівень ключа: найдовший префікс з `levels` (ключ == префікс або «префікс:…»);
    без збігу — critical (обережний бік: невідома тривога не ховається в зведення)."""
    best, level = -1, CRITICAL
    for prefix, lvl in (levels or {}).items():
        if (key == prefix or key.startswith(prefix + ":")) and len(prefix) > best:
            best, level = len(prefix), lvl
    return level


def action_for(key: str, cfg) -> str | None:
    """Рядок «що робити» з alerts.toml [actions] за тим самим найдовшим префіксом."""
    if cfg is None:
        return None
    best, text = -1, None
    for prefix, act in cfg.actions.items():
        if (key == prefix or key.startswith(prefix + ":")) and len(prefix) > best:
            best, text = len(prefix), act
    if text is None:
        return None
    arg = key.split(":", 1)[1] if ":" in key else ""
    return text.replace("{arg}", arg)


def critical_message(key: str, text: str, cfg, *, ongoing_since: str | None = None) -> str:
    """Критичне повідомлення — інший вигляд, ніж у зведення: перший рядок CRITICAL_HEAD."""
    prefix = (f"(досі триває, з {ongoing_since[:16].replace('T', ' ')} UTC) "
              if ongoing_since else "")
    lines = [CRITICAL_HEAD, _header(), prefix + text]
    act = action_for(key, cfg)
    if act:
        lines.append(f"👉 Що робити: {act}")
    return "\n".join(lines)


# --- Черга недоставленого й впалі служби (D58) ------------------------------------------------

OUTBOX_PATH = DATA_DIR / "alerts_outbox.json"
UNITS_PATH = DATA_DIR / "unit_failures.json"


class _LockedJson:
    """JSON-файл під flock (кілька процесів realty-alert@ і сторож одночасно)."""

    def __init__(self, path: Path, empty) -> None:
        self.path, self.empty = path, empty

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = open(self.path.with_suffix(".lock"), "a+")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = self.empty()
        return self

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    def __exit__(self, *exc):
        import fcntl

        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()
        return False


def queue_outbox(key: str, text: str, now: datetime, path: Path | None = None) -> None:
    """Нагальне повідомлення не пішло (Telegram недоступний) — сторож повторить."""
    with _LockedJson(path or OUTBOX_PATH, list) as box:
        box.data.append({"key": key, "text": text, "at": now.isoformat()})
        box.data = box.data[-50:]
        box.save()


def flush_outbox(send, errors: list, path: Path | None = None) -> list[str]:
    """Надіслати те, що чекає в черзі; що не пішло — лишається на наступний запуск."""
    path = path or OUTBOX_PATH
    if not path.exists():
        return []
    sent, keep = [], []
    with _LockedJson(path, list) as box:
        for item in box.data:
            try:
                send(f"{item['text']}\n⏳ Надіслано із запізненням (подія "
                     f"{str(item.get('at', ''))[:16].replace('T', ' ')} UTC).")
                sent.append(item.get("key", "?"))
            except Exception as e:                               # noqa: BLE001
                keep.append(item)
                errors.append(f"{item.get('key')} (черга): {notify._mask(str(e))[:200]}")
        box.data = keep
        box.save()
    return sent


_SECRET_ENV = re.compile(r"TOKEN|PASSWORD|PASSWD|SECRET|KEY|CHAT_ID|AUTH|COOKIE", re.I)
_SECRET_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),                       # токен бота Telegram
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization|bearer|"
               r"cookie|session)\b(\s*[:=]\s*|\s+)\S+"),
    re.compile(r"[A-Za-z0-9+_-]{40,}={0,2}"),          # довгі непрозорі рядки (шляхи з «/» — ні)
)


def _scrub(line: str) -> str:
    """Рядок журналу без того, що схоже на секрет (значення секретних змінних, токени)."""
    for name, value in os.environ.items():
        if _SECRET_ENV.search(name) and value and len(value) >= 4:
            line = line.replace(value, "***")
    line = _SECRET_PATTERNS[0].sub("***", line)
    line = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}=***", line)
    line = _SECRET_PATTERNS[2].sub("***", line)
    return notify._mask(line)[:200]


def _journal_tail(unit: str, lines: int) -> list[str]:
    import subprocess

    if lines <= 0:
        return []
    try:
        r = subprocess.run(["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager",
                            "-o", "cat"], capture_output=True, text=True, timeout=10)
    except Exception:                                            # noqa: BLE001
        return []
    return [_scrub(x) for x in r.stdout.splitlines()[-lines:] if x.strip()]


JOURNAL_TAIL = _journal_tail
UNIT_RE = re.compile(r"[A-Za-z0-9@._:\\-]{1,128}")


def unit_failed(unit: str, *, now: datetime | None = None, send=None, cfg=None,
                path: Path | None = None, outbox: Path | None = None) -> int:
    """`cli.py alert unit-failed <юніт>` (OnFailure=realty-alert@%n.service): критичне
    одразу. Сам НІКОЛИ не падає: будь-яка помилка — рядок у stderr і код 1.

    Служба з Restart= може падати щохвилини — повідомлення не частіше ніж раз на
    units.repeat_minutes на службу, далі лічильник. Не надіслалось — у чергу сторожа."""
    try:
        now = now or ops._now()
        send = send or notify.send_message
        unit = unit if UNIT_RE.fullmatch(unit or "") else "невідома-служба"
        try:
            cfg = cfg or _alerts_cfg()
        except Exception as e:                                   # noqa: BLE001
            # Зламаний alerts.toml не має приглушити падіння служби: без ліміту й журналу.
            log.error("config/alerts.toml не читається: %s", e)
            cfg = None
        repeat = timedelta(minutes=cfg.units.repeat_minutes) if cfg else timedelta(0)
        with _LockedJson(path or UNITS_PATH, dict) as book:
            rec = book.data.setdefault(unit, {"first": now.isoformat(), "count": 0,
                                              "unsent": 0, "last_sent": None})
            rec["count"] += 1
            rec["unsent"] += 1
            rec["last"] = now.isoformat()
            last_sent = rec.get("last_sent")
            due = last_sent is None or now - datetime.fromisoformat(last_sent) >= repeat
            if due:
                rec["last_sent"] = now.isoformat()
                times, rec["unsent"] = rec["unsent"], 0
            # Старші за тиждень записи — геть (зведення дивиться на добу).
            for name in [n for n, r in book.data.items()
                         if now - datetime.fromisoformat(r.get("last", r["first"]))
                         > timedelta(days=7)]:
                del book.data[name]
            book.save()
        if not due:
            print(f"{unit}: повідомлення вже було {last_sent} — лише лічильник")
            return 0
        tz = cfg.digest.timezone if cfg else "Europe/Kyiv"
        when = _local(now, tz).strftime("%d.%m %H:%M")
        text = [f"⛔ Служба {unit} упала (systemd: failed) — {when} за місцевим часом."]
        if times > 1:
            text.append(f"Від попереднього повідомлення падала ще {times - 1} раз(и).")
        tail = JOURNAL_TAIL(unit, cfg.units.log_lines if cfg else 0)
        if tail:
            text.append("Останні рядки журналу:")
            text += [f"  {x}" for x in tail]
        msg = critical_message(f"unit-failed:{unit}", "\n".join(text), cfg)
        try:
            send(msg)
        except Exception as e:                                   # noqa: BLE001
            log.error("unit-failed %s: не надіслано (%s) — у чергу сторожа", unit,
                      notify._mask(str(e))[:200])
            queue_outbox(f"unit-failed:{unit}", msg, now, outbox)
            return 1
        print(f"{unit}: критичне повідомлення надіслано")
        return 0
    except Exception as e:                                       # noqa: BLE001
        try:
            print(f"alert unit-failed: {type(e).__name__}: {notify._mask(str(e))[:200]}",
                  file=sys.stderr)
        except Exception:                                        # noqa: BLE001
            pass
        return 1


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
    """Усі перевірки. Помилка однієї — тривога «watchdog:<перевірка>», решта працюють;
    рівень такої тривоги — з alerts.toml: перевірки, що стережуть КРИТИЧНЕ (тиша, бекап,
    сайт, база, запобіжник, ніч), — critical, інакше впала перевірка ховала б аварію в
    зведенні (D58)."""
    alerts: list[Alert] = []
    for name, check in (("check_silence", lambda: [a] if (a := check_silence(now, state)) else []),
                        ("check_backup", lambda: check_backup(now)),
                        ("check_site", lambda: check_site(now, state)),
                        ("check_db_integrity", lambda: check_db_integrity(now, state))):
        try:
            alerts += check()
        except Exception as e:           # сторож не має падати через одну перевірку
            log.exception("перевірка %s впала", name)
            alerts.append(Alert(f"watchdog:{name}",
                                f"⚠️ Сторож не зміг виконати {name}: {notify._mask(str(e))[:300]}"))
    for check in (check_sources, check_low_sources, check_verify_blocks, check_dedup,
                  check_liveness, check_night, check_places, check_sample):
        try:
            alerts += check(now)
        except Exception as e:
            log.exception("перевірка %s впала", check.__name__)
            alerts.append(Alert(f"watchdog:{check.__name__}",
                                f"⚠️ Сторож не зміг виконати {check.__name__}: "
                                f"{notify._mask(str(e))[:300]}"))
    return alerts


# Перейменовані ключі (D58): тривога, що вже надіслана під старим ім'ям, не дає хибного
# «✅ Відновилось», а продовжується під новим.
RENAMED_KEYS = {"backup": "backup-none", "watchdog": "watchdog:check_silence"}


def _migrate_state(state: dict) -> None:
    for old, new in RENAMED_KEYS.items():
        if old in state and new not in state:
            state[new] = state.pop(old)


def _remember_resolved(state: dict, key: str, entry: dict, now: datetime, keep_h: float) -> None:
    """Зникла тривога — у стан для зведення (обидва рівні), старші за keep_h — геть."""
    resolved = [r for r in state.get("_resolved", [])
                if now - datetime.fromisoformat(r["resolved"]) <= timedelta(hours=keep_h)]
    resolved.append({"key": key, "level": entry.get("level", CRITICAL),
                     "since": entry.get("since"), "resolved": now.isoformat(),
                     "sent": entry.get("sent", 0), "text": (entry.get("text") or "")[:300]})
    state["_resolved"] = resolved[-200:]


def run(now: datetime | None = None, send=None, state_path: Path = STATE_PATH, *,
        digest: bool = False) -> dict:
    """Одна перевірка. Повертає, що надіслано, що притримано, що відновилось.

    Рівні (D55 п. 6, D58): critical — одразу, як і досі (повтор раз на REPEAT_HOURS,
    «✅ Відновилось»), але з першим рядком CRITICAL_HEAD і «що робити»; warning — лише
    стан (з коли, коли востаннє, текст; зникле — у `_resolved`) для щоденного зведення.
    `digest=True` (`cli.py watchdog`, таймер) — ще й зведення, якщо настав його час
    (realty/digest.py); тести й ручні виклики без нього зведення не шлють.
    """
    now = now or ops._now()
    send = send or notify.send_message
    state = load_state(state_path)
    _migrate_state(state)
    report = {"active": [], "sent": [], "held": [], "resolved": [], "warned": [],
              "errors": [], "outbox": [], "digest": None}
    report["outbox"] = flush_outbox(send, report["errors"])
    cfg_error = None
    try:
        cfg = _alerts_cfg()
    except Exception as e:                                       # noqa: BLE001
        # Без рівнів — усе критичне (обережний бік: нічого не ховається в зведення).
        cfg, cfg_error = None, notify._mask(str(e))[:300]
    alerts = collect(now, state)
    if cfg is None:
        alerts.append(Alert("watchdog:alerts-config", (
            f"⚠️ config/alerts.toml не читається — усі тривоги надсилаю одразу як критичні, "
            f"зведення не буде: {cfg_error}")))
    report["active"] = [a.key for a in alerts]
    levels = cfg.levels if cfg is not None else {}

    for a in alerts:
        level = level_of(a.key, levels)
        st = state.setdefault(a.key, {"since": now.isoformat(), "last_sent": None, "sent": 0})
        st["text"] = a.text
        st["level"] = level
        st["last_seen"] = now.isoformat()
        if level == WARNING:
            # Попередження — у зведення: стан є, повідомлення немає.
            report["warned"].append(a.key)
            continue
        last = st.get("last_sent")
        due = last is None or _hours(now - datetime.fromisoformat(last)) >= REPEAT_HOURS
        if a.once is not None:
            due = st.get("once") != a.once
        if not due:
            report["held"].append(a.key)
            continue
        try:
            send(critical_message(a.key, a.text, cfg,
                                  ongoing_since=st["since"] if st["sent"] else None))
            st["last_sent"] = now.isoformat()
            st["sent"] += 1
            if a.once is not None:
                st["once"] = a.once
            report["sent"].append(a.key)
        except Exception as e:
            # Не відмічаємо як надіслане — наступний запуск спробує ще раз.
            report["errors"].append(f"{a.key}: {notify._mask(str(e))[:200]}")
            log.error("не вдалось надіслати %s: %s", a.key, notify._mask(str(e))[:200])

    active = {a.key for a in alerts}
    keep_h = cfg.digest.keep_resolved_hours if cfg is not None else 72
    for key in [k for k in state if not k.startswith("_") and k not in active]:
        entry = state[key]
        if entry.get("sent"):
            try:
                send(f"{_header()}\n✅ Відновилось: {key} "
                     f"(тривога з {entry['since'][:16].replace('T', ' ')} UTC).")
            except Exception as e:
                report["errors"].append(f"{key} (відновлення): {notify._mask(str(e))[:200]}")
                continue
        _remember_resolved(state, key, entry, now, keep_h)
        del state[key]
        report["resolved"].append(key)

    if digest and cfg is not None:
        from . import digest as digest_mod

        try:
            report["digest"] = digest_mod.maybe_send(now, state, cfg, send)
        except Exception as e:                                   # noqa: BLE001
            log.exception("зведення не зібрано")
            report["errors"].append(f"зведення: {notify._mask(str(e))[:200]}")
        if report["digest"] and report["digest"].get("error"):
            report["errors"].append(f"зведення: {report['digest']['error']}")

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
