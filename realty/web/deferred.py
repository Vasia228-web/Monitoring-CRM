"""Фоновий запис телеметрії сайту в ops.db — пакетами, поза запитом.

Навіщо. Запит сайту не має чекати на базу заради службового запису: на
Fedora під чужим 12-секундним блокуванням GET /property чекав 12 047 мс, а
під 40-секундним — отримав 500 «database is locked» (заміри плану Блоку 2,
D48). Тому журнал часу запитів і маячок браузера (Блок 2, крок E2) лише
кладуть рядок у буфер у пам'яті, а окремий потік раз на `timings.flush_s`
записує все одним коротким пакетом.

Правила (config/speed.toml):
  * свій тайм-аут на зайняту базу — `deferred.busy_timeout_ms` (2 с), а не
    30 с, як у решти ops.db: база зайнята — рядки лишаються в буфері до
    наступного разу, нічого не губиться і ніхто не чекає;
  * буфер має стелю `deferred.max_buffer_rows`: понад неї найстаріші рядки
    відкидаються й рахуються (`dropped`) — пам'ять сайту не росте без меж;
  * при зупинці сайту буфер дописується (lifespan);
  * раз на `timings.cleanup_every_h` — прибирання рядків, старших за строк
    зберігання.
Той самий потік раз на `generations.poll_s` викликає «тики» — дешеві фонові
перевірки (чи йде цикл збору, `perf.CYCLE`; покоління даних, `speedcache.poll`),
щоб запит їх не робив.

Крок E5 (D50): сюди ж — перегляди карток (realty.db: views + n, viewed_at —
останнє відкриття; раз на `deferred.views_flush_s`) і last_seen сесій входу
(ops.db; той самий період, що й журнал часу). GET сторінки квартири й перевірка
сесії більше нічого не пишуть у запиті. Перегляди однієї квартири між
записами складаються (n відкриттів → views + n), тож кінцеві views/viewed_at ті
самі, що дав би прямий запис; база зайнята — буфер чекає наступної спроби;
при зупинці сайту — дописується. Падіння сайту губить ≤ `views_flush_s`
лічильника (впливає лише на чергу перевірок).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable

from sqlalchemy import bindparam, delete, func, insert, update
from sqlalchemy.exc import OperationalError

from .. import configfiles, ops, txnwatch
from ..db import BUSY_TIMEOUT_MS as MAIN_BUSY_TIMEOUT_MS
from ..webcache import EMERGENCY_POLL_S as _EMERGENCY_POLL_S

log = logging.getLogger(__name__)

# Які таблиці ops.db пише цей буфер і за яким полем часу їх прибирати.
TABLES = {"web_timings": ops.WebTiming, "web_rum": ops.WebRum}


def _speed():
    return configfiles.get("speed")


# Аварійні налаштування — лише якщо config/speed.toml не читається ще з першого
# разу (зламаний файл на старті сайту; правку, що зламалась ПІСЛЯ старту, Watched
# і так не підхоплює — лишається попередня чинна). Без них фоновий потік падав
# би на першому ж оберті: перегляди й last_seen сесій не писались би (і
# губились би з перезапуском), а покоління ніхто б не перечитував. Числа — ті
# самі, що в config/speed.toml (тест стежить, щоб не розійшлися); прибирання
# журналу без строку зберігання не робиться.
EMERGENCY = SimpleNamespace(
    generations=SimpleNamespace(poll_s=_EMERGENCY_POLL_S),
    timings=SimpleNamespace(flush_s=60.0, cleanup_every_h=None),
    deferred=SimpleNamespace(views_flush_s=60.0, busy_timeout_ms=2000, max_buffer_rows=6000),
)


def _ops_engine():
    """Рушій ops.db, яким користується решта коду (тести його підмінюють)."""
    return ops.OpsSession.kw.get("bind") or ops.engine


def _main_engine():
    """Рушій realty.db сайту (тести підмінюють фабрику сесій сторінок)."""
    from . import app

    return app.SessionLocal.kw.get("bind")


class DeferredWriter:
    def __init__(self, *, settings: Callable = _speed, clock=time.monotonic) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: deque[tuple[str, dict]] = deque()
        self._ticks: list[Callable[[], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_flush = clock()
        self._last_cleanup: float | None = None
        # Перегляди: рушій бази → {id оголошення → [скільки відкриттів, час останнього]}.
        # Рушій — той, з якого сторінка прочитала оголошення: запис іде в ту саму
        # базу, навіть якщо фабрику сесій сайту тим часом підмінили (тести).
        self._views: dict[object, dict[int, list]] = {}
        self._last_views = clock()
        # Сесії: sid → коли востаннє бачили (лише найсвіжіше).
        self._touches: dict[str, object] = {}
        self.written = 0
        self.dropped = 0
        self.failed_flushes = 0
        self.views_written = 0
        self.touches_written = 0
        self.last_error: str | None = None
        self._config_error: str | None = None

    def _cfg(self):
        """Чинні налаштування або аварійні (EMERGENCY), якщо конфіг не читається.

        Помилка — у журнал один раз (а не щоп'ять секунд); щойно файл знову
        читається, діють його значення.
        """
        try:
            cfg = self._settings()
        except configfiles.ConfigError as e:
            if self._config_error != str(e):
                self._config_error = str(e)
                log.error("config/speed.toml не читається — фоновий запис працює з "
                          "аварійними налаштуваннями: %s", e)
            return EMERGENCY
        self._config_error = None
        return cfg

    # --- Буфер ---------------------------------------------------------------------------

    def add(self, table: str, row: dict) -> None:
        """Покласти рядок у буфер (дешево: викликається з обробки запиту)."""
        if table not in TABLES:
            raise ValueError(f"невідома таблиця фонового запису: {table}")
        limit = self._cfg().deferred.max_buffer_rows
        with self._lock:
            self._rows.append((table, row))
            while len(self._rows) > limit:
                self._rows.popleft()
                self.dropped += 1

    def add_views(self, listing_ids, at, engine=None) -> None:
        """Відкриття картки: +1 перегляд кожному оголошенню квартири (без запису тут).

        `engine` — база, звідки взято оголошення (типово — база сторінок сайту).
        """
        engine = engine if engine is not None else _main_engine()
        with self._lock:
            bucket = self._views.setdefault(engine, {})
            for lid in listing_ids:
                entry = bucket.get(lid)
                if entry is None:
                    bucket[lid] = [1, at]
                else:
                    entry[0] += 1
                    entry[1] = max(entry[1], at)

    def touch_session(self, sid: str, at) -> None:
        """last_seen сесії входу — фоном (раз на sessions.TOUCH_EVERY, як і досі)."""
        with self._lock:
            prev = self._touches.get(sid)
            self._touches[sid] = at if prev is None or at > prev else prev

    def pending_views(self) -> dict[int, tuple[int, object]]:
        with self._lock:
            return {k: (v[0], v[1]) for bucket in self._views.values()
                    for k, v in bucket.items()}

    def pending(self) -> int:
        with self._lock:
            return len(self._rows)

    def take(self) -> list[tuple[str, dict]]:
        """Забрати все з буфера, нічого не записуючи (діагностика й тести)."""
        with self._lock:
            rows = list(self._rows)
            self._rows.clear()
        return rows

    def flush(self) -> int:
        """Записати все з буфера одним пакетом. Повертає, скільки записано.

        База зайнята довше за `busy_timeout_ms` — рядки повертаються в буфер
        (на початок, у тому самому порядку) до наступної спроби.
        """
        with self._lock:
            batch = list(self._rows)
            self._rows.clear()
        self._last_flush = self._clock()
        if not batch:
            return 0
        by_table: dict[str, list[dict]] = {}
        for table, row in batch:
            by_table.setdefault(table, []).append(row)
        try:
            self._write(by_table)
        except OperationalError as e:
            self.failed_flushes += 1
            self.last_error = str(e.orig if getattr(e, "orig", None) else e)[:200]
            log.warning("фоновий запис у ops.db відкладено (%s) — %d рядків чекають",
                        self.last_error, len(batch))
            with self._lock:
                self._rows.extendleft(reversed(batch))
                limit = self._cfg().deferred.max_buffer_rows
                while len(self._rows) > limit:
                    self._rows.popleft()
                    self.dropped += 1
            return 0
        self.written += len(batch)
        return len(batch)

    def _transaction(self, work: Callable) -> None:
        """Одна транзакція в ops.db зі своїм коротким тайм-аутом на зайняту базу.

        Тайм-аут ставиться на сирому з'єднанні SQLite (PRAGMA не відкриває
        транзакції) і повертається назад за будь-якого результату: з'єднання
        йде назад у пул, і решта коду має отримати звичайні 30 с.
        """
        busy = int(self._cfg().deferred.busy_timeout_ms)
        with _ops_engine().connect() as conn:
            raw = conn.connection.driver_connection
            raw.execute(f"PRAGMA busy_timeout = {busy}")
            try:
                with conn.begin():
                    work(conn)
            finally:
                raw.execute(f"PRAGMA busy_timeout = {ops.BUSY_TIMEOUT_MS}")

    def _write(self, by_table: dict[str, list[dict]]) -> None:
        def work(conn):
            for table, rows in by_table.items():
                conn.execute(insert(TABLES[table]), rows)
        self._transaction(work)

    def flush_views(self) -> int:
        """Записати буфер переглядів у realty.db коротким пакетом (на кожну базу — один).

        UPDATE views = coalesce(views, 0) + n, viewed_at = час останнього — те
        саме, що робив GET досі, лише раз на період. Свій короткий тайм-аут на
        зайняту базу; зайнята — буфер повертається (зливаючись із новими
        відкриттями) до наступного разу. Запис позначений txnwatch.MINOR: від
        переглядів список не залежить, і кеші сайту через них не скидаються.
        """
        with self._lock:
            batches, self._views = self._views, {}
        self._last_views = self._clock()
        written = 0
        for engine, batch in batches.items():
            if not batch:
                continue
            rows = [{"lid": lid, "n": n, "at": at} for lid, (n, at) in batch.items()]
            try:
                self._write_main(engine, rows)
            except OperationalError as e:
                self.failed_flushes += 1
                self.last_error = str(e.orig if getattr(e, "orig", None) else e)[:200]
                log.warning("перегляди карток відкладено (%s) — %d оголошень чекають",
                            self.last_error, len(batch))
                with self._lock:
                    bucket = self._views.setdefault(engine, {})
                    for lid, (n, at) in batch.items():
                        entry = bucket.get(lid)
                        if entry is None:
                            bucket[lid] = [n, at]
                        else:
                            entry[0] += n
                            entry[1] = max(entry[1], at)
                continue
            written += len(rows)
        self.views_written += written
        return written

    def _write_main(self, engine, rows: list[dict]) -> None:
        from ..models import Listing

        busy = int(self._cfg().deferred.busy_timeout_ms)
        # coalesce: у старих рядках views буває NULL — як і досі `(views or 0) + 1`.
        # Через типізований UPDATE: дата пишеться тим самим форматом, що й з ORM.
        sql = (update(Listing).where(Listing.id == bindparam("lid"))
               .values(views=func.coalesce(Listing.views, 0) + bindparam("n"),
                       viewed_at=bindparam("at", type_=Listing.viewed_at.type)))
        with engine.connect() as conn:
            conn = conn.execution_options(**{txnwatch.MINOR: True})
            raw = conn.connection.driver_connection
            raw.execute(f"PRAGMA busy_timeout = {busy}")
            try:
                with conn.begin():
                    conn.execute(sql, rows)
            finally:
                raw.execute(f"PRAGMA busy_timeout = {MAIN_BUSY_TIMEOUT_MS}")

    def flush_touches(self) -> int:
        """last_seen сесій — одним пакетом у ops.db (свій короткий тайм-аут)."""
        with self._lock:
            batch, self._touches = self._touches, {}
        if not batch:
            return 0
        from .sessions import AuthSession

        rows = [{"sid_": sid, "at": at} for sid, at in batch.items()]

        def work(conn):
            # Лише вперед і лише наявні сесії: «Вийти» (рядка вже немає) не
            # воскрешається, а старіший запис не перетирає новіший.
            conn.execute(update(AuthSession)
                         .where(AuthSession.sid == bindparam("sid_"),
                                AuthSession.last_seen < bindparam("at"))
                         .values(last_seen=bindparam("at")), rows)
        try:
            self._transaction(work)
        except OperationalError as e:
            self.failed_flushes += 1
            self.last_error = str(e.orig if getattr(e, "orig", None) else e)[:200]
            with self._lock:
                for sid, at in batch.items():
                    prev = self._touches.get(sid)
                    self._touches[sid] = at if prev is None or at > prev else prev
            return 0
        self.touches_written += len(rows)
        return len(rows)

    def cleanup(self) -> int:
        """Прибрати рядки, старші за строк зберігання (журнал часу й маячок)."""
        cfg = self._settings()            # без чинного конфігу строку зберігання немає — не прибираємо
        now = ops._now()
        cutoffs = {"web_timings": now - timedelta(days=cfg.timings.retention_days),
                   "web_rum": now - timedelta(days=cfg.rum.retention_days)}
        removed = [0]

        def work(conn):
            for table, cutoff in cutoffs.items():
                model = TABLES[table]
                removed[0] += conn.execute(delete(model).where(model.at < cutoff)).rowcount
        self._transaction(work)
        return removed[0]

    # --- Фоновий потік -------------------------------------------------------------------

    def add_tick(self, fn: Callable[[], None]) -> None:
        self._ticks.append(fn)

    def run_once(self) -> None:
        """Один оберт фонового потоку (його ж викликають тести)."""
        for fn in list(self._ticks):
            try:
                fn()
            except Exception as e:                  # noqa: BLE001 — фон не має падати
                log.warning("фонова перевірка %s не вдалась: %s", getattr(fn, "__name__", fn), e)
        cfg = self._cfg()
        now = self._clock()
        if now - self._last_flush >= cfg.timings.flush_s:
            for flush in (self.flush, self.flush_touches):
                try:
                    flush()
                except Exception as e:              # noqa: BLE001
                    self.last_error = f"{type(e).__name__}: {e}"[:200]
                    log.warning("фоновий запис не вдався: %s", self.last_error)
        if now - self._last_views >= cfg.deferred.views_flush_s:
            try:
                self.flush_views()
            except Exception as e:                  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("запис переглядів не вдався: %s", self.last_error)
        every_h = cfg.timings.cleanup_every_h
        if every_h is not None and (self._last_cleanup is None
                                    or now - self._last_cleanup >= every_h * 3600):
            self._last_cleanup = now
            try:
                self.cleanup()
            except Exception as e:                  # noqa: BLE001
                log.warning("прибирання журналу часу не вдалось: %s", e)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:                  # noqa: BLE001 — потік не має вмирати
                log.warning("оберт фонового запису не вдався: %s", e)
            self._stop.wait(self._cfg().generations.poll_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="realty-deferred", daemon=True)
        self._thread.start()

    def stop(self, *, flush: bool = True) -> None:
        """Зупинити потік і (типово) дописати буфер — при зупинці сайту."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        if flush:
            for fn in (self.flush, self.flush_touches, self.flush_views):
                try:
                    fn()
                except Exception as e:              # noqa: BLE001
                    log.warning("буфер при зупинці не дописано: %s", e)
