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
import os
import time
from datetime import datetime, timedelta, timezone

from pathlib import Path

from sqlalchemy import func, or_, select, update

from . import identity, ops, txnwatch
from .config import DATA_DIR
from .db import SessionLocal, init_db
from .fetcher import BLOCKING_CODES, FetchError, Fetcher
from .models import Listing, PriceEvent
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


class Stop(FetchError):
    """Дозбір у нічній смузі зупинено ДО запиту: дедлайн смуги чи блокування хоста
    (рецензія E9, D53). Картку не питали — «пробували» не позначаємо."""


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_source_state(source: str, entry: dict) -> None:
    """Записати стан ОДНОГО джерела. Уночі проходи LUN і flombu йдуть паралельно в
    різних смугах (E9): кожен перечитує файл перед записом і міняє лише свій ключ, а
    тимчасовий файл — свій для кожного процесу (рецензія E9, D53: інакше прохід LUN
    затирав би стан flombu, і той ішов би щовікна)."""
    state = _state()
    state[source] = entry
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.tmp.{os.getpid()}")
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
        work(sources, deadline, report)
    finally:
        lock.release()
    ops.beat(f"дозбір identity: {report['done']}", busy=False)
    return report


def work(sources: list[str], deadline: float, report: dict, *, gate=None) -> dict:
    """Сам дозбір — без замка (його тримає той, хто кличе: `run` чи нічний диригент).

    Нічний диригент (`cli.py night`, E9, D53) кличе це у смузі хоста джерела —
    dom.ria.com → domria, lun.ua → lun, flombu.com → flombu — ПІСЛЯ перевірок
    актуальності цієї смуги (пріоритет Блоку 1 над дозбором; інтеграція, конфлікт 3):
    так до сайту йде один потік запитів, а не два. `deadline` — time.monotonic().

    `gate` — смуга (night.lane.IdentityGate): той самий темп старт-до-старту, що й у
    перевірок смуги, її дедлайн і її лічильники блокувань (рецензія E9, D53); без
    нього — як раніше (ручний `cli.py identity`).
    """
    report.setdefault("done", {})
    report.setdefault("left", {})
    report.setdefault("errors", 0)
    # Короткі повні проходи — першими, DIM.RIA — на решту бюджету.
    for source in sorted(sources, key=lambda x: x == "domria"):
        if time.monotonic() >= deadline:
            break
        if source == "domria":
            _domria(deadline, report, gate=gate)
        elif source in ("lun", "flombu"):
            _full_pass(source, deadline, report, gate=gate)
    with SessionLocal() as s:
        for source in sources:
            report["left"][source] = _count(s, missing(s, source))
    return report


def _domria(deadline: float, report: dict, *, gate=None) -> None:
    """Картки DIM.RIA пакетами по BATCH: спершу всі запити пакета БЕЗ відкритої
    транзакції, потім один короткий запис (рецензія E9, D53). Раніше перший UPDATE
    брав замок запису SQLite і тримав його, поки йшли решта карток пакета (~30–60 с
    мережі), — уночі пакет застосування смуг чекав би довше за busy_timeout (30 с)."""
    fetcher = (gate.fetcher(RIA_DELAY, "domria") if gate is not None
               else Fetcher(delay=RIA_DELAY, label="domria"))
    # Код останньої відповіді: 401/403/429 — сайт відмовив, а не «картки немає»; таку
    # картку не позначаємо «пробували» — інакше її не спитали б уже ніколи (рецензія E9).
    last = {"code": None}
    observe = getattr(fetcher, "on_status", None)

    def on_status(code: int) -> None:
        last["code"] = code
        if observe is not None:
            observe(code)

    fetcher.on_status = on_status
    done = 0
    try:
        while time.monotonic() < deadline:
            with SessionLocal() as s:
                rows = s.execute(missing(s, "domria").limit(BATCH)).all()
            if not rows:
                break
            got: list[tuple[int, dict]] = []
            stopped = False
            for lid, ext in rows:
                if time.monotonic() >= deadline or (gate is not None and gate.stopped()):
                    stopped = True
                    break
                last["code"] = None
                try:
                    ident = identity.from_domria(
                        fetcher.get_json(RIA_CARD.format(ext), {"lang_id": 4}))
                except Stop:
                    stopped = True
                    break
                except FetchError as e:
                    if last["code"] in BLOCKING_CODES:
                        report["blocked"] = report.get("blocked", 0) + 1
                        continue
                    # Картки немає (знято) чи сайт не відповів — нічого не
                    # вигадуємо, лише позначаємо «пробували», щоб не смикати
                    # цей запис щоночі.
                    report["errors"] += 1
                    ident = {"unavailable": str(e)[:80]}
                ident.setdefault("flat", "ria:?")      # пробували; id немає
                got.append((lid, ident))
            if got:
                with SessionLocal() as s:
                    for lid, ident in got:
                        old = s.scalar(select(Listing.identity).where(Listing.id == lid))
                        # Пряме UPDATE із last_seen = last_seen: інакше onupdate
                        # «освіжив» би і зняті оголошення — дозбір їх не бачив у стрічці.
                        s.execute(update(Listing).where(Listing.id == lid).values(
                            identity={**(old or {}), **ident}, last_seen=Listing.last_seen))
                    s.commit()
                done += len(got)
                # Нічний пакет записано — покоління кешу сайту (Блок 2, E5, D50),
                # не чекаючи кінця дозбору (до 105 хв).
                bump = txnwatch.autobump()
                if bump is not None:
                    bump.bump_if_dirty("lists", "дозбір identity")
            if stopped or not got:
                # Нічого не записано — увесь пакет відмовлено (401/403/429): ті самі
                # рядки взялися б знову, тож далі не питаємо.
                break
    finally:
        fetcher.close()
        report["done"]["domria"] = done


