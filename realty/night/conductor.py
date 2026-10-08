"""Нічний диригент `cli.py night` (крок E9, D53; інтеграція, конфлікти 3, 4, 7, 8).

Одне вікно ночі (config/night.toml):
  1. COLLECTOR_OFF — нічого; поза вікном — нічого (код 2).
  2. Замок циклу (runner.CycleLock) з очікуванням: опитування раз на lock.poll_seconds;
     якщо до stop_requests лишилось менше за lock.min_work_minutes — вікно пропускаємо
     (`run_cycle` бере замок без очікування, тож нічні роботи не мають права його
     пересидіти).
  3. Бекап на старті ночі, якщо останній успішний старший за backup.max_age_hours:
     `cli.py backup run` окремим процесом зі стелею (копія, integrity, відновлення,
     поза машину). Не вдався — цієї ночі нічого не пишемо, смуги не стартують;
     тривога в Telegram — сторож (watchdog.check_backup і check_night).
  4. План (plan.build) і смуги — по процесу на хост (lane.py), паралельно між
     хостами, послідовно всередині; темп — policy.pace(mode="night").
  5. Раз на lanes.batch_minutes — нові результати смуг ОДНИМ пакетом: запобіжник
     оцінює пакет як прогін (пул, що сам не набрав fuse.min_checked, — разом із
     перевірками за fuse.window_hours, як малі прогони E8), далі запис ≤
     run.apply_batch_rows рядків на транзакцію (liveness.apply), held.json для смуг,
     покоління кешу «lists». Убитий процес нічого не губить: застосоване — у базі.
  6. stop_requests — смуги самі перестають видавати запити; через
     lanes.kill_grace_seconds — примусова зупинка групи процесів (runner._kill_group);
     останній пакет; покоління «analytics»; замок — звільнено до release_lock.
  7. Уже без замка — зведення для /status (ops.liveness_runs, kind «night»; сайт лише
     читає готовий рядок), строк продажу до/після, рядок ops.night_runs.

E11 (D60): після ярусів Блоку 1 у тих самих смугах — дозбір доказів Блоків 3/4
(night/evidence.py): рендери OLX (вкладки, сторінки деталей), прохід стрічки LUN/flombu
(робота identity), GET rieltor замість HEAD. Вікно, що їх писатиме, бекапиться за
правилом одноразових робіт (onetime_max_age_hours); мітки вкладок OLX — на старті вікна
після бекапу, лише після звірки з чипом; покриття доказами — у запис ночі (evidence).

Під запобіжником нічого не знімаємо й не повертаємо (apply.py) — і вночі смуга навіть
не питає таких ключів. Недоторкане правило — те саме: «знято» лише за явним сигналом.

Рецензія E9 (D53): замок звільняється вчасно й тоді, коли щось зависло (жорстка
зупинка за release_lock − 60 с, останній пакет — лише якщо до межі ≥ 2 хв); процес
перевірки при відкритті, що встиг стартувати до ночі, спершу дочитується (≤ 200 с);
пакет, якому база відповіла «locked», не валить ніч — його результати йдуть у наступний
пакет; зупинка смуги блокуваннями потрапляє в запис ночі одразу; запис ночі
закривається завжди (і при SIGTERM під час очікування замка, і при збої після замка).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from .. import ops
from ..config import DATA_DIR, ROOT
from ..liveness import engine, queue
from ..models import Listing, PriceEvent
from ..runner import DISABLED_FLAG, LOCK_PATH, CycleLock, Step, _cli, _kill_group, run_step
from . import codec, plan as night_plan, windows
from .report import identity_written
from .lane import BLOCK_STOPS, LANE_ENV  # noqa: F401 — LANE_ENV ставить SubprocessLauncher

log = logging.getLogger(__name__)

WORK_DIR = DATA_DIR / "night"
# Жорстка зупинка: за стільки секунд до release_lock диригент, що досі тримає замок,
# перериває все (SIGALRM → LockDeadline): смуги зупинено, замок звільнено. systemd
# TimeoutStartSec рахує від старту служби й ручний запуск о 02:30 не обмежив би.
HARD_STOP_MARGIN_S = 60
# Останній пакет застосування — лише якщо до release_lock ≥ стільки секунд; інакше
# його ключі перепитаємо наступного вікна (вони не позначені як «пробували»).
FINAL_APPLY_MARGIN_S = 120
# Процес перевірки при відкритті (realty-lookup@, TimeoutStartSec 3 хв), що
# стартував ДО того, як ніч узяла замок, — дочекатися його не довше за стільки
# секунд (інакше два потоки на rieltor.ua: 3,0 с кожен — фактично 1,5 с).
DRAIN_WAIT_CAP_S = 200
# Одноразові роботи (M2/M3): вікно з ними бекапиться за backup.onetime_max_age_hours.
ONETIME_TIERS = (queue.TIER_M2_LEGACY404, queue.TIER_M2_RESEEN, queue.TIER_M3_HINTED,
                 queue.TIER_M3_BLIND)


class LockDeadline(BaseException):
    """До release_lock лишилось HARD_STOP_MARGIN_S, а диригент досі тримає замок.

    BaseException (як SystemExit): жоден `except Exception` по дорозі її не ковтає —
    смуги зупиняються, замок звільняється, запис ночі — «failed»."""


def utcnow() -> datetime:
    return queue._now()


def _utc_naive(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


class RealClock:
    """Справжній годинник (epoch для дедлайнів між процесами)."""

    @staticmethod
    def time() -> float:
        return time.time()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


class ProcLane:
    """Смуга — дочірній процес у своїй групі (як крок циклу)."""

    def __init__(self, host: str, proc: subprocess.Popen) -> None:
        self.host = host
        self.proc = proc

    def alive(self) -> bool:
        return self.proc.poll() is None

    def terminate(self) -> None:
        """Лише SIGTERM групі (без очікування) — щоб зупиняти всі смуги разом."""
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self) -> None:
        # SIGTERM групі, KILL_GRACE, SIGKILL — разом з онуками (дозбір identity LUN
        # запускає `cli.py scrape` у власній групі).
        _kill_group(self.proc)

    @property
    def code(self) -> int | None:
        return self.proc.returncode


class SubprocessLauncher:
    def __init__(self, argv=None) -> None:
        self.argv = argv or (lambda plan, out: _cli("night", "lane", "--plan", str(plan),
                                                    "--out", str(out)))

    def start(self, host: str, plan_path: Path, out_path: Path) -> ProcLane:
        proc = subprocess.Popen(self.argv(plan_path, out_path), cwd=ROOT,
                                start_new_session=True, env={**os.environ, LANE_ENV: host})
        return ProcLane(host, proc)


def default_backup(timeout_s: float) -> dict:
    """`cli.py backup run` окремим процесом зі стелею; статус — з ops.backups.

    Свій запис — найперша спроба ПІСЛЯ тієї, що була останньою до старту (а не просто
    найновіша: таймер 04:30 міг почати ще одну — тепер її не пустить backup.lock); якщо
    інший бекап уже йшов, процес дочекається його й віддасть його результат — тоді
    це та сама «остання до старту» спроба, що була «running» (рецензія E9, D53)."""
    from .. import backup

    before = backup.last_attempt()
    wait_min = max(0.0, timeout_s - 60) / 60
    t0 = time.monotonic()
    res, _ = run_step(Step("бекап на старті ночі",
                           _cli("backup", "run", "--wait-minutes", f"{wait_min:.1f}"),
                           timeout_s), budget=timeout_s)
    took = round(time.monotonic() - t0, 1)
    rec = backup.first_attempt_after(before.id if before is not None else 0)
    if rec is None and before is not None and before.status == "running":
        rec = backup.attempt(before.id)
    if rec is None or rec.status == "running":
        return {"status": "failed", "step": res.status, "seconds": took,
                "message": f"бекап не записався ({res.status}, код {res.code})"}
    status = rec.status if res.status == "ok" else "failed"
    return {"status": status, "step": res.status, "seconds": took, "record_id": rec.id,
            "file": rec.file, "restored_ok": bool(rec.restored_ok), "offsite": rec.offsite,
            "message": rec.message}


def default_backup_due(hours: float) -> tuple[bool, str | None]:
    from .. import backup

    last = backup.last_success_at()
    return backup.is_due(hours), (last.isoformat(timespec="seconds") if last else None)


def default_liquidity(session) -> dict:
    from ..liveness import report

    return report.liquidity(session)


@dataclass
class Env:
    """Усе зовнішнє — для тестів підмінне (годинник, смуги, бекап, база)."""

    clock: object = field(default_factory=RealClock)
    launcher: object = field(default_factory=SubprocessLauncher)
    backup: object = default_backup
    backup_due: object = default_backup_due
    liquidity: object = default_liquidity
    lock_path: Path = LOCK_PATH
    disabled_flag: Path = DISABLED_FLAG
    work_dir: Path = WORK_DIR
    scope: object = None
    utcnow: object = utcnow
    drain_lock: Path | None = None          # None — realty.lookup.opened.DRAIN_LOCK

    def session_scope(self):
        if self.scope is None:
            from ..db import session_scope
            return session_scope()
        return self.scope()

    def drain_lock_path(self) -> Path:
        if self.drain_lock is not None:
            return self.drain_lock
        from ..lookup import opened

        return opened.DRAIN_LOCK

    def arm_hard_stop(self, at_epoch: float):
        """Жорстка зупинка о `at_epoch` (SIGALRM → LockDeadline); повертає «зняти».

        Лише зі справжнім годинником і в головному потоці (сигнали — лише там);
        віртуальний час тестів дедлайни перевіряє сам диригент."""
        if not isinstance(self.clock, RealClock) or \
                threading.current_thread() is not threading.main_thread():
            return lambda: None

        def on_alarm(signum, frame):
            raise LockDeadline(f"до звільнення замка циклу лишилось {HARD_STOP_MARGIN_S} с — "
                               f"жорстка зупинка")

        old = signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, max(1.0, at_epoch - time.time()))

        def disarm() -> None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

        return disarm


# --- Запис ночі (ops.night_runs) ---------------------------------------------------------


class Record:
    def __init__(self, now=ops._now, **fields) -> None:
        self.now = now
        self.closed = False
        ops.init_ops()
        with ops.ops_session() as s:
            row = ops.NightRun(status="running", started_at=now(), **fields)
            s.add(row)
            s.flush()
            self.id = row.id

    def update(self, **fields) -> None:
        with ops.ops_session() as s:
            row = s.get(ops.NightRun, self.id)
            for k, v in fields.items():
                setattr(row, k, json.dumps(v, ensure_ascii=False, default=str)
                        if isinstance(v, (dict, list)) else v)

    def finish(self, status: str, **fields) -> None:
        self.update(status=status, finished_at=self.now(), **fields)
        self.closed = True


def record_failure(message: str) -> None:
    """Запис ночі «failed» без диригента: збій до того, як він створив свій (конфіг,
    база) — інакше systemd «failed», а в ops.night_runs нічого, і сторож мовчить."""
    ops.init_ops()
    with ops.ops_session() as s:
        now = ops._now()
        s.add(ops.NightRun(status="failed", started_at=now, finished_at=now,
                           message=message[:500]))


# --- Результати смуг ------------------------------------------------------------------------


class Reader:
    """Нові рядки файла результатів смуги — лише цілі (з \\n у кінці)."""

    def __init__(self, path: Path, items: list) -> None:
        self.path = path
        self.items = items
        self.pos = 0
        self.done: dict | None = None
        self.identity: dict | None = None
        self.evidence: dict | None = None
        self.skipped = 0
        self.bad_lines = 0
        self.buffer: list = []

    def read(self) -> list:
        """Усе нове (і накопичене poll) — і забрати."""
        self.poll()
        out, self.buffer = self.buffer, []
        return out

    def poll(self) -> None:
        """Дочитати нові рядки: результати — у буфер до пакета, «done» — одразу видно
        (зупинку смуги блокуваннями диригент записує, не чекаючи кінця вікна)."""
        self.buffer += self._read_new()

    def _read_new(self) -> list:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except FileNotFoundError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.pos += end + 1
        out = []
        for line in data[:end + 1].decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                kind = rec["t"]
                if kind == "item":
                    out.append(engine.Outcome(self.items[rec["i"]],
                                              codec.verdict_from_json(rec["verdict"]),
                                              datetime.fromisoformat(rec["at"]),
                                              rec.get("capture") or {}))
                elif kind == "skip":
                    self.skipped += 1
                elif kind == "identity":
                    self.identity = rec
                elif kind == "evidence":
                    self.evidence = rec
                elif kind == "done":
                    self.done = rec
            except (ValueError, KeyError, IndexError, TypeError) as e:
                self.bad_lines += 1
                log.error("%s: зіпсований рядок результатів (%s) — пропущено", self.path.name,
                          type(e).__name__)
        return out


def _mem_available_mb() -> int | None:
    """MemAvailable машини (Linux), МБ — у кожен пакет ночі (рецензія E9, D53: місце для
    рендерів OLX Блоків 3/4 під MemoryHigh 1800M)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _add(into: dict, more: dict) -> None:
    for k, v in more.items():
        if isinstance(v, dict):
            _add(into.setdefault(k, {}), v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            into[k] = into.get(k, 0) + v


# --- Диригент -----------------------------------------------------------------------------


class Conductor:
    def __init__(self, *, ncfg=None, nhash: str = "", lcfg=None, lhash: str = "",
                 env: Env | None = None, budget_min: float | None = None) -> None:
        from .. import configfiles
        from ..liveness import policy

        if ncfg is None:
            ncfg, nhash = configfiles.load_with_hash("night")
        if lcfg is None:
            lcfg, lhash = policy.load_with_hash()
        self.ncfg, self.nhash, self.lcfg, self.lhash = ncfg, nhash, lcfg, lhash
        self.env = env or Env()
        self.budget_min = budget_min
        self.lrun_id: int | None = None
        self.batches: list[dict] = []
        self.per_host: dict = {}
        self.per_source: dict = {}
        self.per_tier: dict = {}
        self.signatures: dict = {}
        self.trips: list = []
        self.lanes_info: dict = {}
        self.identity: dict = {}
        # Дозбір доказів Блоків 3/4 (E11, D60): план вікна, звіти смуг, мітки вкладок.
        self.evidence: dict = {}
        self.scfg = self._seller_config()
        self.plan = None
        self.held_path: Path | None = None
        self.rec: Record | None = None
        self.pending: list = []                 # не застосовано через «locked» — у наступний пакет
        self.unapplied = 0                      # так і не застосовано за вікно (перепитаємо)
        self.readers: dict = {}
        self.handles: dict = {}
        self._merged: set[str] = set()
        self._lrun_closed = False

    @staticmethod
    def _seller_config():
        """config/seller.toml; зламаний — ніч іде без дозбору доказів (журнал), а не падає."""
        from .. import configfiles

        try:
            return configfiles.load("seller")
        except configfiles.ConfigError as e:
            log.error("ніч: config/seller.toml не читається — дозбір доказів вимкнено: %s", e)
            return None

    def _plan_evidence(self, s, plan, began: float) -> dict:
        """Робота доказів у плані смуг (HostPlan.evidence); збій — ніч без дозбору."""
        from . import evidence

        if self.scfg is None:
            return {"error": "config/seller.toml не читається", "writes": 0}
        try:
            return evidence.attach(s, self.lcfg, self.ncfg, self.scfg, plan,
                                   now=self.env.utcnow(), night_start=_utc_naive(began))
        except Exception as e:                          # noqa: BLE001 — Блок 1 важливіший
            log.exception("ніч: план дозбору доказів не побудовано")
            for hp in plan.hosts.values():
                hp.evidence = {}
            return {"error": f"{type(e).__name__}: {e}"[:300], "writes": 0}

    def _tab_labels(self) -> dict | None:
        """Мітки вкладок OLX у seller_evidence — після бекапу, лише після звірки з чипом."""
        from . import evidence

        if self.scfg is None or "olx_tabs" not in self.ncfg.jobs.order:
            return None
        try:
            return evidence.apply_tab_labels(self.env.session_scope, self.scfg,
                                             now=self.env.utcnow())
        except Exception as e:                          # noqa: BLE001
            log.exception("ніч: мітки вкладок OLX не записано")
            return {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}

    # --- головне ---------------------------------------------------------------------

    def run(self) -> dict:
        env, c = self.env, self.env.clock
        t0 = c.time()
        win = windows.current(self.ncfg, t0, budget_min=self.budget_min)
        self.rec = rec = Record(now=env.utcnow, config_hash=self.nhash[:16],
                                liveness_hash=self.lhash[:16],
                                fuse_mode=self.lcfg.fuse.mode,
                                window=win.label if win else None,
                                night_date=win.night_date if win else None)
        try:
            return self._run(win)
        except BaseException as e:
            # Запис ночі закривається ЗАВЖДИ — і при SIGTERM під час очікування замка, і
            # при збої після нього (рецензія E9, D53): інакше «running», доки сторож через
            # 2,5 год не вирішить, що диригента вбито.
            msg = f"{type(e).__name__}: {e}"[:500]
            try:
                self._collect_done()
                if not rec.closed:
                    rec.finish("failed", message=msg, lanes=self.lanes_info,
                               batches=self.batches, per_host=self.per_host)
                self._finish_liveness("failed", message=msg)
            except Exception:                           # noqa: BLE001 — первинна помилка важливіша
                log.exception("ніч: запис аварії не вдався")
            raise

    def _run(self, win: windows.Window | None) -> dict:
        env, c, rec = self.env, self.env.clock, self.rec
        if env.disabled_flag.exists():
            reason = env.disabled_flag.read_text(errors="ignore").strip() or "збір вимкнено"
            rec.finish("disabled", message=reason[:300])
            return self._result("disabled", reason)
        if win is None:
            msg = ("поза нічними вікнами (" + ", ".join(
                f"{w.start}–{w.release_lock}" for w in self.ncfg.windows) + ")")
            rec.finish("outside_window", message=msg)
            return self._result("outside_window", msg)
        rec.update(stop_requests_at=_utc_naive(win.stop_requests),
                   release_lock_at=_utc_naive(win.release_lock))
        lock = CycleLock(env.lock_path)
        waited = 0.0
        min_work = self.ncfg.lock.min_work_minutes * 60
        poll = self.ncfg.lock.poll_seconds
        while not lock.acquire():
            if c.time() + poll > win.stop_requests - min_work:
                msg = (f"цикл не звільнив замок ({waited / 60:.0f} хв); до stop_requests "
                       f"лишилось менше за {self.ncfg.lock.min_work_minutes:g} хв")
                rec.finish("lock_timeout", lock_waited_s=waited, message=msg)
                log.info("ніч: %s", msg)
                return self._result("lock_timeout", msg)
            c.sleep(poll)
            waited += poll
        disarm = lambda: None                          # noqa: E731
        try:
            rec.update(lock_acquired_at=env.utcnow(), lock_waited_s=waited)
            log.info("ніч: замок узято (чекали %.0f хв), %s", waited / 60, win.describe())
            disarm = env.arm_hard_stop(win.release_lock - HARD_STOP_MARGIN_S)
            outcome = self._locked(win)
        finally:
            disarm()
            lock.release()
            rec.update(lock_released_at=env.utcnow())
        late = c.time() > win.release_lock
        if late:
            log.error("ніч: замок звільнено ПІЗНІШЕ за release_lock")
        status, message = outcome["status"], outcome.get("message")
        if outcome.get("lanes_ran"):
            try:
                message = "; ".join(filter(None, [message, self._after_release(win)])) or None
            except Exception as e:                      # noqa: BLE001 — запис ночі закриваємо однаково
                log.exception("ніч: збій після звільнення замка")
                message = "; ".join(filter(None, [message, f"після звільнення замка: "
                                                           f"{type(e).__name__}: {e}"]))[:500]
                self._finish_liveness("failed", message=message)
        rec.finish(status, message=message)
        ops.beat(f"ніч {win.label}: {status}", busy=False)
        return self._result(status, message, late=late)

    def _result(self, status: str, message: str | None = None, **extra) -> dict:
        return {"status": status, "message": message, "night_run_id": self.rec.id if self.rec
                else None, "liveness_run_id": self.lrun_id, "batches": len(self.batches),
                "per_host": self.per_host, "lanes": self.lanes_info, "trips": self.trips,
                "unapplied": self.unapplied, **extra}

    def _finish_liveness(self, status: str, **kw) -> None:
        if self.lrun_id is None or self._lrun_closed:
            return
        from ..liveness import service

        service._finish(self.lrun_id, status=status, **kw)
        self._lrun_closed = True

    @staticmethod
    def _totals(s) -> dict:
        """Звірка ночі: актуальних, усіх рядків, подій ціни (рецензія E9, D53)."""
        return {"active": s.scalar(select(func.count()).select_from(Listing)
                                   .where(Listing.is_active.is_(True))),
                "listings": s.scalar(select(func.count()).select_from(Listing)),
                "price_events": s.scalar(select(func.count()).select_from(PriceEvent))}

    def _wait_drainer(self) -> float:
        """Процес перевірки при відкритті, що стартував ДО замка ночі, ще питає сайти —
        дочекатися його (≤ DRAIN_WAIT_CAP_S): одна смуга на хост (рецензія E9, D53).
        Нові такі процеси, побачивши замок циклу, відкладають завдання самі."""
        from .. import runner

        c, path = self.env.clock, self.env.drain_lock_path()
        waited = 0.0
        while runner.lock_busy(path) is not None:
            if waited >= DRAIN_WAIT_CAP_S:
                log.warning("ніч: перевірка при відкритті тримає %s понад %d с — далі не чекаю",
                            path.name, DRAIN_WAIT_CAP_S)
                break
            step = min(self.ncfg.lanes.poll_seconds, DRAIN_WAIT_CAP_S - waited)
            c.sleep(step)
            waited += step
        if waited:
            log.info("ніч: чекали кінця перевірки при відкритті %.0f с", waited)
        return waited

    def _locked(self, win: windows.Window) -> dict:
        env, c, rec = self.env, self.env.clock, self.rec
        min_work = self.ncfg.lock.min_work_minutes * 60
        if c.time() > win.stop_requests - min_work:
            # До бекапу: бекап під замком без жодної смуги лише з'їв би вікно.
            return {"status": "ok", "message": "до stop_requests лишилось менше за "
                                               "lock.min_work_minutes — смуг не запускаю"}
        drain_waited = self._wait_drainer()
        from ..liveness import fuse

        held = fuse.held_sources()
        skip = self._skip_hosts(win)
        began = self._night_began(win)
        with env.session_scope() as s:
            before = self._totals(s)
            self.plan = plan = night_plan.build(
                s, self.lcfg, self.ncfg, now=env.utcnow(), held=held,
                attempted_since=_utc_naive(began), skip_hosts=skip)
            ev_plan = self._plan_evidence(s, plan, began)
        onetime = sum(n for hp in plan.lanes.values() for t, n in hp.tiers.items()
                      if t in ONETIME_TIERS)
        self.evidence = {"plan": ev_plan}
        rec.update(plan={"hosts": plan.as_dict(), "held": sorted(held),
                         "drain_waited_s": drain_waited, "onetime_keys": onetime},
                   active_before=before["active"], totals={"before": before},
                   evidence=self.evidence)
        # Бекап на старті: за правилом max_age_hours, а вікно з одноразовими роботами
        # (M2/M3 — тисячі повернень і знять) — за onetime_max_age_hours (рецензія E9).
        due, last_ok = env.backup_due(self.ncfg.backup.max_age_hours)
        rule = "max_age_hours"
        # Дозбір доказів (E11, D60) теж дописує ключі в непорожні JSON — правило
        # одноразових робіт (свіжий бекап перед зміною непорожніх даних).
        if not due and (onetime or ev_plan.get("writes")):
            due, last_ok = env.backup_due(self.ncfg.backup.onetime_max_age_hours)
            rule = "onetime_max_age_hours"
        if due:
            timeout = min(self.ncfg.backup.timeout_minutes * 60,
                          max(60.0, win.release_lock - c.time() - 120))
            log.info("ніч: бекап на старті (%s; останній успішний — %s)", rule,
                     last_ok or "ніколи")
            b = env.backup(timeout)
            rec.update(backup={**b, "due": True, "rule": rule, "last_ok_before": last_ok})
            if b.get("status") != "ok":
                msg = (f"бекап на старті вікна не вдався ({b.get('status')}: "
                       f"{(b.get('message') or '')[:200]}) — у цьому вікні нічого не пишемо")
                log.error("ніч: %s", msg)
                return {"status": "backup_failed", "message": msg}
        else:
            rec.update(backup={"status": "fresh", "due": False, "last_ok": last_ok,
                               "onetime_keys": onetime})
        if c.time() > win.stop_requests - min_work:
            return {"status": "ok", "message": "до stop_requests лишилось менше за "
                                               "lock.min_work_minutes — смуг не запускаю"}
        labels = (ev_plan.get("tab_labels") or {})
        if labels.get("status") == "calibrated" and labels.get("pending_rows"):
            self.evidence["tab_labels"] = self._tab_labels()
            rec.update(evidence=self.evidence)
        lanes = plan.lanes
        if not lanes:
            return {"status": "ok", "message": "робити нічого: усі ключі мають відповідь, "
                                               "догін не потрібен"}
        from ..liveness import service

        self.lrun_id = service._start("night", self.lhash, self.lcfg.fuse.mode)
        rec.update(liveness_run_id=self.lrun_id)
        work = env.work_dir / str(rec.id)
        self._prune_work(env.work_dir, keep=work)
        work.mkdir(parents=True, exist_ok=True)
        self.held_path = work / "held.json"
        self._write_held(held)
        handles, readers = self.handles, self.readers
        for host, hp in lanes.items():
            spec = self.lane_spec(hp, win)
            plan_path, out_path = work / f"plan-{host}.json", work / f"out-{host}.jsonl"
            plan_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            readers[host] = Reader(out_path, hp.items)
            handles[host] = env.launcher.start(host, plan_path, out_path)
            log.info("ніч: смуга %s — %d ключів × %.1f с ≈ %.0f хв%s", host, hp.requests, hp.pace,
                     hp.seconds / 60, f", дозбір {hp.identity}" if hp.identity else "")
        try:
            if self.ncfg.report.liquidity:
                try:
                    with env.session_scope() as s:
                        rec.update(liquidity_before=env.liquidity(s))
                except Exception as e:                  # noqa: BLE001 — звіт не зупиняє ночі
                    log.warning("строк продажу (до) не пораховано: %s", e)
            self._supervise(handles, readers, win)
        except BaseException:
            # Диригент падає чи його зупиняють (SIGTERM, жорстка зупинка): смуги — окремі
            # процеси у своїх групах; без замка вони не мають питати сайти ні секунди довше.
            self._stop_all(handles)
            raise
        from .. import webcache

        webcache.bump("analytics", f"кінець нічного вікна {win.label}")
        with env.session_scope() as s:
            after = self._totals(s)
        rec.update(active_after=after["active"], totals={"before": before, "after": after},
                   lanes=self.lanes_info, identity=self.identity, evidence=self.evidence)
        partial = any(d.get("stopped") in BLOCK_STOPS or d.get("killed") or d.get("code")
                      for d in self.lanes_info.values())
        shutil.rmtree(work, ignore_errors=True)
        msg = None
        if self.unapplied:
            msg = (f"не застосовано {self.unapplied} результатів (база зайнята чи до межі "
                   f"замка < {FINAL_APPLY_MARGIN_S // 60} хв) — ці ключі перепитаємо")
        return {"status": "partial" if partial else "ok", "lanes_ran": True, "message": msg}

    # --- смуги ---------------------------------------------------------------------

    def lane_spec(self, hp, win: windows.Window) -> dict:
        return {"host": hp.host, "family": self.lcfg.hosts[hp.host].family, "pace": hp.pace,
                "stop_at": win.stop_requests, "held_path": str(self.held_path),
                # Смуга перевіряє, що цей замок тримає її батьківський процес (lane.refusal).
                "lock_path": str(self.env.lock_path),
                "identity": hp.identity,
                # Рендери OLX після Блоку 1 (E11, D60): вкладки й черга сторінок деталей.
                "evidence": hp.evidence.get("spec"),
                "max_consecutive_blocks": self.lcfg.run.max_consecutive_blocks,
                "block_share": self.ncfg.lanes.block_share,
                "block_min_requests": self.ncfg.lanes.block_min_requests,
                "items": [codec.item_to_json(i) for i in hp.items]}

    def _supervise(self, handles: dict, readers: dict, win: windows.Window) -> None:
        c = self.env.clock
        batch_s = self.ncfg.lanes.batch_minutes * 60
        poll = self.ncfg.lanes.poll_seconds
        kill_at = win.stop_requests + self.ncfg.lanes.kill_grace_seconds
        next_batch = c.time() + batch_s
        while True:
            self._collect_done()
            alive = [h for h, lane in handles.items() if lane.alive()]
            now = c.time()
            if not alive:
                break
            if now >= kill_at:
                for h in alive:
                    log.warning("ніч: смуга %s не зупинилась сама — зупиняю примусово", h)
                self._stop_all({h: handles[h] for h in alive})
                break
            if now >= next_batch:
                self._apply_new(readers)
                next_batch = c.time() + batch_s
            c.sleep(max(0.01, min(poll, next_batch - c.time(), kill_at - c.time())))
        self._collect_done()
        if c.time() > win.release_lock - FINAL_APPLY_MARGIN_S:
            # До межі замка < 2 хв: запис пакета (до 30 с на «busy» SQLite) міг би її
            # перейти. Ці ключі не позначені як «пробували» — наступне вікно їх візьме.
            left = len(self.pending) + sum(len(r.read()) for r in readers.values())
            self.pending = []
            self.unapplied += left
            log.error("ніч: до звільнення замка < %d с — останній пакет (%d результатів) не "
                      "застосовую", FINAL_APPLY_MARGIN_S, left)
        else:
            self._apply_new(readers)
            if self.pending:
                self.unapplied += len(self.pending)
                self.pending = []
        for host, r in readers.items():
            self._merge_done(host, r, final=True)

    def _collect_done(self) -> None:
        """Дочитати файли смуг: зупинки (блокування, дедлайн) — у запис ночі одразу, а не
        в кінці вікна (рецензія E9, D53: тривога сторожа й пропуск хоста вікном 2)."""
        changed = False
        for host, r in self.readers.items():
            if host in self._merged:
                continue
            lane = self.handles.get(host)
            dead = lane is not None and not lane.alive()
            r.poll()                                   # після alive: вихід — уже з «done»
            if r.done is not None or dead:
                self._merge_done(host, r)
                self._merged.add(host)
                changed = True
        if changed and self.rec is not None:
            self.rec.update(lanes=self.lanes_info)

    def _merge_done(self, host: str, r: Reader, *, final: bool = False) -> None:
        info = self.lanes_info.setdefault(host, {})
        if final:
            r.poll()
        if r.done is not None:
            info.update({k: v for k, v in r.done.items() if k not in ("t", "host")})
        elif final:
            info.setdefault("stopped", "killed" if info.get("killed") else "no_summary")
        code = getattr(self.handles.get(host), "code", None)
        if code:
            info["code"] = code
        if r.bad_lines:
            info["bad_lines"] = r.bad_lines
        if r.identity is not None:
            self.identity[host] = r.identity
        if r.evidence is not None:
            self.evidence.setdefault("lanes", {})[host] = r.evidence.get("report")

    def _stop_all(self, handles: dict) -> None:
        """Зупинити смуги РАЗОМ: SIGTERM усім групам одразу, далі кожну — з KILL_GRACE
        (послідовно по 15 с на смугу п'ять смуг з'їли б хвилину з межі замка)."""
        alive = {h: lane for h, lane in handles.items() if lane.alive()}
        for lane in alive.values():
            terminate = getattr(lane, "terminate", None)
            if terminate is not None:
                terminate()
        for h, lane in alive.items():
            lane.stop()
            self.lanes_info.setdefault(h, {})["killed"] = True
        self._collect_done()

    def _applied_before(self, name: str, scope: str) -> dict:
        """Скільки вже знято й повернуто цього вікна для джерела/сайту `name`."""
        from ..liveness import fuse

        if scope == fuse.SCOPE_HOST:
            hosts = [h for h, spec in self.lcfg.hosts.items() if spec.family == name]
            rows = [self.per_host.get(h) or {} for h in hosts]
        else:
            rows = [self.per_source.get(name) or {}]
        return {k: sum(int(r.get(k) or 0) for r in rows) for k in ("delisted", "restored")}

    def _apply_new(self, readers: dict) -> None:
        from .. import webcache
        from ..liveness import apply as lv_apply, fuse

        outcomes, self.pending = self.pending, []
        retried = len(outcomes)
        for r in readers.values():
            outcomes += r.read()
        if not outcomes:
            return
        started = time.perf_counter()
        with self.env.session_scope() as s:
            prior = fuse.window_counts(s, self.lcfg,
                                       since=fuse.window_since(self.lcfg, self.env.utcnow()),
                                       cleared=fuse.cleared_at())
        error = None
        try:
            rep = lv_apply.apply_outcomes(outcomes, cfg=self.lcfg, scope=self.env.session_scope,
                                          run_id=self.lrun_id, prior=prior)
        except lv_apply.ApplyInterrupted as e:
            # База зайнята довше за busy_timeout і повтори (інший процес тримав замок
            # запису): записане — у звіті, решта — у наступний пакет, ніч триває.
            rep, self.pending, error = e.rep, e.pending, str(e)
            log.error("ніч: пакет записано не весь (%s) — %d результатів застосую з наступним "
                      "пакетом", error, len(self.pending))
        self._write_held(fuse.held_sources())
        n = len(self.batches) + 1
        at = self.env.utcnow().isoformat(timespec="seconds")
        for t in rep.trips:
            if t.get("new"):
                # «Нічого не знято й не повернуто» — з цього пакета; що вже застосовано
                # раніше в цьому вікні, звіт показує окремо (рецензія E9, D53).
                t.update(batch=n, at=at,
                         before=self._applied_before(t["source"], t.get("scope") or ""))
        webcache.bump("lists", f"ніч: пакет {n}")
        for host, d in rep.by_host.items():
            _add(self.per_host.setdefault(host, {}), d)
        for src, d in rep.by_source.items():
            _add(self.per_source.setdefault(src, {}), d)
        _add(self.per_tier, rep.by_tier)
        _add(self.signatures, rep.by_signature)
        self.trips += rep.trips
        self.batches.append({
            "n": n, "at": at,
            "keys": len(outcomes), "rows": rep.checked, "alive": rep.alive,
            "removed": rep.delisted, "returned": rep.restored, "repaired": rep.repaired,
            "unknown": rep.unknown, "not_found": rep.not_found, "held_rows": rep.held,
            "stale_rows": rep.stale, "captured": rep.captured, "trips": rep.trips,
            "held_sources": rep.held_sources, "transactions": rep.batches,
            "longest_txn_s": rep.longest_batch_s,
            "retried": retried, "pending": len(self.pending), "error": error,
            "mem_available_mb": _mem_available_mb(),
            "seconds": round(time.perf_counter() - started, 3)})
        self.rec.update(batches=self.batches, per_host=self.per_host, per_tier=self.per_tier,
                        fuse={"mode": self.lcfg.fuse.mode, "trips": self.trips,
                              "held": rep.held_sources})
        log.info("ніч: пакет %d — ключів %d, знято %d, повернуто %d, полагоджено %d, "
                 "без висновку %d%s", n, len(outcomes), rep.delisted, rep.restored, rep.repaired,
                 rep.unknown, f"; ЗАПОБІЖНИК: {[t['source'] for t in rep.trips]}"
                 if rep.trips else "")

    def _write_held(self, held) -> None:
        if self.held_path is None:
            return
        tmp = self.held_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"held": sorted(held),
                                   "at": self.env.utcnow().isoformat(timespec="seconds")}))
        tmp.replace(self.held_path)

    @staticmethod
    def _prune_work(root: Path, keep: Path) -> None:
        """Тека попередньої ночі лишається лише після аварії — результати її смуг
        уже в базі або втрачені разом із процесом; файли більше не потрібні."""
        if not root.is_dir():
            return
        for p in root.iterdir():
            if p.is_dir() and p != keep:
                shutil.rmtree(p, ignore_errors=True)

    # --- зупинки хостів (блокування) ----------------------------------------------------

    def _night_began(self, win: windows.Window) -> float:
        """Старт першого вікна цієї ночі (epoch): ключ, який відтоді вже пробували, —
        не питаємо вдруге (інтеграція: «повторних запитів до ключа за ніч немає»)."""
        day = datetime.fromisoformat(win.night_date).date()
        starts = [datetime.combine(day, datetime.min.time()).timestamp() + m[0] * 60
                  for m in (w.minutes() for w in self.ncfg.windows)]
        return min(starts)

    def _skip_hosts(self, win: windows.Window) -> dict[str, str]:
        out: dict[str, str] = {}
        with ops.ops_session() as s:
            for hold in s.scalars(select(ops.NightHold).where(ops.NightHold.state == "held")):
                out[hold.host] = (f"чекає рішення власника: {hold.reason or 'блокування'} "
                                  f"(cli.py night unhold --host {hold.host})")
            runs = s.scalars(select(ops.NightRun).where(
                ops.NightRun.night_date == win.night_date, ops.NightRun.id != self.rec.id,
                ops.NightRun.lanes.isnot(None))).all()
            for run in runs:
                for host, d in json.loads(run.lanes or "{}").items():
                    if d.get("stopped") in BLOCK_STOPS:
                        out.setdefault(host, f"блокування цієї ночі (вікно {run.window}) — "
                                             f"смуга стоїть до кінця ночі")
        return out

    def _open_holds(self, win: windows.Window) -> list[str]:
        """Смугу зупиняли блокування і минулої ночі — хост чекає рішення власника."""
        blocked = [h for h, d in self.lanes_info.items() if d.get("stopped") in BLOCK_STOPS]
        if not blocked:
            return []
        prev = (datetime.fromisoformat(win.night_date) - timedelta(days=1)).date().isoformat()
        opened = []
        with ops.ops_session() as s:
            before = set()
            for run in s.scalars(select(ops.NightRun).where(ops.NightRun.night_date == prev,
                                                            ops.NightRun.lanes.isnot(None))):
                for host, d in json.loads(run.lanes or "{}").items():
                    if d.get("stopped") in BLOCK_STOPS:
                        before.add(host)
            for host in blocked:
                if host not in before:
                    continue
                hold = s.get(ops.NightHold, host)
                if hold is not None and hold.state == "held":
                    continue
                if hold is None:
                    hold = ops.NightHold(host=host)
                    s.add(hold)
                hold.state, hold.since = "held", ops._now()
                hold.reason = f"блокування дві ночі поспіль ({prev}, {win.night_date})"
                hold.cleared_at = hold.cleared_by = None
                opened.append(host)
        return opened

    # --- після замка ---------------------------------------------------------------------

    def _coverage(self) -> None:
        """Покриття доказами на кінець вікна — у запис ночі (/api/status/night читає
        готове; інтеграція, конфлікт 10). Лише читання, без замка."""
        from . import evidence

        if self.scfg is None:
            return
        try:
            started = time.perf_counter()
            with self.env.session_scope() as s:
                self.evidence["coverage"] = evidence.coverage(s, self.scfg)
            self.evidence["coverage_s"] = round(time.perf_counter() - started, 2)
            self.rec.update(evidence=self.evidence)
        except Exception as e:                          # noqa: BLE001 — звіт не важливіший
            log.warning("ніч: покриття доказами не пораховано: %s", e)

    def _after_release(self, win: windows.Window) -> str | None:
        """Зведення для /status, строк продажу «після», утримання хостів — без замка.
        Повертає повідомлення для запису ночі (хости, що чекають рішення)."""
        from ..liveness import fuse, report

        env, rec = self.env, self.rec
        holds = self._open_holds(win)
        self._coverage()
        totals = {k: sum(int(d.get(k, 0)) for d in self.per_host.values())
                  for k in ("rows", "delisted", "restored", "repaired", "unknown")}
        status_report = None
        try:
            with env.session_scope() as s:
                status_report = report.status_block(s, self.lcfg, now=env.utcnow())
        except Exception as e:                          # noqa: BLE001
            log.warning("зведення для /status не пораховано: %s", e)
        requests = sum(int(d.get("requests") or 0) for d in self.lanes_info.values())
        self._finish_liveness("ok", requests=requests, checked=totals["rows"],
                              removed=totals["delisted"], returned=totals["restored"],
                              per_host=self.lanes_info, per_source=self.per_source,
                              per_tier={"plan": {h: p.tiers for h, p in self.plan.hosts.items()},
                                        "verdicts": self.per_tier},
                              fuse={"trips": self.trips, "mode": self.lcfg.fuse.mode,
                                    "held": sorted(fuse.held_sources())},
                              report=status_report)
        if self.ncfg.report.liquidity:
            try:
                written = identity_written(self.identity)
                if totals["delisted"] or totals["restored"] or any(written.values()):
                    with env.session_scope() as s:
                        rec.update(liquidity_after=env.liquidity(s))
                else:
                    with ops.ops_session() as s:
                        before = s.get(ops.NightRun, rec.id).liquidity_before
                    rec.update(liquidity_after=before)
            except Exception as e:                      # noqa: BLE001
                log.warning("строк продажу (після) не пораховано: %s", e)
        if holds:
            return f"хости чекають рішення власника: {', '.join(holds)}"
        return None


