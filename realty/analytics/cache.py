"""Знімок «Аналітики»: живе за поколінням даних і перебудовується фоном (Блок 2, E5).

Знімок (`Universe` + зведення джерел і сегментів) будується з усієї бази: 333 мс
на M4, ≈3–5 с на Fedora. Досі він жив 600 с, а на КОЖЕН запит перевірялась
його «версія» повним проходом таблиці (кількість оголошень і max(last_seen) —
17 мс на M4); версія змінювалась за кожним кроком збору, тож перше відкриття
«Аналітики» або картки під час циклу будувало знімок прямо в запиті під
глобальним замком (холодний запит ≈5 с на Fedora, D49), а покинутий запит
затримував наступні.

Тепер (план Блоку 2, D48; інтеграція — «лише покоління»):
  * знімок прив'язаний до покоління «analytics» в ops.db (webcache.py): його
    збільшує диригент після кроку «дублі» й у кінці циклу, а також `cli.py
    dedup`. Нове покоління — перебудова у ФОНОВОМУ потоці; поки вона йде,
    запити отримують попередній знімок (single-flight: одна перебудова за раз);
  * запасний ліміт віку `analytics.max_age_h` (config/speed.toml) — якщо цикли
    стали й покоління не змінюється;
  * перший знімок процесу (старт сайту, інша база в тестах) будується в
    запиті — чекати нічого іншого немає;
  * кнопки «розділити/злити» лагодять знімок точково (`patch_properties`): нова
    квартира відкривається одразу, а не 404 до перебудови; зведення
    перераховуються фоном;
  * `invalidate()` — забути знімок (наступний запит збудує заново);
    `get(force=True)` — збудувати зараз (cli.py, тести).

«Днів на ринку» відраховуються від моменту побудови знімка (≤ одного циклу,
≤ `max_age_h`), а не від останніх ≤10 хв, як було з TTL — на тих самих даних із
тим самим «зараз» вивід той самий (scripts/page_equality.py).
"""
from __future__ import annotations

import logging
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .. import configfiles, txnwatch, webcache
from .segments import Universe, below_threshold, build_items, build_universe, segment_table
from .settings import load
from .sources import composition, matched

log = logging.getLogger(__name__)

# Скільки мс цей запит сайту витратив на перебудову знімка (Блок 2, D49). Сайт
# (web/perf.py) кладе сюди порожній список на початку запиту, а `get`
# дописує час перебудови. Так «холодний» запит (знімок будувався в ньому)
# видно в заголовку Server-Timing (`snapshot;dur=…`), у журналі часу й у зонді
# окремо від теплих. Фонова перебудова сюди не пише: вона не в запиті.
BUILD_MS: ContextVar[list | None] = ContextVar("analytics_build_ms", default=None)


@dataclass
class Snapshot:
    version: tuple                 # (база, покоління «analytics») — для діагностики
    built_at: float
    universe: Universe
    segments: list[dict]
    below: dict
    sources_matched: dict
    sources_composition: list[dict]
    db_url: str = ""
    generation: int = 0

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.built_at


def _url(session) -> str:
    return txnwatch._url_key(session.get_bind())


def _build(session, generation: int) -> Snapshot:
    cfg = load()
    universe = build_universe(session)
    url = _url(session)
    return Snapshot(
        version=(url, generation), built_at=time.monotonic(), universe=universe,
        segments=segment_table(universe, cfg), below=below_threshold(universe, cfg),
        sources_matched=matched(session, cfg), sources_composition=composition(session),
        db_url=url, generation=generation,
    )


def _max_age_s() -> float | None:
    try:
        return configfiles.get("speed").analytics.max_age_h * 3600
    except configfiles.ConfigError as e:
        log.error("config/speed.toml не читається — знімок «Аналітики» без ліміту віку: %s", e)
        return None


