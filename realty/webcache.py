"""Покоління даних для кешів сайту — спільне для диригента, cli.py і сайту (Блок 2, E5).

Навіщо. Кеші сайту (список id для фільтра, зведення над списком, знімок
«Аналітики») мають оновлюватись після кожного кроку циклу збору, а не на
кожен запит повним проходом таблиці (досі «Аналітика» робила 17-мс повний
прохід на КОЖЕН запит лише щоб дізнатися, чи не змінились дані; план Блоку 2,
D48). Тому той, хто змінив дані, сам каже про це одним рядком у ops.db:

  * «lists» — список, лічильник «за фільтром», нумерація сторінок, зведення:
    диригент після КОЖНОГО кроку циклу (runner.py) і будь-який процес cli.py,
    що зафіксував запис у realty.db, при виході (txnwatch.install_autobump);
  * «analytics» — знімок «Аналітики»: після кроку «дублі» й у кінці циклу,
    після `cli.py dedup` (рішення власника, D46: «кеш оновлюється після циклу»).

Сайт читає таблицю фоном раз на `generations.poll_s` (web/speedcache.py). Дії
власника в самому сайті сюди не пишуться: сайт бачить їх у своєму процесі
одразу (txnwatch.data_version + speedcache.owner_changed).

Збій запису покоління не має ламати крок циклу: тоді кеш сайту оновиться за
запасним лімітом віку (`list_memo.max_age_s`, `analytics.max_age_h`).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from . import configfiles, ops

log = logging.getLogger(__name__)

SCOPES = ("lists", "analytics")

# Аварійне значення `generations.poll_s`, лише коли config/speed.toml не
# читається (зламаний файл на старті сайту): без нього фоновий потік сайту
# зупинився б, а покоління ніхто б не перечитував до перезапуску. Те саме
# число, що в config/speed.toml; тест стежить, щоб вони не розійшлися.
EMERGENCY_POLL_S = 5.0


def bump(scope: str, reason: str | None = None) -> bool:
    """Покоління `scope` + 1. True — записано; помилка — лише в журнал."""
    if scope not in SCOPES:
        raise ValueError(f"невідоме покоління: {scope}")
    now = ops._now()
    reason = (reason or "")[:96] or None
    try:
        ops.init_ops()
        stmt = insert(ops.WebGeneration).values(name=scope, gen=1, changed_at=now,
                                                reason=reason)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ops.WebGeneration.name],
            set_={"gen": ops.WebGeneration.gen + 1, "changed_at": now, "reason": reason})
        with ops.ops_session() as s:
            s.execute(stmt)
        return True
    except Exception as e:                            # noqa: BLE001 — крок циклу важливіший
        log.warning("покоління кешу сайту «%s» не збільшено (%s): %s", scope, reason, e)
        return False


def read() -> dict[str, int]:
    """{scope: покоління}; відсутній рядок — 0 (ще ніхто не збільшував)."""
    ops.init_ops()
    with ops.ops_session() as s:
        rows = dict(s.execute(select(ops.WebGeneration.name, ops.WebGeneration.gen)).all())
    return {scope: int(rows.get(scope) or 0) for scope in SCOPES}


def describe() -> list[dict]:
    """Для `cli.py webcache show`: покоління, коли й чому змінювалось."""
    ops.init_ops()
    with ops.ops_session() as s:
        rows = s.scalars(select(ops.WebGeneration).order_by(ops.WebGeneration.name)).all()
        return [{"name": r.name, "gen": r.gen, "changed_at": ops.as_utc_iso(r.changed_at),
                 "reason": r.reason} for r in rows]


# --- Покоління в пам'яті сайту ------------------------------------------------------------


class Generations:
    """Покоління з ops.db (webcache), прочитані фоновим потоком.

    Запит лише читає поля в пам'яті. Якщо фонового потоку немає (тести без
    lifespan, перші секунди після старту) — читає сам, не частіше ніж раз на
    `generations.poll_s`: один рядок за первинним ключем.
    """

    def __init__(self, reader: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._reader = reader or read
        self._clock = clock
        self._lock = threading.Lock()
        self.values: dict[str, int] | None = None
        self.read_at: float | None = None
        self._listeners: dict[str, list[Callable[[], None]]] = {}

    def on_change(self, scope: str, fn: Callable[[], None]) -> None:
        self._listeners.setdefault(scope, []).append(fn)

    def poll(self) -> list[str]:
        """Прочитати ops.db; повертає, які покоління змінились (і сповіщає)."""
        try:
            fresh = self._reader()
        except Exception as e:                      # noqa: BLE001 — лишаємо попередні
            log.warning("покоління кешу не прочитано: %s", e)
            return []
        with self._lock:
            before = self.values
            self.values, self.read_at = fresh, self._clock()
        changed = [k for k, v in fresh.items() if before is not None and before.get(k) != v]
        for scope in changed:
            for fn in self._listeners.get(scope, ()):
                try:
                    fn()
                except Exception as e:              # noqa: BLE001
                    log.warning("реакція на нове покоління %s не вдалась: %s", scope, e)
        return changed

    def current(self, scope: str) -> int:
        try:
            poll_s = configfiles.get("speed").generations.poll_s
        except configfiles.ConfigError:
            poll_s = EMERGENCY_POLL_S
        with self._lock:
            values, read_at = self.values, self.read_at
        stale = values is None or (poll_s is not None and read_at is not None
                                   and self._clock() - read_at > 2 * poll_s)
        if stale:
            self.poll()
            with self._lock:
                values = self.values
        return (values or {}).get(scope, 0)


GENERATIONS = Generations()
