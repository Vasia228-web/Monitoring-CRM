"""Вікна транзакцій запису кроків циклу (Блок 2, крок E2, D49).

Навіщо. Поки процес тримає транзакцію запису в realty.db, інші записувачі
чекають: кнопки власника під блокуванням кроку «дублі» на Fedora чекали 8,9 с,
а перегляд картки під чужим 40-секундним блокуванням отримав 500 (план Блоку 2,
D48). Щоб лікувати довгі блокування за виміром, а не наосліп, кожен крок циклу
міряє свої вікна: від ПЕРШОЇ зміни в транзакції (INSERT/UPDATE/DELETE/…; саме
тоді SQLite бере блокування запису) до commit або rollback. Наприкінці процесу
в ops.db пишеться один рядок `write_windows`: найдовше вікно, сума, кількість
транзакцій і з якого запиту почалось найдовше (лише текст SQL зі «?», без даних).

Вмикається змінною TXN_WATCH=1 — її ставить диригент (`runner.py`) крокам
циклу, якщо в config/speed.toml `txn_watch.enabled = true`; TXN_WATCH_STEP —
назва кроку. Без змінної нічого не відбувається.

Межі. Міряються лише записи через SQLAlchemy (рушій `realty.db.engine`):
сирий sqlite3 (бекап, ремонт схеми) — ні. Процес, убитий за тайм-аутом
(SIGTERM/SIGKILL), рядка не залишить: atexit тоді не виконується.

Друга половина модуля (Блок 2, крок E5, D50) — «хто змінив дані»: лічильник
зафіксованих транзакцій із DML для КОЖНОЇ бази в процесі. Він завжди ввімкнений
(дешево: перевірка перших літер запиту) і дає дві речі:
  * сайту — версію даних у власному процесі: дія власника, записана будь-яким
    шляхом, одразу робить застарілими кеші списків (`web/speedcache.py`);
  * будь-якому іншому процесу (`cli.py …`) — автоматичне збільшення покоління
    «lists» у ops.db при виході, якщо він справді щось записав у realty.db
    (`install_autobump`). Так кеш сайту бачить зміни від ЛЮБОЇ команди, а не
    лише від тих, що потрапили в перелік (інтеграція, конфлікт «кеш»).
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

log = logging.getLogger(__name__)

ENV_FLAG = "TXN_WATCH"
ENV_STEP = "TXN_WATCH_STEP"
_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP")


class Watch:
    """Лічильники вікон запису одного рушія."""

    def __init__(self, process: str | None = None, step: str | None = None,
                 clock=time.perf_counter) -> None:
        self.process = process
        self.step = step
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[int, tuple[float, str]] = {}      # з'єднання → (початок, SQL)
        self.started = clock()
        self.max_ms = 0.0
        self.total_ms = 0.0
        self.txns = 0
        self.committed = 0
        self.max_sql: str | None = None

    # --- події рушія ---------------------------------------------------------------------

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip()[:8].upper()
        if not head.startswith(_WRITES):
            return
        key = id(conn.connection.dbapi_connection)
        with self._lock:
            if key not in self._open:
                self._open[key] = (self._clock(), " ".join(statement.split())[:160])

    def _close(self, dbapi_connection, committed: bool) -> None:
        key = id(dbapi_connection)
        with self._lock:
            opened = self._open.pop(key, None)
            if opened is None:
                return
            ms = (self._clock() - opened[0]) * 1000
            self.txns += 1
            self.total_ms += ms
            if committed:
                self.committed += 1
            if ms > self.max_ms:
                self.max_ms, self.max_sql = ms, opened[1]

    def _commit(self, conn):
        self._close(conn.connection.dbapi_connection, True)

    def _rollback(self, conn):
        self._close(conn.connection.dbapi_connection, False)

    def _reset(self, dbapi_connection, connection_record, reset_state=None):
        # Пул відкочує незавершену транзакцію, коли з'єднання повертається без
        # commit (сесію закрили) — це теж кінець вікна.
        self._close(dbapi_connection, False)

    def attach(self, engine) -> "Watch":
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "commit", self._commit)
        event.listen(engine, "rollback", self._rollback)
        event.listen(engine.pool, "reset", self._reset)
        return self

    def detach(self, engine) -> None:
        for name, fn in (("before_cursor_execute", self._before), ("commit", self._commit),
                         ("rollback", self._rollback)):
            if event.contains(engine, name, fn):
                event.remove(engine, name, fn)
        if event.contains(engine.pool, "reset", self._reset):
            event.remove(engine.pool, "reset", self._reset)

    # --- запис ---------------------------------------------------------------------------

    def row(self) -> dict:
        return {"process": (self.process or "")[:64] or None, "step": (self.step or "")[:96] or None,
                "max_ms": round(self.max_ms, 1), "total_ms": round(self.total_ms, 1),
                "txns": self.txns, "max_sql": self.max_sql,
                "seconds": round(self._clock() - self.started, 1)}

    def record(self) -> None:
        """Один рядок у ops.db write_windows (наприкінці процесу)."""
        from . import ops

        try:
            ops.init_ops()
            with ops.ops_session() as s:
                s.add(ops.WriteWindow(**self.row()))
        except Exception as e:                      # noqa: BLE001 — вимір не валить крок
            log.warning("txn_watch: рядок не записано: %s", e)


_active: Watch | None = None


def install_from_env(argv: list[str]) -> Watch | None:
    """Увімкнути вимір для цього процесу, якщо диригент поставив TXN_WATCH=1."""
    global _active
    if os.environ.get(ENV_FLAG) != "1" or _active is not None:
        return _active
    from .db import engine

    process = " ".join(a for a in argv[1:3] if not a.startswith("-")) or None
    _active = Watch(process, os.environ.get(ENV_STEP) or None).attach(engine)
    atexit.register(_active.record)
    return _active


# --- Хто змінив дані: версія даних у процесі й автоматичне покоління (Блок 2, E5) ----------

_DML = ("INSERT", "UPDATE", "DELETE", "REPLACE")
# Опція виконання для записів, що НЕ змінюють видимого сайтом: лічильник
# переглядів карток (`web/deferred.py`). Без неї кожен фоновий запис переглядів
# раз на хвилину скидав би кеші списків сайту — хоча список від переглядів не
# залежить.
MINOR = "realty_minor_write"


def _url_key(engine) -> str:
    return engine.url.render_as_string(hide_password=True)


class DmlTracker:
    """Лічильник зафіксованих транзакцій із DML — окремо для кожної бази процесу.

    Події рівня класу Engine/Pool: охоплюють і рушії, створені пізніше (тести
    підставляють свій рушій на tmp_path). Транзакція, що лише читала, і
    відкочена транзакція лічильника не змінюють.

    Чому версія росте ДВІЧІ на коміт. Подія Engine «commit» у SQLAlchemy 2.0
    спрацьовує ДО самого коміту в SQLite (Connection._commit_impl: спершу
    dispatch.commit, потім dialect.do_commit). Запит сайту, що саме в цю мить
    обчислив ключ кешу, прочитав би знімок бази ДО коміту — і закешував би
    старі id під НОВОЮ версією аж до наступного покоління (до 15 хв). Тому
    друге збільшення — після коміту: коли з'єднання повертається в пул (подія
    Pool «reset» — уже після do_commit) або виконує наступний запит. Ключ,
    обчислений у вікні між ними, стає застарілим одразу після коміту.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[int, bool] = {}      # з'єднання → чи є «справжній» запис
        self._committing: dict[int, str] = {}    # з'єднання → база, коміт якої ще йде
        self._versions: dict[str, int] = {}
        self._installed = False

    def install(self) -> "DmlTracker":
        with self._lock:
            if self._installed:
                return self
            event.listen(Engine, "before_cursor_execute", self._before)
            event.listen(Engine, "commit", self._commit)
            event.listen(Engine, "rollback", self._rollback)
            event.listen(Pool, "reset", self._reset)
            self._installed = True
        return self

    def _after_commit(self, key: int) -> None:
        """Друге збільшення — коміт у SQLite уже завершився (див. докстрінг класу)."""
        url = self._committing.pop(key, None)
        if url is not None:
            self._versions[url] = self._versions.get(url, 0) + 1

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        key = id(conn.connection.dbapi_connection)
        if key in self._committing:
            with self._lock:
                self._after_commit(key)
        if not statement.lstrip()[:7].upper().startswith(_DML):
            return
        options = context.execution_options if context is not None else {}
        major = not options.get(MINOR)
        with self._lock:
            self._pending[key] = self._pending.get(key, False) or major

    def _commit(self, conn):
        key = id(conn.connection.dbapi_connection)
        with self._lock:
            major = self._pending.pop(key, False)
            if major:
                url = _url_key(conn.engine)
                self._versions[url] = self._versions.get(url, 0) + 1
                self._committing[key] = url

    def _rollback(self, conn):
        key = id(conn.connection.dbapi_connection)
        with self._lock:
            self._pending.pop(key, None)
            self._after_commit(key)

    def _reset(self, dbapi_connection, connection_record, reset_state=None):
        key = id(dbapi_connection)
        with self._lock:
            self._pending.pop(key, None)
            self._after_commit(key)

    def version(self, engine) -> int:
        """Скільки транзакцій із записом цей процес зафіксував у базі рушія."""
        with self._lock:
            return self._versions.get(_url_key(engine), 0)

    def bump_local(self, engine) -> None:
        """Позначити дані бази зміненими без запису (тести, ручне скидання)."""
        with self._lock:
            url = _url_key(engine)
            self._versions[url] = self._versions.get(url, 0) + 1


