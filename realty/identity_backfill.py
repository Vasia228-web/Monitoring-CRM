"""Нічний дозбір сильних ознак (identity) для вже зібраних оголошень.

Нові оголошення отримують identity під час звичайного збору. Для старих:
  * DIM.RIA — картка кожного оголошення (той самий запит, що й у зборі);
  * LUN і flombu — повний прохід списком (там identity є прямо в стрічці:
    ~240 і ~40 сторінок замість тисяч карток). Вони короткі (~15 хв), тому
    йдуть першими, а DIM.RIA забирає решту бюджету;
  * OLX — лише зі сторінки оголошення, тож дозбору немає: identity
    накопичується, коли збір відкриває сторінки деталей.

Ніколи не паралельно зі звичайним збором: дозбір тримає ТОЙ САМИЙ замок, що й
цикл (`runner.CycleLock`). Якщо цикл ще йде — чекає, поки той закінчиться
(цикл триває близько 55 хв і може накластись на початок вікна), але не довше,
ніж лишає собі час на роботу. Бюджет часу рахується від початку вікна, щоб
воно в будь-якому разі закінчилось до наступного циклу; між вікнами — продовжує
з того місця, де зупинився (бере лише записи без identity).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from pathlib import Path

from sqlalchemy import func, or_, select, update

from . import identity, ops
from .config import DATA_DIR
from .db import SessionLocal, init_db
from .fetcher import FetchError, Fetcher
from .models import Listing
from .runner import DISABLED_FLAG, LOCK_PATH, CycleLock, Step, _cli, run_step

log = logging.getLogger(__name__)

RIA_CARD = "https://dom.ria.com/realty/data/{}"
RIA_DELAY = 1.2          # як у звичайному зборі DIM.RIA
BATCH = 25
LOCK_POLL = 60           # як часто перевіряти, чи звільнився замок
MIN_WORK = 10 * 60       # менше десяти хвилин роботи — вікно не варте заходу


# Повний прохід списком LUN/flombu має сенс, лише коли в АКТИВНИХ оголошень
# справді бракує ознак: зняті повним проходом уже не знайти, і без цього
# порогу LUN щоночі проходився б весь заради записів, яких на сайті немає.
FULL_PASS_MIN = 50
# Прохід LUN займає близько години — майже все вікно. У ніч на 23.09 він
# відпрацював двічі: перший раз дав 3 783 записи, другий — 80, і година пішла
# намарно. Тому не частіше ніж раз на добу, а якщо попередній прохід дав мало —
# раз на тиждень.
PASS_COOLDOWN = timedelta(hours=24)
PASS_COOLDOWN_LOW = timedelta(days=7)
PASS_MIN_GAIN = 200
STATE_PATH = DATA_DIR / "identity_backfill.json"


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(STATE_PATH)


def missing(session, source: str, active_only: bool = False):
    """Оголошення джерела, у яких ще немає сильних ознак (і які ще не пробували).

    Для LUN і flombu ознакою «дозібрано» є сам факт identity: координати там
    дає не кожне оголошення, і вимога координат робила такі записи вічно
    недозібраними — через це прохід LUN просився щоночі.
    """
    stmt = (select(Listing.id, Listing.external_id)
            .where(Listing.source == source, Listing.identity.is_(None))
            .order_by(Listing.is_active.desc(), Listing.id.desc()))
    if source == "domria":                    # там ознака — саме id квартири
        stmt = (select(Listing.id, Listing.external_id)
                .where(Listing.source == source,
                       or_(Listing.identity.is_(None),
                           func.json_extract(Listing.identity, "$.flat").is_(None)))
                .order_by(Listing.is_active.desc(), Listing.id.desc()))
    if active_only:
        stmt = stmt.where(Listing.is_active.is_(True))
    return stmt


def _count(session, stmt) -> int:
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def run(sources: list[str], budget_s: float, lock_path: Path = LOCK_PATH,
        disabled_flag: Path = DISABLED_FLAG) -> dict:
    init_db()
    report = {"status": "ok", "done": {}, "left": {}, "errors": 0}
    if disabled_flag.exists():
        report["status"] = "disabled"
        return report
    deadline = time.monotonic() + budget_s
    lock = CycleLock(lock_path)
    waited = 0
    while not lock.acquire():
        if time.monotonic() + LOCK_POLL > deadline - MIN_WORK:
            report["status"] = f"skipped: цикл збору не звільнив замок ({waited // 60} хв)"
            log.info("дозбір identity: %s", report["status"])
            return report
        time.sleep(LOCK_POLL)
        waited += LOCK_POLL
    if waited:
        report["waited_min"] = waited // 60
    try:
        # Короткі повні проходи — першими, DIM.RIA — на решту бюджету.
        for source in sorted(sources, key=lambda x: x == "domria"):
            if time.monotonic() >= deadline:
                break
            if source == "domria":
                _domria(deadline, report)
            elif source in ("lun", "flombu"):
                _full_pass(source, deadline, report)
        with SessionLocal() as s:
            for source in sources:
                report["left"][source] = _count(s, missing(s, source))
    finally:
        lock.release()
    ops.beat(f"дозбір identity: {report['done']}", busy=False)
    return report


def _domria(deadline: float, report: dict) -> None:
    fetcher = Fetcher(delay=RIA_DELAY, label="domria")
    done = 0
    try:
        while time.monotonic() < deadline:
            with SessionLocal() as s:
                rows = s.execute(missing(s, "domria").limit(BATCH)).all()
                if not rows:
                    break
                for lid, ext in rows:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        ident = identity.from_domria(
                            fetcher.get_json(RIA_CARD.format(ext), {"lang_id": 4}))
                    except FetchError as e:
                        # Картки немає (знято) чи сайт не відповів — нічого не
                        # вигадуємо, лише позначаємо «пробували», щоб не смикати
                        # цей запис щоночі.
                        report["errors"] += 1
                        ident = {"unavailable": str(e)[:80]}
                    ident.setdefault("flat", "ria:?")      # пробували; id немає
                    old = s.scalar(select(Listing.identity).where(Listing.id == lid))
                    # Пряме UPDATE із last_seen = last_seen: інакше onupdate
                    # «освіжив» би і зняті оголошення — дозбір їх не бачив у стрічці.
                    s.execute(update(Listing).where(Listing.id == lid).values(
                        identity={**(old or {}), **ident}, last_seen=Listing.last_seen))
                    done += 1
                s.commit()
    finally:
        fetcher.close()
        report["done"]["domria"] = done


def _full_pass(source: str, deadline: float, report: dict) -> None:
    """Повний прохід окремим процесом зі стелею часу, як крок циклу: інакше
    прохід, що затягнувся, тримав би замок і з'їв би наступний цикл збору."""
    state = _state()
    st = state.get(source, {})
    now = datetime.now(timezone.utc)
    if st.get("at"):
        since = now - datetime.fromisoformat(st["at"])
        cooldown = PASS_COOLDOWN_LOW if st.get("gain", 0) < PASS_MIN_GAIN else PASS_COOLDOWN
        if since < cooldown:
            report["done"][source] = (f"пропущено: попередній прохід {since.days} д "
                                      f"{since.seconds // 3600} год тому дав {st.get('gain')}")
            return
    with SessionLocal() as s:
        need = _count(s, missing(s, source, active_only=True))
    if need < FULL_PASS_MIN:
        report["done"][source] = f"не потрібно ({need} активних без ознак)"
        return
    # Без LLM: дозбір лише освіжає ознаки, платні виклики тут ні до чого.
    step = Step(f"дозбір {source}", _cli("scrape", "--sources", source, "--mode", "full",
                                         "--no-llm", "--trigger", "manual"),
                timeout=deadline - time.monotonic())
    res, _ = run_step(step, budget=deadline - time.monotonic())
    with SessionLocal() as s:
        left = _count(s, missing(s, source, active_only=True))
    state[source] = {"at": now.isoformat(), "gain": need - left, "left": left,
                     "status": res.status}
    _save_state(state)
    report["done"][source] = f"{res.status}: активних без ознак {need} → {left}"
