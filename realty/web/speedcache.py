"""Кеші шляху запиту сайту за поколіннями даних (Блок 2, крок E5, D50).

Навіщо. Головна «/» робила на КОЖЕН перегляд сім повних проходів 94-МБ таблиці
(лічильник зі згортанням, медіани зведення, групування за якістю…; Етап 0:
1,85 с на Fedora). Дані ж змінюються кроками циклу збору раз на кілька хвилин,
а не між двома кліками. Тому:

  * **список у дві фази** (queries.list_ids_select): спершу впорядковані id
    усіх рядків фільтра з покривного індексу, потім 50 рядків сторінки за id.
    Список id кешується тут за ключем «покоління даних + фільтр»: лічильник
    «N за фільтром» і нумерація сторінок — довжина й зріз цього списку, тож
    вони ті самі, що дав би живий запит на тих самих даних;
  * **зведення над списком** (медіани, кількості) — раз на покоління;
  * **покоління** — три незалежні лічильники, з яких складається ключ:
      1) `web_generations` в ops.db (webcache.py) — дані змінив ІНШИЙ процес
         (крок циклу, cli.py); читає фоновий потік раз на `generations.poll_s`;
      2) версія даних у власному процесі (txnwatch.data_version) — будь-який
         зафіксований запис сайту в realty.db (дія власника будь-яким шляхом);
      3) `owner_changed()` — явний сигнал (результат перевірки при відкритті
         від окремого процесу, кнопки, що пишуть не через цей рушій).
    Дія власника тому видна на НАСТУПНОМУ ж відкритті сторінки — умова
    власника для кешу (промт 11, Блок 2);
  * запасний ліміт віку `list_memo.max_age_s` — якщо якийсь запис у базу не
    збільшив покоління (сирий sqlite3, процес, убитий до виходу).

Кеш не змінює виводу: на заморожених даних сторінка байт у байт та сама
(scripts/page_equality.py). Вимкнути — `list_memo.enabled = false` у
config/speed.toml: тоді кожен запит рахує живий список (~150 мс на Fedora).

Пам'ять: id зберігаються масивом `array('i')` (4 Б на id), а не списком
Python-об'єктів (~36 Б): 64 ключі × ≤22 тис. id ≤ 5,6 МБ у найгіршому
випадку (межа власника — приріст RSS сайту ≤40 МБ).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from array import array
from collections import OrderedDict
from typing import Callable, Hashable, Iterable

from .. import configfiles, txnwatch, webcache
from .navstate import LIST_KEYS

log = logging.getLogger(__name__)

# Параметри адреси списку, що НЕ впливають на список id: нумерація сторінки й
# розмір сторінки — це зріз уже готового списку.
PAGING_KEYS = ("page", "per_page")


def _speed():
    return configfiles.get("speed")


# --- Фонова робота -----------------------------------------------------------------------


class Background:
    """Один фоновий потік із чергою завдань; однакові завдання не дублюються.

    Для перебудови знімка «Аналітики» й прогріву кешу після нового покоління:
    ця робота не має потрапляти в запит користувача. Потік стартує ліниво —
    і в тестах без lifespan, і на сайті.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._q: queue.Queue = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.done = 0
        self.failed = 0

    def submit(self, key: str, fn: Callable[[], None]) -> bool:
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
                self._thread.start()
        self._q.put((key, fn))
        return True

    def _loop(self) -> None:
        while True:
            key, fn = self._q.get()
            with self._lock:
                self._pending.discard(key)
            try:
                fn()
                self.done += 1
            except Exception as e:                  # noqa: BLE001 — фон не має падати
                self.failed += 1
                log.warning("фонове завдання %s не вдалось: %s", key, e)
            finally:
                self._q.task_done()

    def join(self, timeout: float | None = None) -> bool:
        """Дочекатися порожньої черги (тести). True — дочекались."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                idle = not self._pending and self._q.unfinished_tasks == 0
            if idle:
                return True
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.01)


BACKGROUND = Background("realty-speedcache")


# --- Покоління -----------------------------------------------------------------------------

GENERATIONS = webcache.GENERATIONS


class _OwnerGen:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0
        self.last_reason: str | None = None

    def bump(self, reason: str) -> int:
        with self._lock:
            self.value += 1
            self.last_reason = reason
            return self.value


OWNER = _OwnerGen()


def owner_changed(reason: str = "owner") -> None:
    """Дані змінились так, що процес сам цього не побачив би (або для ясності).

    Кнопки власника пишуть у realty.db через рушій сайту — це й так видно з
    txnwatch.data_version; виклик тут — явна позначка в коді обробника й захист
    від запису в обхід рушія. Результат перевірки при відкритті пише ІНШИЙ
    процес — тоді це єдиний спосіб побачити його одразу, не чекаючи покоління.
    """
    OWNER.bump(reason)
    for memo in (LIST_IDS, STATS):
        memo.clear()
    warm()


def data_key(session) -> tuple:
    """Ключ версії даних, від якої залежать список і зведення."""
    engine = session.get_bind()
    return (txnwatch._url_key(engine), GENERATIONS.current("lists"),
            txnwatch.data_version(engine), OWNER.value)


# --- Пам'ять ключ → значення з одним обчисленням на ключ --------------------------------------


class Memo:
    """LRU з обмеженням віку й одним обчисленням на ключ (single-flight).

    Поки один запит рахує список для ключа, інші з тим самим ключем чекають
    його результату, а не рахують удруге (на Fedora ≈150 мс процесора кожен).
    Обчислення йде ПОЗА замком: інші ключі не чекають.
    """

    def __init__(self, name: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[Hashable, tuple[float, object]] = OrderedDict()
        self._inflight: dict[Hashable, threading.Event] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable, compute: Callable[[], object], *, max_keys: int,
            max_age_s: float) -> object:
        while True:
            with self._lock:
                entry = self._data.get(key)
                if entry is not None and self._clock() - entry[0] <= max_age_s:
                    self._data.move_to_end(key)
                    self.hits += 1
                    return entry[1]
                waiting = self._inflight.get(key)
                if waiting is None:
                    mine = self._inflight[key] = threading.Event()
                    break
            waiting.wait(timeout=60)
        try:
            value = compute()
            with self._lock:
                self.misses += 1
                self._data[key] = (self._clock(), value)
                self._data.move_to_end(key)
                while len(self._data) > max(1, max_keys):
                    self._data.popitem(last=False)
            return value
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            mine.set()

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


LIST_IDS = Memo("list_ids")
STATS = Memo("stats")


def list_key(state: dict, *, in_progress: bool | None, collapse: bool) -> tuple:
    """Ключ фільтра — з УСІХ параметрів navstate.LIST_KEYS, крім нумерації.

    Новий фільтр (Блоки 3/4) додається в LIST_KEYS — і автоматично потрапляє в
    ключ; якщо обробник забув передати його значення — KeyError, а не тихий
    спільний ключ для різних вибірок (тест test_list_key_covers_every_filter).
    """
    return (tuple((k, state[k]) for k in LIST_KEYS if k not in PAGING_KEYS),
            ("in_progress", in_progress), ("collapse", collapse))


def _memo_settings():
    try:
        cfg = _speed().list_memo
        return cfg.enabled, cfg.max_keys, cfg.max_age_s
    except configfiles.ConfigError as e:
        log.error("config/speed.toml не читається — список без кешу: %s", e)
        return False, 1, 0.0


def list_ids(session, key: tuple, compute: Callable[[], Iterable[int]]) -> array:
    """Упорядковані id рядків списку для фільтра `key` — з кешу або живим запитом."""
    enabled, max_keys, max_age = _memo_settings()

    def run() -> array:
        ids = compute()
        # 4 байти на id (id оголошень — десятки тисяч, далеко до 2^31); 8 — якщо
        # колись переросте.
        try:
            return array("i", ids)
        except OverflowError:
            return array("q", ids)

    if not enabled:
        return run()
    return LIST_IDS.get((data_key(session), key), run, max_keys=max_keys, max_age_s=max_age)


def stats(session, compute: Callable[[], dict]) -> dict:
    """Зведення над усією таблицею (шапка списку, /api/stats) — раз на покоління.

    Значення спільне між запитами — не змінювати.
    """
    enabled, _keys, max_age = _memo_settings()
    if not enabled:
        return compute()
    # Ключів тут небагато (одна база); 4 — запас на перехід між поколіннями.
    return STATS.get(data_key(session), compute, max_keys=4, max_age_s=max_age)


# --- Прогрів після нового покоління ------------------------------------------------------------

_warmer: Callable[[], None] | None = None


def set_warmer(fn: Callable[[], None]) -> None:
    """app.py реєструє, як порахувати типові ключі («/», «/processing», зведення)."""
    global _warmer
    _warmer = fn


def warm() -> None:
    """Порахувати типові ключі фоном — щоб перший перегляд після кроку циклу не платив."""
    if _warmer is not None:
        BACKGROUND.submit("warm-lists", _warmer)


def _lists_changed() -> None:
    LIST_IDS.clear()
    STATS.clear()
    warm()


GENERATIONS.on_change("lists", _lists_changed)


def poll() -> None:
    """Тік фонового потоку сайту (web/deferred.py): прочитати покоління."""
    GENERATIONS.poll()


def snapshot() -> dict:
    """Діагностика для /api/status/speed і тестів."""
    return {"generations": dict(GENERATIONS.values or {}), "owner_gen": OWNER.value,
            "list_ids": {"keys": len(LIST_IDS), "hits": LIST_IDS.hits,
                         "misses": LIST_IDS.misses},
            "stats": {"keys": len(STATS), "hits": STATS.hits, "misses": STATS.misses},
            "background": {"done": BACKGROUND.done, "failed": BACKGROUND.failed}}
