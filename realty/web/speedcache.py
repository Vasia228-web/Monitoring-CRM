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
    for memo in (LIST_IDS, STATS, FACETS, FACET_OPTIONS, PLACE_MAP, FLAGS):
        memo.clear()
    warm()


def data_key(session) -> tuple:
    """Ключ версії даних, від якої залежать список і зведення.

    Запит, що бере з кешу кілька пов'язаних значень (список id, лічильники фільтрів
    місця, варіанти), читає ключ ОДИН раз і передає його всім (`dk=`): інакше між
    двома читаннями фоновий потік міг побачити нове покоління, і лічильники зі старого
    списку лягли б під новий ключ — «число біля варіанта ≠ видачі» на ціле покоління
    (рецензія E10). Значення зі старіших даних тепер може потрапити лише під старіший
    ключ."""
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

    def peek(self, key: Hashable, *, max_age_s: float):
        """Значення з кешу без обчислення (None — немає або застаріло)."""
        with self._lock:
            entry = self._data.get(key)
            if entry is not None and self._clock() - entry[0] <= max_age_s:
                return entry[1]
        return None

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


LIST_IDS = Memo("list_ids")
STATS = Memo("stats")
# Лічильники біля варіантів «Район»/«ЖК»/«тільки місто» (Блок 4, E10, D57): ОДИН GROUP BY
# над вибіркою без фільтрів місця — раз на покоління даних і фільтр (як і список id).
FACETS = Memo("facets")
# Готові варіанти фільтрів (числа біля кожного) — для пари «групи + вибрані значення».
FACET_OPTIONS = Memo("facet_options")
# id оголошення → (row_district, row_complex, row_area) рядків, які можуть бути в списку
# (чисті й актуальні), — раз на покоління, ЛИШЕ фоном (прохід покривного
# ix_listings_place, ≈10 мс на M4, 2,2 МБ): з неї лічильники списку без фільтрів місця
# рахуються за готовими id (≈0,6 мс). На шляху запиту карта не будується ніколи (рецензія
# E10: холодний «/» платив 13–17 мс за карту): немає — лічильники з того самого проходу,
# що й список id (row_* поруч з id, +2–4 мс), або один GROUP BY, а карта будується у фоні
# із затримкою — після відповіді, а не паралельно з нею. Кортежі інтерновані; один ключ.
PLACE_MAP = Memo("place_map")
# Дрібні ознаки даних за поколінням (напр. «райони й ЖК уже визначались» — до першого
# `places assign` сайт показує сирий район, як до E10).
FLAGS = Memo("flags")


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


def list_ids(session, key: tuple, compute: Callable[[], Iterable[int]], *,
             dk: tuple | None = None) -> array:
    """Упорядковані id рядків списку для фільтра `key` — з кешу або живим запитом.
    `dk` — ключ даних, прочитаний запитом один раз (див. data_key)."""
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
    return LIST_IDS.get((dk or data_key(session), key), run, max_keys=max_keys,
                        max_age_s=max_age)


def facets(session, key: tuple, compute: Callable[[], list], *,
           dk: tuple | None = None) -> list:
    """Групи (row_district, row_complex, row_area, n) вибірки без фільтрів місця.

    `key` — list_key вибірки з порожніми district/complex/area: лічильники однакові
    для всіх варіантів району при тих самих інших фільтрах. Значення спільне — не
    змінювати.
    """
    enabled, max_keys, max_age = _memo_settings()
    if not enabled:
        return compute()
    return FACETS.get((dk or data_key(session), key), lambda: tuple(compute()),
                      max_keys=max_keys, max_age_s=max_age)


def place_map_peek(dk: tuple) -> dict | None:
    """Карта id → row_* для ключа даних `dk`, якщо вже побудована (без обчислення).
    Кеш вимкнено — None (лічильники тоді — одним GROUP BY)."""
    enabled, _keys, max_age = _memo_settings()
    if not enabled:
        return None
    return PLACE_MAP.peek(dk, max_age_s=max_age)