# --- План без мережі й запису (`cli.py night --dry-run`) ---------------------------------


def _observed_rates(limit: int = 20) -> dict[str, float]:
    """Секунд на запит смуги за останнім заміром ночі (check_seconds / requests, ≥ 20
    запитів): темп — старт-до-старту, тож справжній крок = max(темп, відповідь)."""
    out: dict[str, float] = {}
    ops.init_ops()
    with ops.ops_session() as s:
        for run in s.scalars(select(ops.NightRun).where(ops.NightRun.lanes.isnot(None))
                             .order_by(ops.NightRun.id.desc()).limit(limit)):
            try:
                lanes = json.loads(run.lanes or "{}")
            except ValueError:
                continue
            for host, d in lanes.items():
                req, secs = int(d.get("requests") or 0), d.get("check_seconds")
                if host not in out and req >= 20 and secs:
                    out[host] = float(secs) / req
    return out


def _last_backup_seconds(limit: int = 20) -> float | None:
    """Скільки тривав останній бекап на старті ночі (з запису ночі)."""
    ops.init_ops()
    with ops.ops_session() as s:
        for (raw,) in s.execute(select(ops.NightRun.backup).where(ops.NightRun.backup.isnot(None))
                                .order_by(ops.NightRun.id.desc()).limit(limit)):
            try:
                b = json.loads(raw)
            except ValueError:
                continue
            if b.get("due") and b.get("status") == "ok" and b.get("seconds"):
                return float(b["seconds"])
    return None