def _written(source: str) -> tuple[int, int]:
    """(рядків джерела, подій ціни його рядків) — що дописав прохід стрічки."""
    with SessionLocal() as s:
        rows = s.scalar(select(func.count()).select_from(Listing)
                        .where(Listing.source == source)) or 0
        prices = s.scalar(select(func.count()).select_from(PriceEvent)
                          .join(Listing, Listing.id == PriceEvent.listing_id)
                          .where(Listing.source == source)) or 0
    return rows, prices


def _full_pass(source: str, deadline: float, report: dict, *, gate=None) -> None:
    """Повний прохід окремим процесом зі стелею часу, як крок циклу: інакше
    прохід, що затягнувся, тримав би замок і з'їв би наступний цикл збору.

    Лише стрічка (`--no-detail`, рецензія E9, D53): identity LUN і flombu — у самій
    стрічці, а сторінки деталей LUN — це rieltor.ua, olx.ua і dom.ria.com, куди вночі
    ходять лише їхні смуги у своєму темпі. У смузі — ще й `--min-delay` = нічний темп
    хоста (flombu: повний збір 1,0 с, а нічний темп 1,2 с).

    Прохід — це звичайний збір стрічки (pipeline._upsert): нові оголошення, події ціни
    й last_seen (D43 — законно). Скільки рядків і подій ціни він дописав, іде у звіт
    (`written`): нічний запис звіряє «актуальних після» з ними (рецензія E9, D53)."""
    st = _state().get(source, {})
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
    pace: tuple[str, ...] = ()
    if gate is not None:
        if gate.stopped():
            report["done"][source] = "пропущено: смугу зупинено (дедлайн чи блокування)"
            return
        # Перший запит проходу — не раніше за темп смуги після її останнього запиту.
        gate.wait()
        pace = ("--min-delay", f"{gate.pace:g}")
    rows0, prices0 = _written(source)
    # Без LLM: дозбір лише освіжає ознаки, платні виклики тут ні до чого.
    step = Step(f"дозбір {source}", _cli("scrape", "--no-detail", *pace, "--sources", source,
                                         "--mode", "full", "--no-llm", "--trigger", "manual"),
                timeout=deadline - time.monotonic())
    res, _ = run_step(step, budget=deadline - time.monotonic())
    rows1, prices1 = _written(source)
    report.setdefault("written", {})[source] = {"listings": rows1 - rows0,
                                                "price_events": prices1 - prices0}
    with SessionLocal() as s:
        left = _count(s, missing(s, source, active_only=True))
    _save_source_state(source, {"at": now.isoformat(), "gain": need - left, "left": left,
                                "status": res.status})
    report["done"][source] = f"{res.status}: активних без ознак {need} → {left}"
