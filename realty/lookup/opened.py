"""Перевірка актуальності квартири, яку щойно відкрили, — у власному процесі.

Досі її робив сам сайт прямо в запиті: посилання «аналіз об'єкта» (без
verify=0) запускало HEAD-запити до джерел (медіана 1, p99 9 запитів, паузи
хостів до десятків секунд) і писало в базу — сторінка чекала мережі й
блокування запису (план Блоку 2, D48). Функція лишається тією самою —
перевірка при кожному відкритті тих самих оголошень тим самим verify_batch з
reason="opened", — але виконує її окремий процес (`cli.py lookup check --job N`),
а сторінка отримує результат банером (long-poll /api/property/{id}/liveness).

Що саме перевіряється: актуальні (is_active) оголошення квартири на сайтах, які
вміємо перевіряти (verify.is_checkable), — як і досі; плюс нове правило
інтеграції: оголошення, яке вже пробували менше ніж
`run.opened_recheck_minutes` тому (config/liveness.toml), повторно не
смикається — повторні відкриття тієї самої картки не множать запитів до
джерела.

Правила процесу (D50, виправлення після рецензії):
  * «доїдає» чергу ОДИН процес за раз — замок data/lookup.lock. Без нього
    8 відкритих підряд квартир давали 8 одночасних процесів (~80 МБ кожен на
    машині з 3,7 ГБ) і подвоєний темп запитів до тих самих хостів;
  * збір вимкнено (COLLECTOR_OFF) — завдання закривається без мережі;
  * замок циклу зайнятий (цикл, дозбір, міграція) — завдання відкладається
    (`deferred`) БЕЗ жодного запиту: інакше запити йшли б паралельно з кроком
    перевірки циклу на ті самі хости, а запис вердикту чекав би блокування
    кроку «дублі» й падав з «database is locked» (інтеграція, конфлікт 5).
    Після циклу відкладені бере наступний процес перевірки (його запускає
    сайт, livecheck.py);
  * покоління «lists» — лише якщо перевірка справді зняла чи повернула
    оголошення: службові записи (last_attempt, check_events) списку не
    змінюють, а кожне зайве покоління скидало б усі кеші списків сайту. І
    одразу після такого завдання, а не при виході: процес, убитий systemd за
    тайм-аутом, atexit не виконує.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select

from .. import configfiles, runner
from ..config import DATA_DIR, RUN_TIMEOUT
from ..models import Listing
from . import queue

log = logging.getLogger(__name__)

RESULT_KEYS = ("checked", "alive", "delisted", "restored", "unknown", "requests", "blocked")

# Замки й прапорець — атрибутами модуля: тести підставляють свої шляхи (на Mac
# розробника data/COLLECTOR_OFF існує — базу перенесено на Fedora).
DRAIN_LOCK = DATA_DIR / "lookup.lock"
CYCLE_LOCK = runner.LOCK_PATH
DISABLED_FLAG = runner.DISABLED_FLAG

# Скільки відкладене завдання чекає кінця циклу, перш ніж стати непотрібним:
# найдовше, що цикл може тримати замок, — RUN_TIMEOUT × 1,5 (після цього
# диригент примусово зупиняє завислий цикл, runner._reap_overdue_holder).
# Число не нове — похідне від наявного ліміту циклу, тому окремого ключа немає.
DEFERRED_MAX_AGE_S = RUN_TIMEOUT * 1.5


def recheck_minutes() -> float:
    return configfiles.get("liveness").run.opened_recheck_minutes


def collector_off() -> bool:
    return DISABLED_FLAG.exists()


def cycle_busy() -> dict | None:
    """Хто тримає замок циклу (не беручи його) — runner.lock_busy."""
    return runner.lock_busy(CYCLE_LOCK)


def drainer_busy() -> dict | None:
    """Чи вже працює процес перевірки, що «доїдає» чергу (не беручи замка)."""
    return runner.lock_busy(DRAIN_LOCK)


def candidates(session, property_id: int, *, now, min_interval_min: float) -> list[int]:
    """id оголошень квартири, які варто перевірити зараз (у порядку id)."""
    from ..verify import is_checkable

    cutoff = now - timedelta(minutes=min_interval_min)
    rows = session.execute(
        select(Listing.id, Listing.original_url, Listing.last_attempt)
        .where(Listing.property_id == property_id, Listing.is_active.is_(True))
        .order_by(Listing.id)).all()
    return [lid for lid, url, attempt in rows
            if is_checkable(url) and (attempt is None or attempt < cutoff)]


def _zero() -> dict:
    return {k: 0 for k in RESULT_KEYS}


def execute(job_id: int) -> tuple[str, dict]:
    """Виконати одне завдання `opened`: (кінцевий стан, результат)."""
    from .. import verify
    from ..db import SessionLocal

    job = queue.get(job_id)
    if job is None:
        return "missing", {}
    if job.kind == queue.KIND_LINK:
        # «Перевірити зараз» за посиланням (Блок 5, E14, D59) — той самий процес і
        # ті самі правила замка циклу й COLLECTOR_OFF, свій адаптер сайту.
        from . import link
        return link.execute(job_id)
    if job.kind != queue.KIND_OPENED:
        return "foreign", {}              # невідомий вид — не наше
    if job.state not in queue.RUNNABLE:
        return "taken", {}
    if collector_off():
        # Збір на цій машині вимкнено свідомо (базу перенесено) — і перевірки теж
        # (інтеграція, конфлікт 19: lookup поважає COLLECTOR_OFF).
        queue.finish(job_id, "skipped", result=_zero(), message="COLLECTOR_OFF",
                     only_if=queue.RUNNABLE)
        return "skipped", _zero()
    holder = cycle_busy()
    if holder is not None:
        if job.state == "queued":
            queue.defer(job_id, message=f"замок циклу: PID {holder.get('pid')}")
        return "deferred", {}
    if not queue.claim(job_id):
        return "taken", {}
    try:
        with SessionLocal() as s:
            ids = candidates(s, job.property_id, now=verify._now(),
                             min_interval_min=recheck_minutes())
        if not ids:
            queue.finish(job_id, "skipped", result=_zero())
            return "skipped", _zero()
        stats = verify.verify_batch(limit=len(ids), ids=ids, reason="opened")
        result = {k: int(stats.get(k) or 0) for k in RESULT_KEYS}
        queue.finish(job_id, "done", result=result)
        log.info("перевірка при відкритті %s: %s", job.key, result)
        return "done", result
    except Exception as e:                      # noqa: BLE001 — завдання закриваємо будь-як
        log.warning("перевірка при відкритті %s не вдалась: %s", job.key, e)
        queue.finish(job_id, "failed", message=f"{type(e).__name__}: {e}")
        return "failed", {}
    except BaseException:
        # SIGTERM від systemd (TimeoutStartSec) приходить як SystemExit
        # (cli.py, обробник сигналу): завдання не лишається «running» назавжди.
        queue.finish(job_id, "failed", message="процес перевірки зупинено")
        raise


def run_job(job_id: int) -> str:
    """Виконати одне завдання `opened`. Повертає кінцевий стан (або «taken»)."""
    return execute(job_id)[0]


def _announce(result: dict) -> None:
    """Покоління «lists» — одразу, якщо перевірка змінила видиме списку.

    І позначка для автоматичного покоління при виході (txnwatch): записи цього
    завдання вже враховано — службові (last_attempt, check_events) кешів не
    скидають, а видимі щойно скинуло явне покоління.
    """
    from .. import txnwatch, webcache

    if queue.changed_visibility(result):
        webcache.bump("lists", "перевірка при відкритті")
    txnwatch.mark_clean()


def run(job_id: int, *, drain: bool = True, budget_s: float, timeout_s: float,
        deferred_max_age_s: float = DEFERRED_MAX_AGE_S) -> list[tuple[int, str]]:
    """Своє завдання, а потім — інші, що чекають (поки є час). Один процес за раз.

    Замок lookup.lock зайнятий — інший процес уже «доїдає» чергу й візьме і
    це завдання: виходимо одразу («drainer-active»). Коли черга порожня, замок
    відпускається й черга перевіряється ще раз: завдання, поставлене саме в цю
    мить (сайт побачив замок зайнятим і процесу не запускав), не загубиться.
    `budget_s` — після нього нових завдань не беремо (ліміт процесу systemd —
    `timeout_s`; поточне завдання має встигнути); решту запустить сайт.
    """
    lock = runner.CycleLock(DRAIN_LOCK)
    if not lock.acquire():
        return [(job_id, "drainer-active")]
    started = time.monotonic()
    done: list[tuple[int, str]] = []
    seen: set[int] = set()
    try:
        queue.expire_deferred(queue.WORKER_KINDS, max_age_s=deferred_max_age_s)
        nxt: int | None = job_id
        while True:
            if nxt is not None:
                seen.add(nxt)
                state, result = execute(nxt)
                done.append((nxt, state))
                if state == "done":
                    _announce(result)
                if not drain or time.monotonic() - started >= budget_s:
                    break
            nxt = queue.next_runnable(queue.WORKER_KINDS, timeout_s=timeout_s,
                                      deferred_max_age_s=deferred_max_age_s,
                                      include_deferred=cycle_busy() is None, exclude=seen)
            if nxt is not None:
                continue
            # Черга порожня: відпустити замок і глянути ще раз (див. докстрінг).
            lock.release()
            nxt = queue.next_runnable(queue.WORKER_KINDS, timeout_s=timeout_s,
                                      deferred_max_age_s=deferred_max_age_s,
                                      include_deferred=cycle_busy() is None, exclude=seen)
            if nxt is None or not lock.acquire():
                break
    finally:
        lock.release()
    return done
