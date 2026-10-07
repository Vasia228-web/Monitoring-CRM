"""Перевірка при відкритті квартири — завданням у черзі, без мережі в сайті (Блок 2, E5).

Відкриття картки (без verify=0) досі запускало HEAD-запити до джерел прямо в
запиті й писало результат у базу: сторінка чекала мережі (до десятків секунд) і
блокування запису. Тепер запит лише кладе номер квартири в пам'ять
(`request`, без жодного вводу-виводу), а фоновий потік сайту:

  1. дивиться, чи є що перевіряти (актуальні оголошення, яких не пробували
     менше ніж `run.opened_recheck_minutes` тому — config/liveness.toml);
  2. чи немає вже завдання для цієї квартири (повторні відкриття об'єднуються);
  3. ставить завдання `opened` у ops.lookup_checks — `deferred`, якщо замок
     циклу зайнятий (перевірка після циклу, інтеграція, конфлікт 5), інакше
     `queued` — і запускає процес перевірки (`systemctl --user start
     realty-lookup@N` під systemd, інакше — `cli.py lookup check --job N`
     окремим процесом), але ЛИШЕ якщо жоден процес перевірки ще не працює й не
     стартує: той, що працює, «доїдає» чергу сам (lookup/opened.run). Сам сайт у
     мережу не ходить.

Поки є незавершені завдання, потік раз на `generations.poll_s` дивиться на них:
завдання, що чекає, а процесу немає (цикл скінчився, процес не вклався в
`drain_budget_s` чи впав на старті), — запуск; завершене, що зняло чи
повернуло оголошення, — кеші списку скидаються (speedcache.owner_changed), а
рядок квартири в знімку «Аналітики» оновлюється точково.

Сторінка питає GET /api/property/{id}/liveness?wait=… (long-poll ≤
`open_check.wait_max_s`) і показує банер, лише якщо щось змінилось («N оголошень
знято — оновити сторінку») або перевірку відкладено до кінця циклу збору.
Маршрут — для обох ролей, як і сама сторінка.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict

import anyio
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import configfiles, ops
from ..config import ROOT
from ..lookup import opened, queue
from ..lookup.opened import candidates, recheck_minutes

log = logging.getLogger(__name__)
router = APIRouter()

UNIT = "realty-lookup@{job}.service"
# Скільки номерів змінених завдань пам'ятати, щоб не скидати кеші двічі за одне
# завдання (long-poll питає щосекунди): двоє людей за годину відкривають
# десятки квартир, 1 024 — із великим запасом і без росту пам'яті.
NOTIFIED_MAX = 1024


def _cfg():
    return configfiles.get("speed").open_check


def _poll_s() -> float:
    try:
        return configfiles.get("speed").generations.poll_s
    except configfiles.ConfigError:
        from ..webcache import EMERGENCY_POLL_S
        return EMERGENCY_POLL_S


# --- Запуск процесу перевірки -------------------------------------------------------------


def _under_systemd() -> bool:
    """Сайт працює службою systemd --user (на Fedora) і є systemctl."""
    return bool(os.environ.get("INVOCATION_ID")) and shutil.which("systemctl") is not None


_children: list[subprocess.Popen] = []


def _reap() -> None:
    """Прибрати завершені дочірні процеси (інакше лишаються зомбі)."""
    for proc in list(_children):
        if proc.poll() is not None:
            _children.remove(proc)


def _low_priority() -> list[str]:
    """Префікс команди: поступитися сайту, як і юніт realty-lookup@ (Nice=10, ionice 7).

    Запасний шлях (без systemd або systemctl не спрацював) інакше лишав би
    процес у групі сайту з його пріоритетом (CPUWeight=1000, ionice 0).
    Утиліти немає (ionice — лише Linux) — без неї.
    """
    prefix = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "10"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c2", "-n7"]
    return prefix


def launch(job_id: int) -> str:
    """Запустити процес перевірки завдання; повертає, як саме запущено.

    Під systemd — шаблон realty-lookup@ (свої ліміти пам'яті й пріоритети,
    deploy/fedora/systemd/realty-lookup@.service), сайт не стає батьком
    процесу. Якщо systemctl не спрацював (юніт не встановлено) — звичайний
    дочірній процес із нижчим пріоритетом, щоб перевірка не зникла (запис у журналі).
    """
    mode = _cfg().launcher
    _reap()
    if mode in ("auto", "systemd") and _under_systemd():
        try:
            r = subprocess.run(["systemctl", "--user", "start", "--no-block",
                                UNIT.format(job=int(job_id))],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return "systemd"
            log.error("realty-lookup@%s не запустився (%s) — запускаю окремим процесом",
                      job_id, (r.stderr or r.stdout).strip()[:200])
        except (OSError, subprocess.SubprocessError) as e:
            log.error("systemctl недоступний (%s) — запускаю окремим процесом", e)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    with (logs / "lookup.log").open("a", encoding="utf-8") as out:
        proc = subprocess.Popen(
            [*_low_priority(), sys.executable, str(ROOT / "cli.py"), "lookup", "check",
             "--job", str(int(job_id))],
            cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
    _children.append(proc)
    return "subprocess"


# --- Диспетчер ------------------------------------------------------------------------------


class LiveCheck:
    """Запити «перевір цю квартиру» від сторінок → завдання в черзі (фоновий потік)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[int, float] = {}       # квартира → коли попросили
        self._jobs: dict[int, tuple[int | None, str, float]] = {}   # квартира → (завдання, стан, коли)
        self._watch: dict[int, int] = {}           # незавершене завдання → квартира
        self._scan = True                          # перший оберт: завдання, що лишились до старту
        self._launched: tuple[int, float] | None = None   # (завдання, коли) останнього запуску
        self._launch_lock = threading.Lock()       # рішення «запускати?» — одне за раз
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._notified: OrderedDict[int, None] = OrderedDict()
        self.dispatched = 0
        self.deferred = 0
        self.launched = 0
        self.skipped = 0

    # --- з обробника запиту (без вводу-виводу) ---------------------------------------------

    def start(self) -> None:
        """Запустити фоновий потік (старт сайту або перший запит)."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="realty-livecheck",
                                                daemon=True)
                self._thread.start()

    def request(self, property_id: int) -> None:
        with self._lock:
            self._pending[int(property_id)] = time.monotonic()
            self._idle.clear()
        self.start()
        self._wake.set()

    def join(self, timeout: float | None = 10) -> bool:
        """Дочекатися, поки всі запити розіслано (тести)."""
        return self._idle.wait(timeout)

    def reset(self) -> None:
        """Забути стан сторінок і завдань (тести: у кожного своя ops.db)."""
        with self._lock:
            self._pending.clear()
            self._jobs.clear()
            self._watch.clear()
            self._notified.clear()
            self._scan = False
            self._launched = None
            self._idle.set()

    # --- фоновий потік --------------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            # Чекаємо запиту; поки є незавершені завдання — ще й раз на poll_s.
            with self._lock:
                busy = bool(self._watch) or self._scan
            self._wake.wait(timeout=_poll_s() if busy else None)
            self._wake.clear()
            self.tick()

    def tick(self) -> None:
        """Один оберт потоку: розіслати запити, оглянути завдання (його ж викликають тести)."""
        while True:
            with self._lock:
                if not self._pending:
                    break
                pid = next(iter(self._pending))
                self._pending.pop(pid)
            try:
                self._dispatch(pid)
            except Exception as e:              # noqa: BLE001 — фон не має падати
                log.warning("перевірку квартири %s не поставлено: %s", pid, e)
                with self._lock:
                    self._jobs[pid] = (None, "failed", time.monotonic())
        try:
            self._review()
        except Exception as e:                  # noqa: BLE001
            log.warning("огляд черги перевірок не вдався: %s", e)
        self._prune()
        _reap()
        with self._lock:
            if not self._pending:
                self._idle.set()

    def _dispatch(self, property_id: int) -> None:
        from .app import SessionLocal          # той самий фабрикант сесій, що й у сторінок
        from .. import verify

        cfg = _cfg()
        key = queue.opened_key(property_id)
        if not cfg.enabled:
            return
        if opened.collector_off():
            # Збір на цій машині вимкнено (COLLECTOR_OFF) — і перевірки теж.
            with self._lock:
                self._jobs[property_id] = (None, "skipped", time.monotonic())
                self.skipped += 1
            return
        existing = queue.active_for(key, timeout_s=cfg.job_timeout_s,
                                    deferred_max_age_s=opened.DEFERRED_MAX_AGE_S)
        if existing is not None:
            with self._lock:
                self._jobs[property_id] = (existing.id, existing.state, time.monotonic())
                self._watch[existing.id] = property_id
            return
        with SessionLocal() as s:
            ids = candidates(s, property_id, now=verify._now(),
                             min_interval_min=recheck_minutes())
        if not ids:
            # Нічого перевіряти: усе вже пробували недавно або немає що перевірити.
            # У базу нічого не пишемо — лише відповідь для сторінки.
            with self._lock:
                self._jobs[property_id] = (None, "skipped", time.monotonic())
                self.skipped += 1
            return
        # Замок циклу зайнятий — завдання одразу відкладене: ні процесу, ні мережі
        # під час циклу (сам процес теж перевіряє це перед запитами).
        state = "deferred" if opened.cycle_busy() is not None else "queued"
        job_id = queue.enqueue(queue.KIND_OPENED, key, property_id, state=state)
        with self._lock:
            self._jobs[property_id] = (job_id, state, time.monotonic())
            self._watch[job_id] = property_id
            self.dispatched += 1
            if state == "deferred":
                self.deferred += 1
        log.info("перевірка при відкритті: квартира %s, завдання %s (%s)", property_id,
                 job_id, state)
        if state == "queued":
            self._maybe_launch(job_id)

    def _maybe_launch(self, job_id: int) -> bool:
        """Запустити процес перевірки, якщо жоден не працює й не стартує саме зараз.

        Один процес «доїдає» всю чергу (lookup/opened.run, замок lookup.lock), тож
        другий потрібен, лише коли першого немає. Замків сайт не бере — лише
        дивиться (runner.lock_busy): узяти замок черги навіть на мить означало б,
        що процес, який саме стартує, вирішить, ніби черга вже чиясь.
        """
        with self._launch_lock:
            if opened.cycle_busy() is not None or opened.drainer_busy() is not None:
                return False
            now = time.monotonic()
            with self._lock:
                last = self._launched
            if last is not None and now - last[1] < _cfg().launch_grace_s:
                # Попередній процес ще стартує (його завдання ніхто не взяв) — він
                # візьме й це. Узяв і вже вийшов — новий запуск без очікування.
                prev = queue.get(last[0])
                if prev is not None and prev.state in queue.RUNNABLE:
                    return False
            with self._lock:
                self._launched = (int(job_id), now)
            how = launch(job_id)
            with self._lock:
                self.launched += 1
        log.info("процес перевірки запущено для завдання %s (%s)", job_id, how)
        return True

    def _review(self) -> None:
        """Незавершені завдання: завершені — сповістити, ті, що чекають, — запустити."""
        with self._lock:
            scan = self._scan
            self._scan = False
        if scan:
            # Перший оберт після старту сайту: завдання, поставлені до перезапуску
            # (відкладені на цикл чи не взяті), — під той самий нагляд.
            cfg = _cfg()
            leftovers = queue.unfinished(queue.KIND_OPENED, timeout_s=cfg.job_timeout_s,
                                         deferred_max_age_s=opened.DEFERRED_MAX_AGE_S)
            with self._lock:
                for job_id, pid in leftovers:
                    self._watch.setdefault(job_id, pid)
        with self._lock:
            watch = dict(self._watch)
        for job_id, pid in watch.items():
            job = queue.get(job_id)
            if job is None or job.state in queue.FINAL or self._expired(job):
                with self._lock:
                    self._watch.pop(job_id, None)
                if job is not None and job.state == "done" and \
                        queue.changed_visibility(queue.result_of(job)):
                    self._changed(job_id, pid)
        with self._lock:
            pending = bool(self._watch)
        if not pending:
            return
        cfg = _cfg()
        if opened.collector_off() or opened.cycle_busy() is not None:
            return
        nxt = queue.next_runnable(queue.KIND_OPENED, timeout_s=cfg.job_timeout_s,
                                  deferred_max_age_s=opened.DEFERRED_MAX_AGE_S,
                                  include_deferred=True)
        if nxt is not None:
            self._maybe_launch(nxt)

    @staticmethod
    def _expired(job) -> bool:
        """Завдання, якого вже ніхто не виконає (процес не дожив) або не чекає.

        Виконання рахується від старту (відкладене могло чекати циклу годину),
        очікування — від постановки.
        """
        since = job.started_at if job.state == "running" and job.started_at else job.created_at
        if since is None:
            return False
        age = (ops._now() - since).total_seconds()
        limit = opened.DEFERRED_MAX_AGE_S if job.state == "deferred" else _cfg().job_timeout_s
        return age > limit

    def _prune(self) -> None:
        """Записи сторінок старші за тайм-аут завдання — більше не потрібні."""
        try:
            limit = _cfg().job_timeout_s
        except configfiles.ConfigError:
            return
        now = time.monotonic()
        with self._lock:
            for pid, (job_id, state, at) in list(self._jobs.items()):
                horizon = opened.DEFERRED_MAX_AGE_S if state == "deferred" else limit
                if now - at > horizon and job_id not in self._watch:
                    del self._jobs[pid]

    # --- стан для сторінки ----------------------------------------------------------------

    def status(self, property_id: int) -> dict:
        """Стан перевірки квартири для long-poll (може читати ops.db — з потоку)."""
        pid = int(property_id)
        with self._lock:
            if pid in self._pending:
                return {"state": "pending"}
            known = self._jobs.get(pid)
        cfg = _cfg()
        job = None
        if known is not None:
            job_id, state, _at = known
            if job_id is None:
                return {"state": state}
            job = queue.get(job_id)
        if job is None:
            # Сторінку відкрили до перезапуску сайту або в іншій вкладці.
            job = queue.latest_for(queue.opened_key(pid),
                                   since_s=max(cfg.job_timeout_s, opened.DEFERRED_MAX_AGE_S))
        if job is None:
            return {"state": "none"}
        state = job.state
        if state in ("queued", "running", "deferred") and self._expired(job):
            state = "failed"                  # процес не дожив — більше не чекаємо
        out = {"state": state, "job": job.id}
        if state in queue.FINAL:
            result = queue.result_of(job)
            out.update({k: result.get(k, 0) for k in ("checked", "delisted", "restored",
                                                      "unknown")})
            out["finished_at"] = ops.as_utc_iso(job.finished_at)
            if state == "done" and queue.changed_visibility(result):
                self._changed(job.id, pid)
        return out

    def _changed(self, job_id: int, property_id: int | None) -> None:
        """Перевірка змінила актуальність — кеші списку й рядок квартири застаріли вже зараз.

        Список, лічильник і зведення — owner_changed (наступне відкриття бачить
        зміну). Рядок квартири в знімку «Аналітики» (днів на ринку, «ще в
        продажу», ліквідність) — точкове оновлення тим самим кодом і з тим самим
        «зараз», що й повна перебудова (cache.patch_properties): інакше сторінка
        квартири після «оновити» показувала б рядки «знято» поруч зі старими
        блоками до наступного покоління «analytics» (до кінця циклу, ~3 год).
        """
        with self._lock:
            if job_id in self._notified:
                return
            self._notified[job_id] = None
            while len(self._notified) > NOTIFIED_MAX:
                self._notified.popitem(last=False)
        from . import speedcache
        speedcache.owner_changed("liveness")
        if property_id is None:
            return
        try:
            from ..analytics import cache
            from .app import SessionLocal
            with SessionLocal() as s:
                cache.patch_properties(s, [int(property_id)], rebuild=False)
        except Exception as e:                  # noqa: BLE001 — лише знімок; список уже скинуто
            log.warning("рядок квартири %s у знімку «Аналітики» не оновлено: %s",
                        property_id, e)


LIVE = LiveCheck()


def request(property_id: int) -> dict | None:
    """Для сторінки квартири: поставити перевірку (нічого не чекає).

    Повертає налаштування для блоку очікування на сторінці або None, якщо
    перевірку вимкнено (аварійний вимикач `open_check.enabled`).
    """
    try:
        cfg = _cfg()
    except configfiles.ConfigError as e:
        log.error("config/speed.toml не читається — перевірка при відкритті вимкнена: %s", e)
        return None
    if not cfg.enabled:
        return None
    LIVE.request(property_id)
    return {"wait_s": cfg.wait_max_s, "poll_ms": int(cfg.poll_s * 1000)}


@router.get("/api/property/{property_id}/liveness")
async def api_liveness(property_id: int, wait: float = Query(0.0, ge=0)):
    """Стан перевірки при відкритті; `wait` — скільки секунд чекати кінця (long-poll).

    Чекання — асинхронне (event loop вільний), читання ops.db — у потоці.
    `deferred` (замок циклу зайнятий) — теж кінець очікування: перевірка буде
    після циклу, а сторінка показує, що її відкладено.
    """
    cfg = _cfg()
    deadline = time.monotonic() + min(wait, cfg.wait_max_s)
    while True:
        status = await anyio.to_thread.run_sync(LIVE.status, property_id)
        if status["state"] not in ("pending", "queued", "running") \
                or time.monotonic() >= deadline:
            return JSONResponse(status)
        await anyio.sleep(min(cfg.poll_s, max(0.0, deadline - time.monotonic())))