def dry_run(*, ncfg=None, lcfg=None, now: float | None = None, scope=None) -> dict:
    """План найближчого (чи поточного) вікна: ключі × крок = тривалість по хостах.

    Крок хоста — max(темп, замір минулої ночі, типовий з report.typical_request_seconds);
    з першого вікна віднімається бекап, якщо він потрібен (тривалість — з минулої ночі,
    інакше стеля backup.timeout_minutes); прогноз — скільки ключів лишиться після
    вікон 1–4 (ночі 1 і 2), без очікування замка циклу (рецензія E9, D53).
    Нічого не пише й замка не бере (лише читання бази й ops.db)."""
    from .. import configfiles
    from ..liveness import fuse, policy

    ncfg = ncfg or configfiles.load("night")
    lcfg = lcfg or policy.load()
    now = time.time() if now is None else now
    win = windows.upcoming(ncfg, now)
    held = fuse.held_sources()
    c = Conductor(ncfg=ncfg, lcfg=lcfg, env=Env(scope=scope))

    class _Rec:                                          # _skip_hosts читає лише id
        id = -1

    c.rec = _Rec()
    skip = c._skip_hosts(win)
    env = Env(scope=scope)
    with env.session_scope() as s:
        p = night_plan.build(s, lcfg, ncfg, now=utcnow(), held=held,
                             attempted_since=_utc_naive(c._night_began(win)), skip_hosts=skip)
        ev_plan = c._plan_evidence(s, p, c._night_began(win))
    onetime = sum(n for hp in p.lanes.values() for t, n in hp.tiers.items()
                  if t in ONETIME_TIERS)
    due, last_ok = default_backup_due(ncfg.backup.max_age_hours)
    rule = "max_age_hours"
    if not due and (onetime or ev_plan.get("writes")):
        due, last_ok = default_backup_due(ncfg.backup.onetime_max_age_hours)
        rule = "onetime_max_age_hours"
    backup_s = ((_last_backup_seconds() or ncfg.backup.timeout_minutes * 60) if due else 0.0)
    observed = _observed_rates()
    typical = ncfg.report.typical_request_seconds
    span = win.stop_requests - win.start
    hosts = {}
    for host, hp in sorted(p.hosts.items()):
        d = hp.as_dict()
        if host in observed:
            rate, source = max(hp.pace, observed[host]), "замір ночі"
        elif host in typical:
            rate, source = max(hp.pace, typical[host]), "типовий"
        else:
            rate, source = hp.pace, "темп"
        cap = int(span // rate) if rate > 0 else 0
        cap1 = int(max(0.0, span - backup_s) // rate) if rate > 0 else 0
        left, proj = hp.requests, []
        for w in range(4):
            left = max(0, left - (cap1 if w == 0 else cap))
            proj.append(left)
        if hp.requests <= cap1:
            need = 1 if hp.requests else 0
        else:
            need = (1 + -(-(hp.requests - cap1) // cap)) if cap else None
        d.update(rate=round(rate, 2), rate_source=source, window_capacity=cap,
                 first_window_capacity=cap1, windows_needed=need, left_after_windows=proj,
                 seconds_real=round(hp.requests * rate, 1))
        ev = _evidence_estimate(ncfg, host, hp, rate, span, backup_s)
        if ev:
            d["evidence_estimate"] = ev
        hosts[host] = d
    notes = []
    if lcfg.fuse.mode == "literal":
        heavy = sorted({lcfg.hosts[h].family for h, hp in p.lanes.items()
                        if any(hp.tiers.get(t) for t in ONETIME_TIERS)} & {"domria", "olx"})
        if heavy:
            notes.append(f"fuse.mode literal: перший же пакет вікна може тримати "
                         f"{', '.join(heavy)} (уже зняті рядки M2 — у частці, D53 відх. 4); "
                         f"тоді M2/M3 цього сайту стоять до рішення власника на /status")
    return {"window": {"label": win.label, "night_date": win.night_date,
                       "start": datetime.fromtimestamp(win.start).isoformat(timespec="minutes"),
                       "stop_requests": datetime.fromtimestamp(win.stop_requests)
                       .isoformat(timespec="minutes"),
                       "release_lock": datetime.fromtimestamp(win.release_lock)
                       .isoformat(timespec="minutes"),
                       "current": windows.current(ncfg, now) is not None},
            "backup": {"due": due, "rule": rule, "last_ok": last_ok,
                       "max_age_hours": ncfg.backup.max_age_hours,
                       "onetime_max_age_hours": ncfg.backup.onetime_max_age_hours,
                       "seconds": backup_s, "onetime_keys": onetime},
            "held": sorted(held), "skip": skip, "fuse_mode": lcfg.fuse.mode,
            "order": list(ncfg.jobs.order), "hosts": hosts, "notes": notes,
            "evidence": {k: v for k, v in ev_plan.items() if k != "feed"},
            "disabled": DISABLED_FLAG.exists()}


def _evidence_estimate(ncfg, host: str, hp, b1_rate: float, span: float,
                       backup_s: float) -> dict | None:
    """Дозбір доказів смуги в `--dry-run` (E11, D60): скільки запитів і за який крок —
    ПІСЛЯ Блоку 1 у тому самому вікні (той самий дедлайн), прогноз на вікна 1–4."""
    e = hp.evidence or {}
    if hp.skipped is not None:
        return None
    if "est_renders" in e:
        rcfg = ncfg.olx_render
        tabs = e.get("tabs_est_renders", 0) if any(
            (t or {}).get("due") for t in (e.get("tabs") or {}).values()) else 0
        # Уся черга без доказів (у вікнах з M2/M3 Блоку 1 її ключі ще в ярусах — вони
        # стануть у чергу деталей, щойно Блок 1 їх пройде).
        total = tabs + int((e.get("detail") or {}).get("missing", 0))
        out = {"job": "olx_render", "rate": round(max(hp.pace, rcfg.typical_seconds), 2),
               "rate_source": "типовий рендер", "total": total, "tabs": tabs,
               "cap_per_window": rcfg.max_per_window,
               "detail_missing": int((e.get("detail") or {}).get("missing", 0))}
    elif (e.get("feed") or {}).get("due"):
        source = next((s for s, h in (("lun", "lun.ua"), ("flombu", "flombu.com"))
                       if h == host), None)
        if source is None:
            return None
        f = e["feed"]
        out = {"job": f"feed:{source}", "rate": round(max(
                   hp.pace, ncfg.feed.typical_page_seconds[source]), 2),
               "rate_source": "типова сторінка", "total": int(f.get("pages_est") or 0),
               "cap_per_window": int(f.get("max_pages") or 0), "need": f.get("need")}
    elif (e.get("feed") or {}):
        return {"job": "feed", "total": 0, "why": e["feed"].get("why")}
    else:
        return None
    # Вікна: спершу Блок 1 (його крок), у решті часу — дозбір (не більше стелі вікна).
    b1_left, ev_left, per_window, used = hp.requests, out["total"], [], []
    for w in range(4):
        t = max(0.0, span - backup_s) if w == 0 else span
        take = min(b1_left, int(t // b1_rate)) if b1_rate > 0 else b1_left
        b1_left -= take
        t -= take * b1_rate
        n = 0
        if b1_left == 0 and ev_left and out["rate"] > 0:
            n = min(ev_left, int(t // out["rate"]), out["cap_per_window"] or ev_left)
            ev_left -= n
        per_window.append(n)
        used.append(round((take * b1_rate + n * out["rate"]) / 60, 1))
    full = min(out["cap_per_window"] or 10 ** 9, int(span // out["rate"])) if out["rate"] else 0
    out.update(per_window=per_window, left_after_windows=[
        max(0, out["total"] - sum(per_window[:i + 1])) for i in range(4)],
        minutes_used=used, window_minutes=round(span / 60, 1),
        # Вікно без Блоку 1 (після M2/M3 — сталий режим): скільки запитів дозбору і вікон.
        full_window=full, windows_alone=(-(-out["total"] // full) if full else None))
    return out


def run(*, budget_min: float | None = None, env: Env | None = None) -> dict:
    return Conductor(budget_min=budget_min, env=env).run()


def unhold(host: str, *, by: str) -> bool:
    ops.init_ops()
    with ops.ops_session() as s:
        hold = s.get(ops.NightHold, host)
        if hold is None or hold.state != "held":
            return False
        hold.state, hold.cleared_at, hold.cleared_by = "clear", ops._now(), by[:32]
        return True
