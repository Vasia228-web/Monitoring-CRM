"""Телеметрія роботи системи: прогони, запити, серцебиття воркера.

Свідомо окрема база (`data/ops.db`): дані спостереження за краулером не мають
змішуватись із самими оголошеннями. Основна база лишається чистою, її можна
перебудувати чи перенести, не втрачаючи історії роботи — і навпаки.
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, Integer, String, Text, create_engine, func, inspect,
    select, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import DATA_DIR

OPS_DB_URL = os.getenv("OPS_DB_URL", f"sqlite:///{DATA_DIR / 'ops.db'}")
# Після скількох хвилин мовчання воркер вважається таким, що впав.
IDLE_AFTER_MIN = int(os.getenv("WORKER_IDLE_MIN", "15"))
DOWN_AFTER_MIN = int(os.getenv("WORKER_DOWN_MIN", "240"))
# Скільки прогін може тривати, перш ніж вважати його покинутим. Повний збір
# одного джерела законно триває годинами, звичайний — хвилини.
STALE_AFTER_MIN = {"fresh": 90, "full": 600}


class OpsBase(DeclarativeBase):
    pass


def _now() -> datetime:
    """UTC без зони — так пишемо в базу, щоб порівняння були однорідні."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc_iso(value: datetime | None) -> str | None:
    """Мітка часу з явною зоною.

    У базі час зберігається в UTC без зони. Якщо віддати його так само, браузер
    прочитає рядок як місцевий і покаже похибку в кілька годин — на дашборді
    моніторингу це просто вводить в оману.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


class RunRecord(OpsBase):
    """Один прогін збору — по одному джерелу або по всіх одразу."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="fresh")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")   # schedule|manual|cli
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    pages: Mapped[int] = mapped_column(Integer, default=0)
    kept: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)

    requests_ok: Mapped[int] = mapped_column(Integer, default=0)
    requests_failed: Mapped[int] = mapped_column(Integer, default=0)
    requests_blocked: Mapped[int] = mapped_column(Integer, default=0)

    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    llm_in_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_out_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Контроль якості цього прогону.
    q_accepted: Mapped[int] = mapped_column(Integer, default=0)
    q_review: Mapped[int] = mapped_column(Integer, default=0)
    q_rejected: Mapped[int] = mapped_column(Integer, default=0)
    llm_passed: Mapped[int] = mapped_column(Integer, default=0)
    llm_failed: Mapped[int] = mapped_column(Integer, default=0)

    pid: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)


class Heartbeat(OpsBase):
    """Ознака життя воркера. Рядок один, оновлюється на місці."""

    __tablename__ = "heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    beat_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    counter: Mapped[int] = mapped_column(Integer, default=0)
    # Контроль якості цього прогону.
    q_accepted: Mapped[int] = mapped_column(Integer, default=0)
    q_review: Mapped[int] = mapped_column(Integer, default=0)
    q_rejected: Mapped[int] = mapped_column(Integer, default=0)
    llm_passed: Mapped[int] = mapped_column(Integer, default=0)
    llm_failed: Mapped[int] = mapped_column(Integer, default=0)

    pid: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(200))
    busy: Mapped[bool] = mapped_column(Boolean, default=False)