TRACKER = DmlTracker()


def data_version(engine) -> int:
    return TRACKER.version(engine)


class AutoBump:
    """Покоління «lists» в ops.db — щойно процес зафіксував запис у realty.db.

    `bump_if_dirty()` — явні точки (кінець процесу, пакет нічної роботи);
    збільшує покоління, лише якщо від попереднього разу були нові записи.
    """

    def __init__(self, process: str | None) -> None:
        self.process = (process or "cli")[:64]
        self._seen = 0

    def bump_if_dirty(self, scope: str = "lists", reason: str | None = None) -> bool:
        from .db import engine
        from . import webcache

        now = TRACKER.version(engine)
        if now <= self._seen:
            return False
        self._seen = now
        return webcache.bump(scope, reason or self.process)

    def mark_clean(self) -> None:
        """Записи досі вже враховано — при виході покоління за них не збільшувати.

        Для процесу, який сам знає, чи змінилось видиме сайту (перевірка при
        відкритті: службові last_attempt/check_events списку не змінюють, а
        зняття чи повернення оголошення вона оголошує явним покоління).
        """
        from .db import engine

        self._seen = TRACKER.version(engine)


_autobump: AutoBump | None = None


def install_autobump(argv: list[str]) -> AutoBump:
    """Для процесів, що не є сайтом: при виході — покоління «lists», якщо був запис.

    Сайт цього не ставить: свої записи він бачить у власному процесі одразу
    (`data_version`), а покоління в ops.db — сигнал для нього від інших.
    Убитий процес (SIGKILL за тайм-аутом кроку) нічого не збільшить — тому
    диригент збільшує покоління ще й після КОЖНОГО кроку сам (runner.py).
    """
    global _autobump
    if _autobump is None:
        process = " ".join(a for a in argv[1:3] if not a.startswith("-")) or None
        _autobump = AutoBump(process)
        atexit.register(_exit_bump)
    return _autobump


def autobump() -> AutoBump | None:
    return _autobump


def mark_clean() -> None:
    """AutoBump.mark_clean для цього процесу (якщо автоматичне покоління ввімкнене)."""
    if _autobump is not None:
        _autobump.mark_clean()


def _exit_bump() -> None:
    if _autobump is None:
        return
    try:
        _autobump.bump_if_dirty("lists")
    except Exception as e:                          # noqa: BLE001 — вихід процесу не валимо
        log.warning("покоління кешу сайту не збільшено: %s", e)