class AnalyticsHolder:
    """Поточний знімок + одна фонова перебудова за раз."""

    def __init__(self) -> None:
        self._lock = threading.Lock()            # короткі операції з полями
        self._build_lock = threading.Lock()      # синхронна побудова (одна за раз)
        self._current: Snapshot | None = None
        self._bind = None                        # рушій, з якого збудовано поточний знімок
        self._rebuilding = False
        # Квартири, оновлені точково, поки йде фонова перебудова: її знімок
        # читав базу ДО (або під час) «розділити/злити», тож ці квартири в ньому
        # оновлюються ще раз перед підміною — інакше точкове оновлення загубилось би.
        self._patched_during: set[int] | None = None
        self._rebuild_thread: threading.Thread | None = None
        self.builds = 0
        self.background_builds = 0
        self.patches = 0

    # --- читання ---------------------------------------------------------------------------

    def current(self) -> Snapshot | None:
        with self._lock:
            return self._current

    def get(self, session, *, force: bool = False) -> Snapshot:
        """Знімок для запиту: наявний (і фонова перебудова, якщо застарів) або новий."""
        url = _url(session)
        if not force:
            snap = self.current()
            if snap is not None and snap.db_url == url:
                if self._stale(snap):
                    self.request_rebuild(session.get_bind())
                return snap
        return self._build_now(session, url, force=force)

    def _stale(self, snap: Snapshot) -> bool:
        if webcache.GENERATIONS.current("analytics") != snap.generation:
            return True
        limit = _max_age_s()
        return limit is not None and snap.age_seconds > limit

    def _build_now(self, session, url: str, *, force: bool) -> Snapshot:
        with self._build_lock:
            # Хтось інший уже збудував, поки ми чекали замка — беремо його.
            snap = self.current()
            if not force and snap is not None and snap.db_url == url:
                return snap
            started = time.perf_counter()
            generation = webcache.GENERATIONS.current("analytics")
            snap = _build(session, generation)
            with self._lock:
                self._current = snap
                self._bind = session.get_bind()
                self.builds += 1
            box = BUILD_MS.get()
            if box is not None:
                box.append((time.perf_counter() - started) * 1000)
            return snap

    # --- фонова перебудова -------------------------------------------------------------------

    def request_rebuild(self, bind) -> bool:
        """Перебудувати фоном; поки йде перебудова — віддається попередній знімок."""
        with self._lock:
            if self._rebuilding:
                return False
            self._rebuilding = True
            thread = threading.Thread(target=self._rebuild, args=(bind,),
                                      name="realty-analytics-rebuild", daemon=True)
            self._rebuild_thread = thread
        thread.start()
        return True

    def _rebuild(self, bind) -> None:
        try:
            with self._lock:
                self._patched_during = set()
            generation = webcache.GENERATIONS.current("analytics")
            with Session(bind=bind) as s:
                snap = _build(s, generation)
                with self._lock:
                    late = set(self._patched_during or ())
                if late:
                    snap = _patched(snap, build_items(s, late, now=snap.universe.built_at),
                                    late)
            with self._lock:
                late_again = set(self._patched_during or ()) - late
                # Інша база (тести) за час перебудови — не підміняємо. Точкові
                # оновлення, що встигли між читанням і підміною, — наступною перебудовою.
                if self._current is None or self._current.db_url == snap.db_url:
                    self._current = snap
                    self._bind = bind
                self.background_builds += 1
            if late_again:
                log.info("точкові оновлення під час перебудови: %s — ще одна перебудова",
                         sorted(late_again))
        except Exception as e:                      # noqa: BLE001 — лишається попередній
            late_again = set()
            log.warning("фонова перебудова знімка «Аналітики» не вдалась: %s", e)
        finally:
            with self._lock:
                self._rebuilding = False
                self._patched_during = None
        if late_again:
            self.request_rebuild(bind)

    def wait_rebuild(self, timeout: float | None = None) -> bool:
        """Дочекатися фонової перебудови (тести й cli). True — завершилась."""
        with self._lock:
            thread = self._rebuild_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # --- точкові зміни -------------------------------------------------------------------

    def patch_properties(self, session, property_ids, *, rebuild: bool = True) -> bool:
        """Оновити в знімку лише ці квартири (після «розділити/злити»).

        Рядки квартир будуються тим самим кодом, що й повний знімок, з тим самим
        «зараз» (`universe.built_at`), і стають на своє місце за id — тож
        `items` такі самі, як у повністю перебудованого знімка (тест
        test_universe_patch_equals_rebuild). Квартири, якої вже немає (злилась),
        зі знімка прибирається. Зведення (сегменти, джерела) лишаються від
        попереднього знімка до фонової перебудови, яку тут же й замовляємо.
        Новий об'єкт знімка, а не зміна наявного на місці: запити, що саме
        зараз читають старий, дочитають його цілим, а пам'ять кривих — нова.
        `rebuild=False` — без фонової перебудови (сторінка квартири, якої знімок
        ще не знає: перебудову після кроку «дублі» й так замовить покоління).
        """
        url = _url(session)
        ids = {int(i) for i in property_ids}
        with self._lock:
            snap = self._current
            if self._patched_during is not None:
                self._patched_during |= ids
        if snap is None or snap.db_url != url:
            return False
        patched = _patched(snap, build_items(session, ids, now=snap.universe.built_at), ids)
        with self._lock:
            if self._current is snap:
                self._current = patched
                self.patches += 1
        if rebuild:
            self.request_rebuild(session.get_bind())
        return True

    def invalidate(self) -> None:
        """Забути знімок: наступний запит збудує новий (тести, зміна налаштувань)."""
        with self._lock:
            self._current = None


def _patched(snap: Snapshot, fresh: list, ids: set[int]) -> Snapshot:
    """Знімок, у якому рядки квартир `ids` замінено на `fresh` (на місці за id)."""
    items = [o for o in snap.universe.items if o.property_id not in ids] + fresh
    items.sort(key=lambda o: o.property_id)
    return Snapshot(
        version=snap.version, built_at=snap.built_at,
        universe=Universe(items=items, built_at=snap.universe.built_at),
        segments=snap.segments, below=snap.below, sources_matched=snap.sources_matched,
        sources_composition=snap.sources_composition, db_url=snap.db_url,
        generation=snap.generation)


HOLDER = AnalyticsHolder()


def get(session, *, force: bool = False) -> Snapshot:
    """Знімок для запиту — див. AnalyticsHolder.get."""
    return HOLDER.get(session, force=force)


def invalidate() -> None:
    HOLDER.invalidate()


def patch_properties(session, property_ids, *, rebuild: bool = True) -> bool:
    return HOLDER.patch_properties(session, property_ids, rebuild=rebuild)


def _on_new_generation() -> None:
    """Нове покоління «analytics» від фонового потоку сайту — перебудова одразу,
    не чекаючи першого запиту (з тієї самої бази, з якої збудовано знімок)."""
    with HOLDER._lock:
        bind = HOLDER._bind if HOLDER._current is not None else None
    if bind is not None:
        HOLDER.request_rebuild(bind)


webcache.GENERATIONS.on_change("analytics", _on_new_generation)