engine = create_engine(OPS_DB_URL, future=True)
OpsSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_ops() -> None:
    OpsBase.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Доливає нові колонки в уже створені таблиці телеметрії.

    `create_all` наявних таблиць не чіпає, тож без цього нове поле призводить
    до «no such column» на робочій базі.
    """
    insp = inspect(engine)
    for table in OpsBase.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = col.type.compile(engine.dialect)
            default = " DEFAULT 0" if ddl.upper().startswith(("BOOL", "INT", "FLOAT")) else ""
            with engine.begin() as conn:
                conn.execute(text(
                    f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}{default}'
                ))


@contextmanager
def ops_session():
    s = OpsSession()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# --- Лічильники запитів -------------------------------------------------------
# Мережевий шар не знає про базу; він лише інкрементує лічильники в пам'яті,
# а знімок потрапляє в запис прогону наприкінці.

_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "failed": 0, "blocked": 0})
_lock = threading.Lock()


def record_request(source: str | None, ok: bool, blocked: bool = False) -> None:
    with _lock:
        bucket = _counts[source or "?"]
        if ok:
            bucket["ok"] += 1
        else:
            bucket["failed"] += 1
            if blocked:
                bucket["blocked"] += 1


def take_counts(source: str | None = None) -> dict[str, int]:
    """Знімає й обнуляє лічильники (для одного джерела або для всіх)."""
    with _lock:
        if source is not None:
            return dict(_counts.pop(source, {"ok": 0, "failed": 0, "blocked": 0}))
        total = {"ok": 0, "failed": 0, "blocked": 0}
        for bucket in _counts.values():
            for k in total:
                total[k] += bucket[k]
        _counts.clear()
        return total


# --- Прогони ------------------------------------------------------------------


def start_run(source: str, mode: str = "fresh", trigger: str = "manual") -> int:
    init_ops()
    with ops_session() as s:
        run = RunRecord(source=source, mode=mode, trigger=trigger, status="running",
                        pid=os.getpid())
        s.add(run)
        s.flush()
        return run.id


def finish_run(run_id: int, status: str = "ok", message: str | None = None, **fields) -> None:
    with ops_session() as s:
        run = s.get(RunRecord, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = _now()
        run.message = (message or "")[:2000] or None
        for key, value in fields.items():
            if hasattr(run, key) and value is not None:
                setattr(run, key, value)


def beat(note: str | None = None, busy: bool | None = None) -> None:
    """Позначає, що воркер живий."""
    init_ops()
    with ops_session() as s:
        hb = s.get(Heartbeat, 1)
        if hb is None:
            hb = Heartbeat(id=1, counter=0)
            s.add(hb)
        hb.beat_at = _now()
        hb.counter += 1
        hb.pid = os.getpid()
        if note is not None:
            hb.note = note[:200]
        if busy is not None:
            hb.busy = busy


# --- Зведення для дашборда ----------------------------------------------------


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)          # сигнал 0 нічого не робить, лише перевіряє
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # процес є, просто чужий
    return True


def reap_stale_runs() -> int:
    """Закриває прогони, які нікуди не ведуть.

    Прогін лишається «виконується», якщо процес упав так, що не встиг себе
    закрити — наприклад, від обриву мережі. Без цього дашборд назавжди
    показує хибний зелений сигнал.
    """
    init_ops()
    closed = 0
    with ops_session() as s:
        for run in s.scalars(select(RunRecord).where(RunRecord.status == "running")):
            age_min = (_now() - run.started_at).total_seconds() / 60
            limit = STALE_AFTER_MIN.get(run.mode, 90)
            if _process_alive(run.pid) and age_min < limit:
                continue
            run.status = "failed"
            run.finished_at = _now()
            reason = ("процес завершився, не закривши прогін"
                      if not _process_alive(run.pid)
                      else f"прогін триває понад {limit} хв")
            run.message = (run.message or "") + f" [{reason}]"
            closed += 1
    return closed


def worker_health() -> dict:
    """Active / Idle / Down + коли востаннє подавав ознаки життя."""
    init_ops()
    reap_stale_runs()
    with ops_session() as s:
        hb = s.get(Heartbeat, 1)
        running = s.scalar(
            select(func.count()).select_from(RunRecord).where(RunRecord.status == "running")
        ) or 0
        last_run = s.scalar(select(func.max(RunRecord.finished_at)))

    if hb is None:
        return {"state": "down", "beat_at": None, "age_min": None, "counter": 0,
                "running": running, "last_run": last_run, "pid": None,
                "alert": "воркер жодного разу не подавав ознак життя"}

    age = (_now() - hb.beat_at).total_seconds() / 60
    # `busy` без свіжого сигналу означає, що воркер помер посеред роботи,
    # а не що він працює.
    busy = hb.busy and age <= IDLE_AFTER_MIN
    if running or busy:
        state = "active"
    elif age <= IDLE_AFTER_MIN:
        state = "idle"
    elif age <= DOWN_AFTER_MIN:
        state = "idle"
    else:
        state = "down"
    alert = None
    if state == "down":
        alert = f"немає сигналу {int(age)} хв — перевірте `cli.py schedule status`"
    elif hb.busy and age > IDLE_AFTER_MIN:
        alert = (f"воркер позначений як зайнятий, але мовчить {int(age)} хв — "
                 f"схоже, прогін обірвався")
    return {"state": state, "beat_at": hb.beat_at, "age_min": round(age, 1),
            "counter": hb.counter, "pid": hb.pid, "running": running,
            "last_run": last_run, "alert": alert}


def source_stats(hours: int = 24) -> dict[str, dict]:
    """Агрегати по джерелах за останні N годин."""
    init_ops()
    since = _now() - timedelta(hours=hours)
    out: dict[str, dict] = {}
    with ops_session() as s:
        rows = s.execute(
            select(
                RunRecord.source,
                func.sum(RunRecord.requests_ok),
                func.sum(RunRecord.requests_failed),
                func.sum(RunRecord.requests_blocked),
                func.sum(RunRecord.new),
                func.sum(RunRecord.errors),
                func.count(),
            ).where(RunRecord.started_at >= since, RunRecord.source != "all")
            .group_by(RunRecord.source)
        ).all()
        for src, ok, failed, blocked, new, errors, runs in rows:
            ok, failed = int(ok or 0), int(failed or 0)
            total = ok + failed
            out[src] = {
                "requests_ok": ok, "requests_failed": failed,
                "requests_blocked": int(blocked or 0),
                "success_rate": round(100 * ok / total, 1) if total else None,
                "new": int(new or 0), "errors": int(errors or 0), "runs": runs,
            }
        last = s.execute(
            select(RunRecord.source, func.max(RunRecord.finished_at))
            .where(RunRecord.status == "ok", RunRecord.source != "all")
            .group_by(RunRecord.source)
        ).all()
    for src, ts in last:
        out.setdefault(src, {})["last_success"] = ts
    return out


def llm_totals(hours: int | None = None) -> dict:
    init_ops()
    stmt = select(
        func.sum(RunRecord.llm_calls), func.sum(RunRecord.llm_in_tokens),
        func.sum(RunRecord.llm_out_tokens), func.sum(RunRecord.llm_cost_usd),
        func.sum(RunRecord.llm_passed), func.sum(RunRecord.llm_failed),
    )
    if hours:
        stmt = stmt.where(RunRecord.started_at >= _now() - timedelta(hours=hours))
    with ops_session() as s:
        calls, tin, tout, cost, passed, failed = s.execute(stmt).one()
    return {"calls": int(calls or 0), "in_tokens": int(tin or 0),
            "out_tokens": int(tout or 0), "cost_usd": round(float(cost or 0), 4),
            "passed": int(passed or 0), "failed": int(failed or 0)}


def quality_totals(hours: int = 24) -> dict:
    """Скільки записів прийнято, відхилено й поставлено на перегляд."""
    init_ops()
    since = _now() - timedelta(hours=hours)
    with ops_session() as s:
        rows = s.execute(
            select(RunRecord.source, func.sum(RunRecord.q_accepted),
                   func.sum(RunRecord.q_review), func.sum(RunRecord.q_rejected))
            .where(RunRecord.started_at >= since, RunRecord.source != "all")
            .group_by(RunRecord.source)
        ).all()
    return {src: {"accepted": int(a or 0), "review": int(r or 0), "rejected": int(x or 0)}
            for src, a, r, x in rows}


def recent_runs(limit: int = 12) -> list[dict]:
    init_ops()
    with ops_session() as s:
        runs = s.scalars(
            select(RunRecord).order_by(RunRecord.started_at.desc()).limit(limit)
        ).all()
    return [{
        "id": r.id, "source": r.source, "trigger": r.trigger, "mode": r.mode,
        "status": r.status,
        "started_at": as_utc_iso(r.started_at),
        "finished_at": as_utc_iso(r.finished_at),
        "seconds": round((r.finished_at - r.started_at).total_seconds())
                   if r.finished_at else None,
        "pid": r.pid,
        "pages": r.pages, "new": r.new, "inserted": r.inserted, "updated": r.updated,
        "errors": r.errors,
        "requests_ok": r.requests_ok, "requests_failed": r.requests_failed,
        "requests_blocked": r.requests_blocked,
        "q_accepted": r.q_accepted, "q_review": r.q_review, "q_rejected": r.q_rejected,
        "llm_passed": r.llm_passed, "llm_failed": r.llm_failed,
        "llm_calls": r.llm_calls,
        "llm_cost_usd": round(r.llm_cost_usd, 4), "message": r.message,
    } for r in runs]
