"""Регулярний цикл збору з жорсткими лімітами часу.

Цикл = збір по кожному джерелу → різниця списків → перевірка актуальності →
дублі → контроль якості → готовність аналітики. Кожен крок іде ОКРЕМИМ
процесом у власній групі процесів, і кожен має стелю часу:

  * джерело не вклалось у свій ліміт — його процес вбивається разом із
    браузером, прогін джерела закривається як «тайм-аут», цикл іде далі;
  * увесь цикл не вклався у RUN_TIMEOUT — поточний крок вбивається, решта
    пропускається, цикл закривається як «тайм-аут». Наступний стартує за
    розкладом.

Окремий процес — не прикраса. Потік не можна вбити ззовні, а завислий виклик
у бібліотеці (саме так стояв OLX 9 днів) не реагує ні на що, крім смерті
процесу. Ліміт, який не може вбити, — це побажання, а не механізм.

Успішним вважається цикл, який ЗІБРАВ хоч щось. Живий процес, що нічого не
приніс, — теж аварія: саме на цьому тримається сигнал тиші.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import ops
from .config import DATA_DIR, ROOT, RUN_TIMEOUT, enabled_sources, source_timeout

log = logging.getLogger(__name__)

LOCK_PATH = DATA_DIR / "cycle.lock"
# Якщо цей файл існує, збір на цій машині вимкнено свідомо: базу перенесено
# на іншу машину, і дві машини не мають збирати одночасно — інакше історія
# розійдеться на дві версії.
DISABLED_FLAG = DATA_DIR / "COLLECTOR_OFF"

# Стелі службових кроків. Звичайна тривалість — хвилини (заміряно 09.09):
# перелік ~1 хв, перевірка ~7 хв, дублі ~1 хв.
TASK_TIMEOUTS = {
    "snapshot": 20 * 60,
    "verify": 30 * 60,
    "dedup": 15 * 60,
    "quality": 20 * 60,
    "forecast": 5 * 60,
    "backup": 15 * 60,
}
KILL_GRACE = 15          # секунд між SIGTERM і SIGKILL


@dataclass
class Step:
    name: str
    argv: list[str]
    timeout: float
    kind: str = "task"             # source | task
    source: str | None = None


@dataclass
class StepResult:
    name: str
    status: str                    # ok | failed | timeout | skipped
    seconds: float = 0.0
    code: int | None = None
    note: str | None = None


@dataclass
class CycleResult:
    status: str                    # ok | partial | failed | timeout | skipped | disabled
    started_at: str = ""
    seconds: float = 0.0
    kept: int = 0
    inserted: int = 0
    steps: list[StepResult] = field(default_factory=list)
    message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in ("ok", "partial")


def _cli(*args: str) -> list[str]:
    return [sys.executable, str(ROOT / "cli.py"), *args]


def _quality_routine(today: datetime) -> str:
    """Щодня — легка рутина, у понеділок — глибша, першого числа — повна."""
    if today.day == 1:
        return "monthly"
    if today.isoweekday() == 1:
        return "weekly"
    return "daily"


def default_steps(trigger: str = "schedule", sources: list[str] | None = None,
                  today: datetime | None = None, tasks: bool = True) -> list[Step]:
    steps = [
        Step(f"збір: {name}",
             _cli("scrape", "--sources", name, "--trigger", trigger),
             source_timeout(name), kind="source", source=name)
        for name in (sources or enabled_sources())
    ]
    if not tasks:
        return steps
    routine = _quality_routine(today or datetime.now())
    steps += [
        Step("різниця списків", _cli("snapshot"), TASK_TIMEOUTS["snapshot"]),
        Step("перевірка актуальності", _cli("verify"), TASK_TIMEOUTS["verify"]),
        Step("дублі", _cli("dedup"), TASK_TIMEOUTS["dedup"]),
        Step(f"контроль якості ({routine})", _cli("quality", routine),
             TASK_TIMEOUTS["quality"]),
        Step("готовність аналітики", _cli("analytics", "forecast"),
             TASK_TIMEOUTS["forecast"]),
        # Раз на добу: крок сам вирішує, чи настав час (--if-due).
        Step("бекап", _cli("backup", "--if-due"), TASK_TIMEOUTS["backup"]),
    ]
    return steps


# --- Процеси ---------------------------------------------------------------------


def _kill_group(proc: subprocess.Popen) -> None:
    """Зупиняє крок разом з усім, що він породив (драйвер, браузер).

    Спершу просимо (SIGTERM), потім примушуємо (SIGKILL). Браузер Playwright
    запускається у власній групі, тому його добиваємо ще й по дереву нащадків.
    """
    from .fetcher import _descendants

    kids = _descendants(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    for pid in (proc.pid, *kids):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.error("процес %s не завершився навіть після SIGKILL", proc.pid)


def run_step(step: Step, budget: float) -> tuple[StepResult, int | None]:
    """Виконує крок. Повертає результат і PID дочірнього процесу."""
    limit = min(step.timeout, budget)
    started = time.monotonic()
    log.info("▶ %s (ліміт %.0f хв)", step.name, limit / 60)
    try:
        proc = subprocess.Popen(step.argv, cwd=ROOT, start_new_session=True)
    except OSError as e:
        return StepResult(step.name, "failed", 0.0, None, f"не запустився: {e}"), None
    try:
        code = proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        took = time.monotonic() - started
        why = ("вичерпано час усього циклу" if budget < step.timeout
               else f"не вклався у {limit / 60:.0f} хв")
        log.error("✖ %s: %s — зупинено через %.0f с", step.name, why, took)
        return StepResult(step.name, "timeout", round(took, 1), None, why), proc.pid
    took = time.monotonic() - started
    status = "ok" if code == 0 else "failed"
    log.info("%s %s: код %s, %.0f с", "✔" if code == 0 else "✖", step.name, code, took)
    return StepResult(step.name, status, round(took, 1), code), proc.pid


# --- Замок -------------------------------------------------------------------------


class CycleLock:
    """Один цикл на машину. `flock` знімається сам, коли процес помирає,
    тож завислий замок після падіння неможливий у принципі."""

    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(json.dumps({"pid": os.getpid(), "started": time.time()}))
        self._fh.flush()
        return True

    def holder(self) -> dict:
        try:
            return json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError):
            return {}

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def _reap_overdue_holder(lock: CycleLock, run_timeout: float) -> bool:
    """Добиває цикл, який тримає замок довше, ніж будь-який цикл має право.

    Власний ліміт циклу мав би закрити його сам. Якщо ні — значить, завис
    сам диригент, і чекати на нього означає повторити 9 днів тиші.
    """
    info = lock.holder()
    pid, started = info.get("pid"), info.get("started")
    if not pid or not started:
        return False
    age = time.time() - float(started)
    if age < run_timeout * 1.5:
        return False
    log.error("попередній цикл (PID %s) триває %.0f хв — понад ліміт, зупиняємо",
              pid, age / 60)
    from .fetcher import kill_tree
    kill_tree(int(pid))
    time.sleep(1)
    return True


# --- Цикл ----------------------------------------------------------------------------


def _collected(child_pids: list[int], since: datetime) -> tuple[int, int]:
    """Скільки зібрано й додано дочірніми процесами збору цього циклу."""
    from sqlalchemy import func, select

    if not child_pids:
        return 0, 0
    ops.init_ops()
    with ops.ops_session() as s:
        kept, inserted = s.execute(
            select(func.coalesce(func.sum(ops.RunRecord.kept), 0),
                   func.coalesce(func.sum(ops.RunRecord.inserted), 0))
            .where(ops.RunRecord.pid.in_(child_pids),
                   ops.RunRecord.started_at >= since)
        ).one()
    return int(kept), int(inserted)


def _close_killed_runs(pid: int, source: str, note: str) -> None:
    """Процес джерела вбито — його запис прогону сам себе вже не закриє."""
    from sqlalchemy import select

    ops.init_ops()
    with ops.ops_session() as s:
        for run in s.scalars(select(ops.RunRecord).where(
                ops.RunRecord.pid == pid, ops.RunRecord.status == "running")):
            run.status = "timeout"
            run.finished_at = ops._now()
            run.message = f"{source}: {note}; процес зупинено"
            run.errors = (run.errors or 0) + 1


def run_cycle(trigger: str = "schedule", sources: list[str] | None = None,
              steps: list[Step] | None = None, run_timeout: float = RUN_TIMEOUT,
              tasks: bool = True,
              lock_path: Path = LOCK_PATH, disabled_flag: Path = DISABLED_FLAG
              ) -> CycleResult:
    started_wall = ops._now()
    started = time.monotonic()
    if disabled_flag.exists():
        reason = disabled_flag.read_text(errors="ignore").strip() or "збір вимкнено"
        log.warning("збір на цій машині вимкнено: %s", reason)
        return CycleResult("disabled", message=reason)

    lock = CycleLock(lock_path)
    if not lock.acquire():
        if _reap_overdue_holder(lock, run_timeout) and lock.acquire():
            log.warning("замок звільнено після примусової зупинки попереднього циклу")
        else:
            holder = lock.holder()
            log.info("попередній цикл (PID %s) ще триває — цей пропускаємо",
                     holder.get("pid"))
            return CycleResult("skipped", message="попередній цикл ще триває")

    cycle_id = ops.start_cycle(trigger, socket.gethostname())
    ops.beat("цикл: старт", busy=True)
    results: list[StepResult] = []
    child_pids: list[int] = []
    timed_out = False
    try:
        for step in steps if steps is not None else default_steps(trigger, sources,
                                                                          tasks=tasks):
            remaining = run_timeout - (time.monotonic() - started)
            if remaining <= 1:
                timed_out = True
                results.append(StepResult(step.name, "skipped",
                                          note="вичерпано час усього циклу"))
                continue
            result, pid = run_step(step, remaining)
            results.append(result)
            if pid and step.kind == "source":
                child_pids.append(pid)
                if result.status == "timeout":
                    _close_killed_runs(pid, step.source or step.name, result.note or "")
            if result.status == "timeout" and remaining < step.timeout:
                timed_out = True
    finally:
        kept, inserted = _collected(child_pids, started_wall)
        sources_run = [r for r in results if r.name.startswith("збір")]
        broken = [r for r in sources_run if r.status != "ok"]
        if timed_out:
            status = "partial" if kept else "timeout"
        elif not kept:
            status = "failed"
        elif broken:
            status = "partial"
        else:
            status = "ok"
        message = None
        if not kept:
            message = "за цикл не зібрано жодного оголошення"
        if broken:
            message = ((message + "; ") if message else "") + "проблемні кроки: " + ", ".join(
                f"{r.name} ({r.status})" for r in broken)
        result = CycleResult(status, started_wall.isoformat(timespec="seconds"),
                             round(time.monotonic() - started, 1), kept, inserted,
                             results, message)
        ops.finish_cycle(cycle_id, status=status, kept=kept, inserted=inserted,
                         sources_ok=len(sources_run) - len(broken),
                         sources_failed=len(broken), message=message,
                         steps=json.dumps([asdict(r) for r in results], ensure_ascii=False))
        ops.beat(f"цикл: {status}", busy=False)
        lock.release()
    return result


def render(result: CycleResult) -> str:
    marks = {"ok": "✔", "failed": "✖", "timeout": "⏱", "skipped": "·"}
    lines = ["", "=" * 62, f"ЦИКЛ: {result.status.upper()}  ({result.seconds / 60:.1f} хв)",
             "=" * 62]
    for r in result.steps:
        tail = f"  — {r.note}" if r.note else ""
        lines.append(f"  {marks.get(r.status, '?')} {r.name:<32} {r.seconds:>6.0f} с{tail}")
    lines += ["-" * 62, f"  зібрано: {result.kept}   нових: {result.inserted}"]
    if result.message:
        lines.append(f"  {result.message}")
    lines.append("=" * 62)
    return "\n".join(lines)
