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

from sqlalchemy import select

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
STATE_PATH = DATA_DIR / "alerts.json"
PUBLIC_URL_PATH = DATA_DIR / "public_url"


@dataclass
class Alert:
    key: str
    text: str


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
    for field, key, what in (("suspicious", "dedup-suspicious", "підозрілих квартир"),
                             ("missed", "dedup-missed", "пропущених дублів")):
        usual = median(getattr(r, field) for r in before)
        now_n = getattr(last, field)
        if now_n > usual * DEDUP_RISE + DEDUP_RISE_MIN:
            kinds = json.loads(last.by_kind or "{}") if field == "suspicious" else {}
            top = ", ".join(f"{dedup_audit.KINDS.get(k, k)} — {n}"
                            for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:3])
            alerts.append(Alert(key, (
                f"🧩 Зведення квартир: {what} {now_n}, звичайно ~{usual:.0f} "
                f"(перевірка {_ago(last.at, now)}).{' Найчастіше: ' + top + '.' if top else ''}\n"
                f"Черга на перегляд — на /status.")))
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
    for check in (check_sources, check_verify_blocks, check_dedup):
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
        st["text"] = a.text
        if not due:
            report["held"].append(a.key)
            continue
        prefix = "" if st["sent"] == 0 else f"(досі триває, з {st['since'][:16].replace('T', ' ')} UTC) "
        try:
            send(f"{_header()}\n{prefix}{a.text}")
            st["last_sent"] = now.isoformat()
            st["sent"] += 1
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
