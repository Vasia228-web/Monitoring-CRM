"""Черга завдань ops.lookup_checks: поставити, відкласти, взяти, закрити, прочитати.

Взяття — атомарне (`UPDATE … WHERE state IN ('queued', 'deferred')`): навіть
якщо два процеси перевірки стартували одночасно, кожне завдання виконає рівно
один. «Доїдає» чергу лише один процес за раз (замок lookup.lock, opened.run).

Стани: queued → running → done | failed | skipped. `deferred` — замок циклу
був зайнятий: завдання чекає кінця циклу без жодного запиту до джерел, потім
його бере процес перевірки так само, як queued (інтеграція, конфлікт 5).

Види завдань: `opened` — перевірка при відкритті квартири (Блок 2, D50), `link` —
«Перевірити зараз» за посиланням (Блок 5, E14, D59). Обидва «доїдає» той самий
процес (opened.run), по черзі за номером.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

from sqlalchemy import func, or_, select, update

from .. import ops

KIND_OPENED = "opened"
KIND_LINK = "link"
# Що «доїдає» процес перевірки (opened.run) і за чим стежить сайт (web/livecheck.py).
WORKER_KINDS = (KIND_OPENED, KIND_LINK)
FINAL = ("done", "failed", "skipped")
RUNNABLE = ("queued", "deferred")


def opened_key(property_id: int) -> str:
    return f"property:{int(property_id)}"


def link_key(site_key: str) -> str:
    """Ключ завдання «Перевірити зараз»: «link:» + ключ «сайт:id» (≤ 64 символи)."""
    return f"link:{site_key}"


def _kind_is(kind):
    """Умова на вид: один (`"opened"`) або кілька (`WORKER_KINDS`)."""
    if isinstance(kind, str):
        return ops.LookupCheck.kind == kind
    return ops.LookupCheck.kind.in_(tuple(kind))


def enqueue(kind: str, key: str, property_id: int | None = None, *,
            state: str = "queued") -> int:
    if state not in RUNNABLE:
        raise ValueError(f"нове завдання не може мати стан {state!r}")
    ops.init_ops()
    with ops.ops_session() as s:
        job = ops.LookupCheck(kind=kind, key=key, property_id=property_id, state=state,
                              created_at=ops._now())
        s.add(job)
        s.flush()
        return job.id


def _fresh(*, timeout_s: float, deferred_max_age_s: float):
    """Умова «завдання ще живе»: queued — поставлене не раніше за тайм-аут процесу,
    running — узяте не раніше (відкладене могло чекати циклу годину й лише тепер
    стартувати), deferred — не старше за `deferred_max_age_s` (чекає кінця циклу)."""
    now = ops._now()
    fresh = now - timedelta(seconds=timeout_s)
    return or_(
        (ops.LookupCheck.state == "queued") & (ops.LookupCheck.created_at >= fresh),
        (ops.LookupCheck.state == "running")
        & (func.coalesce(ops.LookupCheck.started_at, ops.LookupCheck.created_at) >= fresh),
        (ops.LookupCheck.state == "deferred")
        & (ops.LookupCheck.created_at >= now - timedelta(seconds=deferred_max_age_s)))


def active_for(key: str, *, timeout_s: float,
               deferred_max_age_s: float | None = None) -> ops.LookupCheck | None:
    """Завдання ключа, що ще чекає, відкладене чи виконується (не застаріле)."""
    ops.init_ops()
    with ops.ops_session() as s:
        job = s.scalars(select(ops.LookupCheck)
                        .where(ops.LookupCheck.key == key,
                               _fresh(timeout_s=timeout_s,
                                      deferred_max_age_s=(timeout_s if deferred_max_age_s is None
                                                          else deferred_max_age_s)))
                        .order_by(ops.LookupCheck.id.desc()).limit(1)).first()
        if job is not None:
            s.expunge(job)
        return job


def latest_for(key: str, *, since_s: float) -> ops.LookupCheck | None:
    """Найсвіжіше завдання ключа за останні `since_s` секунд (будь-який стан)."""
    since = ops._now() - timedelta(seconds=since_s)
    ops.init_ops()
    with ops.ops_session() as s:
        job = s.scalars(select(ops.LookupCheck)
                        .where(ops.LookupCheck.key == key,
                               ops.LookupCheck.created_at >= since)
                        .order_by(ops.LookupCheck.id.desc()).limit(1)).first()
        if job is not None:
            s.expunge(job)
        return job


def get(job_id: int) -> ops.LookupCheck | None:
    ops.init_ops()
    with ops.ops_session() as s:
        job = s.get(ops.LookupCheck, job_id)
        if job is not None:
            s.expunge(job)
        return job


def claim(job_id: int) -> bool:
    """Взяти завдання в роботу; False — його вже взяв інший процес (або закрито)."""
    ops.init_ops()
    with ops.ops_session() as s:
        res = s.execute(update(ops.LookupCheck)
                        .where(ops.LookupCheck.id == job_id,
                               ops.LookupCheck.state.in_(RUNNABLE))
                        .values(state="running", started_at=ops._now(), pid=os.getpid()))
        return res.rowcount == 1


def defer(job_id: int, *, message: str | None = None) -> bool:
    """queued → deferred (замок циклу зайнятий); False — завдання вже не чекає."""
    ops.init_ops()
    with ops.ops_session() as s:
        res = s.execute(update(ops.LookupCheck)
                        .where(ops.LookupCheck.id == job_id, ops.LookupCheck.state == "queued")
                        .values(message=(message or "")[:200] or None, state="deferred"))
        return res.rowcount == 1


def next_runnable(kind, *, timeout_s: float, deferred_max_age_s: float,
                  include_deferred: bool, exclude=()) -> int | None:
    """Найстаріше завдання виду `kind`, яке можна виконати (для «доїдання» черги).

    `include_deferred=False`, поки замок циклу зайнятий: відкладені й далі
    чекають, а щойно поставлені (queued) лише позначаються відкладеними.
    """
    now = ops._now()
    states = [(ops.LookupCheck.state == "queued")
              & (ops.LookupCheck.created_at >= now - timedelta(seconds=timeout_s))]
    if include_deferred:
        states.append((ops.LookupCheck.state == "deferred")
                      & (ops.LookupCheck.created_at
                         >= now - timedelta(seconds=deferred_max_age_s)))
    stmt = (select(ops.LookupCheck.id)
            .where(_kind_is(kind), or_(*states))
            .order_by(ops.LookupCheck.id).limit(1))
    if exclude:
        stmt = stmt.where(ops.LookupCheck.id.notin_(list(exclude)))
    ops.init_ops()
    with ops.ops_session() as s:
        return s.scalar(stmt)


def unfinished(kind, *, timeout_s: float, deferred_max_age_s: float,
               limit: int = 1000) -> list[tuple[int, int | None]]:
    """[(завдання, квартира)] виду `kind`, що ще чекають, відкладені чи виконуються.

    Для сайту після перезапуску: завдання, поставлені до нього, лишаються під
    наглядом (запуск процесу після циклу, сповіщення про зміни).
    """
    ops.init_ops()
    with ops.ops_session() as s:
        return [(i, p) for i, p in s.execute(
            select(ops.LookupCheck.id, ops.LookupCheck.property_id)
            .where(_kind_is(kind),
                   _fresh(timeout_s=timeout_s, deferred_max_age_s=deferred_max_age_s))
            .order_by(ops.LookupCheck.id).limit(limit))]


def finish(job_id: int, state: str, *, result: dict | None = None,
           message: str | None = None, only_if: tuple[str, ...] | None = None) -> bool:
    """Закрити завдання. `only_if` — лише якщо воно ще в одному з цих станів."""
    ops.init_ops()
    stmt = update(ops.LookupCheck).where(ops.LookupCheck.id == job_id)
    if only_if:
        stmt = stmt.where(ops.LookupCheck.state.in_(only_if))
    with ops.ops_session() as s:
        # target (адреса запиту «Перевірити зараз») після завдання не потрібна — стерти.
        res = s.execute(stmt.values(
            state=state, finished_at=ops._now(), target=None,
            result=json.dumps(result, ensure_ascii=False) if result is not None else None,
            message=(message or "")[:200] or None))
        return res.rowcount == 1


def back_to_deferred(job_id: int, *, message: str | None = None) -> bool:
    """running → deferred: цикл почався, поки йшов запит, — запис після нього.

    Для «Перевірити зараз» (Блок 5): відповідь сайту відкидається, завдання знову
    чекає кінця циклу й після нього виконується заново (D59, відхилення 3).
    """
    ops.init_ops()
    with ops.ops_session() as s:
        res = s.execute(update(ops.LookupCheck)
                        .where(ops.LookupCheck.id == job_id, ops.LookupCheck.state == "running")
                        .values(state="deferred", started_at=None, pid=None,
                                message=(message or "")[:200] or None))
        return res.rowcount == 1


def expire_deferred(kind, *, max_age_s: float) -> int:
    """Відкладені довше за `max_age_s` — закрити як skipped (їх уже ніхто не чекає).
    Адресу запиту (target) — стерти, як і в finish (рецензія E14, 08.10)."""
    cutoff = ops._now() - timedelta(seconds=max_age_s)
    ops.init_ops()
    with ops.ops_session() as s:
        res = s.execute(update(ops.LookupCheck)
                        .where(_kind_is(kind),
                               ops.LookupCheck.state == "deferred",
                               ops.LookupCheck.created_at < cutoff)
                        .values(state="skipped", finished_at=ops._now(), target=None,
                                message="відкладена перевірка застаріла"))
        return res.rowcount or 0


def expire_stale(kind, *, timeout_s: float) -> int:
    """queued старші за тайм-аут процесу (їх уже не візьмуть) — skipped, running без
    живого процесу понад тайм-аут — failed; target стирається (рецензія E14, 08.10:
    інакше адреса запиту лишалась у ops.db назавжди)."""
    now = ops._now()
    cutoff = now - timedelta(seconds=timeout_s)
    ops.init_ops()
    with ops.ops_session() as s:
        n = s.execute(update(ops.LookupCheck)
                      .where(_kind_is(kind), ops.LookupCheck.state == "queued",
                             ops.LookupCheck.created_at < cutoff)
                      .values(state="skipped", finished_at=now, target=None,
                              message="перевірка застаріла в черзі")).rowcount or 0
        n += s.execute(update(ops.LookupCheck)
                       .where(_kind_is(kind), ops.LookupCheck.state == "running",
                              func.coalesce(ops.LookupCheck.started_at,
                                            ops.LookupCheck.created_at) < cutoff)
                       .values(state="failed", finished_at=now, target=None,
                               message="процес перевірки не дожив")).rowcount or 0
        return n


def result_of(job: ops.LookupCheck) -> dict:
    try:
        return json.loads(job.result) if job.result else {}
    except ValueError:
        return {}


def changed_visibility(result: dict) -> bool:
    """Чи змінила перевірка те, що бачить список: знято, повернуто або додано хоч одне."""
    return bool((result.get("delisted") or 0) + (result.get("restored") or 0)
                + (result.get("inserted") or 0))