def place_map_build(dk: tuple | None, build: Callable[[], tuple[tuple, dict]]) -> dict | None:
    """Побудувати карту (лише фон: прогрів і `place_map_schedule`). `build` — (ключ
    даних на момент читання, карта). `dk` задано (прогрів покоління) — карта лише для
    нього: ключ уже інший — не зберігається (інакше витіснила б чинну; один ключ);
    `dk=None` — для покоління, яке побачила сама побудова."""
    enabled, _keys, max_age = _memo_settings()
    if not enabled:
        return None
    if dk is not None:
        got = PLACE_MAP.peek(dk, max_age_s=max_age)
        if got is not None:
            return got
    now_dk, value = build()
    if dk is not None and now_dk != dk:
        return None
    if PLACE_MAP.peek(now_dk, max_age_s=max_age) is not None:
        return PLACE_MAP.peek(now_dk, max_age_s=max_age)
    return PLACE_MAP.get(now_dk, lambda: value, max_keys=1, max_age_s=max_age)


_map_timer_lock = threading.Lock()
_map_timer: threading.Timer | None = None


def place_map_schedule(build: Callable[[], tuple[tuple, dict]], *,
                       delay_s: float = 1.0) -> None:
    """Поставити побудову карти у фон через `delay_s` — після відповіді, яка її
    попросила (паралельно з рендером вона б забирала в запиту GIL: +12 мс на M4 у
    замірі рецензії). Один таймер на раз; карта — для покоління на момент побудови
    (не того, що бачив запит: за секунду воно могло змінитись); однакові завдання не
    дублюються."""
    global _map_timer
    enabled, _keys, _age = _memo_settings()
    if not enabled:
        return
    with _map_timer_lock:
        if _map_timer is not None and _map_timer.is_alive():
            return

        def fire() -> None:
            BACKGROUND.submit("place-map", lambda: place_map_build(None, build))

        _map_timer = threading.Timer(delay_s, fire)
        _map_timer.daemon = True
        _map_timer.start()


def flag(session, name: str, compute: Callable[[], bool], *, dk: tuple | None = None) -> bool:
    """Ознака даних (bool) — раз на покоління."""
    enabled, _keys, max_age = _memo_settings()
    if not enabled:
        return compute()
    return FLAGS.get((dk or data_key(session), name), compute, max_keys=8, max_age_s=max_age)


def facets_peek(key: tuple, dk: tuple):
    """Лічильники вибірки, якщо вже є в кеші (без обчислення), інакше None."""
    enabled, _keys, max_age = _memo_settings()
    if not enabled:
        return None
    return FACETS.peek((dk, key), max_age_s=max_age)


def cached_list_ids(session, key: tuple, *, dk: tuple | None = None):
    """Список id фільтра, якщо він уже є в кеші (без обчислення), інакше None."""
    enabled, _keys, max_age = _memo_settings()
    if not enabled:
        return None
    return LIST_IDS.peek((dk or data_key(session), key), max_age_s=max_age)


def facet_options(session, key: tuple, compute: Callable[[], dict], *,
                  dk: tuple | None = None) -> dict:
    """Варіанти фільтрів місця з числами (places.facets.options) — раз на покоління й
    вибір (інакше кожен теплий перегляд «/» перераховував би їх, ~0,6 мс на M4).
    Значення спільне — не змінювати."""
    enabled, max_keys, max_age = _memo_settings()
    if not enabled:
        return compute()
    return FACET_OPTIONS.get((dk or data_key(session), key), compute, max_keys=max_keys,
                             max_age_s=max_age)


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
    FACETS.clear()
    FACET_OPTIONS.clear()
    PLACE_MAP.clear()
    FLAGS.clear()
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
            "facets": {"keys": len(FACETS), "hits": FACETS.hits, "misses": FACETS.misses,
                       "place_map": len(PLACE_MAP)},
            "background": {"done": BACKGROUND.done, "failed": BACKGROUND.failed}}
