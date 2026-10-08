"""Телеметрія роботи системи: прогони, запити, серцебиття воркера.

Свідомо окрема база (`data/ops.db`): дані спостереження за краулером не мають
змішуватись із самими оголошеннями. Основна база лишається чистою, її можна
перебудувати чи перенести, не втрачаючи історії роботи — і навпаки.
"""
from __future__ import annotations

import os
import threading
import weakref
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, Index, Integer, String, Text, create_engine, event, func,
    inspect, select, text,
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
    # Зібрано, але не записано через помилку бази — окремо від «відхилено
    # карантином». 20.09 саме це й мовчало добу.
    skipped: Mapped[int] = mapped_column(Integer, default=0)
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
    # Звірка відповіді моделі з парсером там, де обидва щось дали.
    llm_agreed: Mapped[int] = mapped_column(Integer, default=0)
    llm_disagreed: Mapped[int] = mapped_column(Integer, default=0)
    llm_uncomparable: Mapped[int] = mapped_column(Integer, default=0)

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


class CycleRecord(OpsBase):
    """Один регулярний цикл цілком: збір по всіх джерелах і службові кроки.

    Сигнал тиші дивиться саме сюди: «коли востаннє був УСПІШНИЙ цикл».
    Успішний — той, що зібрав хоч одне оголошення. Живий процес, який нічого
    не приніс, — теж аварія.
    """

    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")
    host: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    kept: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    sources_ok: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    pid: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[str | None] = mapped_column(Text)        # JSON: кроки з тривалістю


class DedupAudit(OpsBase):
    """Самоперевірка зведення після кроку «дублі» (D41): протиріччя всередині
    квартир і пропущені дублі. Списки — JSON, щоб /status показав чергу."""

    __tablename__ = "dedup_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    rules: Mapped[str | None] = mapped_column(String(200))
    properties: Mapped[int] = mapped_column(Integer, default=0)     # квартир з 2+ оголошень
    suspicious: Mapped[int] = mapped_column(Integer, default=0)
    missed: Mapped[int] = mapped_column(Integer, default=0)
    by_kind: Mapped[str | None] = mapped_column(Text)               # JSON {вид: квартир}
    queue: Mapped[str | None] = mapped_column(Text)                 # JSON [{property_id, kinds, n}]
    missed_list: Mapped[str | None] = mapped_column(Text)           # JSON [{kind, key, properties}]


class DedupSample(OpsBase):
    """Щотижнева перевірка 20 випадкових квартир (D41) — частка помилок зведення."""

    __tablename__ = "dedup_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    one: Mapped[int] = mapped_column(Integer, default=0)
    several: Mapped[int] = mapped_column(Integer, default=0)
    unclear: Mapped[int] = mapped_column(Integer, default=0)
    error_share: Mapped[float | None] = mapped_column(Float)
    details: Mapped[str | None] = mapped_column(Text)               # JSON по кожній квартирі


class WebTiming(OpsBase):
    """Скільки сервер відповідав на один запит сайту (Блок 2, D48).

    Пишеться пакетами фоном (`web/deferred.py`), а не в самому запиті. Хто
    саме дивився — не зберігається: ні IP, ні User-Agent, лише роль. Маршрут —
    шаблон (`/property/{property_id}`), а не адреса з номером квартири.
    """

    __tablename__ = "web_timings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    route: Mapped[str] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(8))
    status: Mapped[int] = mapped_column(Integer)
    ms: Mapped[float] = mapped_column(Float)           # до початку відповіді (Server-Timing)
    bytes: Mapped[int | None] = mapped_column(Integer)
    cycle_active: Mapped[bool] = mapped_column(Boolean, default=False)
    cycle_step: Mapped[str | None] = mapped_column(String(96))
    role: Mapped[str | None] = mapped_column(String(16))
    config_hash: Mapped[str | None] = mapped_column(String(16))   # версія config/speed.toml
    # Скільки з `ms` пішло на перебудову знімка «Аналітики» в цьому ж запиті;
    # None — знімок був готовий (теплий запит). Без цього холодні запити (≈5 с
    # на Fedora) змішувались би з теплими (≈0,7 с) в одне p95 (D49).
    snapshot_ms: Mapped[float | None] = mapped_column(Float)


class WebRum(OpsBase):
    """Час переходу між сторінками, як його побачив браузер (маячок RUM, Блок 2).

    Ціль власника (D46 п. 6) — перехід між вкладками з телефона на вже
    відкритому з'єднанні ≤1,5 с — видно лише в браузері. Без IP і без рядка
    User-Agent: лише роль і «телефон/комп'ютер».
    """

    __tablename__ = "web_rum"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    route: Mapped[str] = mapped_column(String(128))
    nav_type: Mapped[str | None] = mapped_column(String(16))   # navigate | reload | back_forward
    reused: Mapped[bool | None] = mapped_column(Boolean)      # з'єднання вже було відкрите
    ttfb_ms: Mapped[int | None] = mapped_column(Integer)
    dcl_ms: Mapped[int | None] = mapped_column(Integer)
    load_ms: Mapped[int | None] = mapped_column(Integer)
    transfer_bytes: Mapped[int | None] = mapped_column(Integer)
    server_ms: Mapped[float | None] = mapped_column(Float)    # Server-Timing цієї ж сторінки
    proto: Mapped[str | None] = mapped_column(String(16))
    device: Mapped[str | None] = mapped_column(String(8))     # mobile | desktop
    role: Mapped[str | None] = mapped_column(String(16))
    resources: Mapped[str | None] = mapped_column(Text)       # JSON [[маршрут /api, мс], …]
    config_hash: Mapped[str | None] = mapped_column(String(16))


class WriteWindow(OpsBase):
    """Найдовше вікно транзакції запису в realty.db одного процесу кроку циклу.

    Поки процес тримає транзакцію запису, кнопки власника чекають (на Fedora
    виміряно 8,9 с під блокуванням кроку «дублі», D48). `realty/txnwatch.py`
    міряє від першої зміни в транзакції до commit/rollback — щоб лікувати
    довгі блокування за виміром, а не наосліп.
    """

    __tablename__ = "write_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    process: Mapped[str | None] = mapped_column(String(64))    # команда cli.py
    step: Mapped[str | None] = mapped_column(String(96))       # крок циклу, якщо є
    max_ms: Mapped[float] = mapped_column(Float, default=0.0)
    total_ms: Mapped[float] = mapped_column(Float, default=0.0)
    txns: Mapped[int] = mapped_column(Integer, default=0)
    max_sql: Mapped[str | None] = mapped_column(String(160))   # з чого почалось найдовше вікно
    seconds: Mapped[float | None] = mapped_column(Float)       # скільки жив процес


class WebGeneration(OpsBase):
    """Покоління даних для кешів сайту (Блок 2, крок E5, D50).

    «lists» — список, лічильники, зведення: збільшує диригент після КОЖНОГО
    кроку циклу і будь-який процес, що зафіксував запис у realty.db, при виході
    (`txnwatch.install_autobump`). «analytics» — знімок «Аналітики»: після
    кроку «дублі» й у кінці циклу. Сайт читає таблицю фоном раз на
    `generations.poll_s` і скидає лише кеші, чиє покоління змінилось — без
    повних проходів таблиці на кожен запит.
    """

    __tablename__ = "web_generations"

    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    gen: Mapped[int] = mapped_column(Integer, default=0)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reason: Mapped[str | None] = mapped_column(String(96))


class LookupCheck(OpsBase):
    """Черга перевірок актуальності «на вимогу» (Блок 2 E5; спільна з Блоком 5).

    Сайт сам у мережу не ходить (інтеграція, конфлікт «перевірка під час
    відкриття»): відкриття квартири ставить сюди завдання `kind="opened"`, а
    виконує його окремий короткий процес (`cli.py lookup check --job N`,
    шаблон systemd realty-lookup@). Сторінка дізнається результат long-poll'ом
    GET /api/property/{id}/liveness. Блок 5 додасть свої колонки й `kind`.
    Ні IP, ні рядка браузера тут немає.
    """

    __tablename__ = "lookup_checks"
    __table_args__ = (Index("ix_lookup_key_created", "key", "created_at"),
                      Index("ix_lookup_state", "state"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))                # opened (Блок 5: lookup)
    key: Mapped[str] = mapped_column(String(64))                 # «property:123»
    property_id: Mapped[int | None] = mapped_column(Integer)
    # queued → running → done | failed; skipped — нічого перевіряти (усе вже
    # перевірене менш ніж opened_recheck_minutes тому або немає що перевіряти,
    # чи збір на машині вимкнено — COLLECTOR_OFF). deferred — замок циклу
    # зайнятий: перевірка (і мережа, і запис) — після нього (інтеграція,
    # конфлікт 5), далі знову queued → running → …
    state: Mapped[str] = mapped_column(String(12), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    pid: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[str | None] = mapped_column(Text)             # JSON: checked/delisted/…


class LivenessRun(OpsBase):
    """Один прогін перевірки актуальності (Блок 1, E8, D52).

    Крок циклу, завдання «перевірка при відкритті», ручний запуск. Наприкінці
    прогону циклу сюди ж пишеться готове зведення для /status (`report`, JSON):
    сторінка й /api/status/liveness читають цей рядок, а не агрегують check_events
    на кожен запит (інтеграція, конфлікт 9). `config_hash` — версія
    config/liveness.toml, з якою прогін працював.
    """

    __tablename__ = "liveness_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="cycle")   # cycle|opened|manual|explicit
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    config_hash: Mapped[str | None] = mapped_column(String(16))
    fuse_mode: Mapped[str | None] = mapped_column(String(16))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    checked: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    returned: Mapped[int] = mapped_column(Integer, default=0)
    per_host: Mapped[str | None] = mapped_column(Text)        # JSON
    per_source: Mapped[str | None] = mapped_column(Text)      # JSON
    per_tier: Mapped[str | None] = mapped_column(Text)        # JSON
    fuse: Mapped[str | None] = mapped_column(Text)            # JSON: спрацювання прогону
    report: Mapped[str | None] = mapped_column(Text)          # JSON: зведення для /status
    message: Mapped[str | None] = mapped_column(Text)


class LivenessFuse(OpsBase):
    """Запобіжник перевірки актуальності — по джерелу (Блок 1, E8, D52).

    `held` — для джерела нічого не знімаємо й не повертаємо, доки власник не зніме
    запобіжник на /status (POST /api/status/liveness-fuse) чи `cli.py liveness
    fuse clear`. Тривогу в Telegram шле сторож (watchdog.check_liveness), поки
    джерело тримається. `examples` — до 10 безпечних адрес (без query й піддоменів).
    """

    __tablename__ = "liveness_fuse"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(8), default="held")      # held | clear
    mode: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(24))      # share | hinted_share | canary
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime)
    run_id: Mapped[int | None] = mapped_column(Integer)
    checked: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    share: Mapped[float | None] = mapped_column(Float)
    examples: Mapped[str | None] = mapped_column(Text)          # JSON
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime)
    cleared_by: Mapped[str | None] = mapped_column(String(32))


class LivenessFuseLog(OpsBase):
    """Журнал запобіжника, що лише дописується: кожне спрацювання й кожне зняття.

    `liveness_fuse` — поточний стан (нове спрацювання переписує його рядок); тут —
    історія рішень «тримати → власник відпустив → знову тримати» з тим, хто й коли
    відпустив (правило ескалації; рецензія E8, D52).
    """

    __tablename__ = "liveness_fuse_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(8))                     # trip | clear
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    by: Mapped[str | None] = mapped_column(String(32))                 # clear: хто відпустив
    run_id: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(24))
    checked: Mapped[int | None] = mapped_column(Integer)
    removed: Mapped[int | None] = mapped_column(Integer)
    share: Mapped[float | None] = mapped_column(Float)
    examples: Mapped[str | None] = mapped_column(Text)                 # JSON


class NightRun(OpsBase):
    """Одне нічне вікно диригента `cli.py night` (E9, D53).

    Статус: running | ok | partial (смугу зупинено блокуваннями чи дедлайном не все
    встигли — це норма) | backup_failed (нічого не писали) | lock_timeout (цикл не
    звільнив замка) | outside_window | disabled (COLLECTOR_OFF) | failed (виняток).
    Часи — UTC без зони, як і решта ops.db; `night_date` — місцева дата ночі (два
    вікна однієї ночі мають ту саму). JSON-поля — план, смуги, пакети, підсумки по
    хостах, строк продажу до/після; `liveness_run_id` — рядок ops.liveness_runs
    (kind «night») із тими самими підсумками й зведенням для /status.
    """

    __tablename__ = "night_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    night_date: Mapped[str | None] = mapped_column(String(10), index=True)
    window: Mapped[str | None] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    stop_requests_at: Mapped[datetime | None] = mapped_column(DateTime)
    release_lock_at: Mapped[datetime | None] = mapped_column(DateTime)
    lock_acquired_at: Mapped[datetime | None] = mapped_column(DateTime)
    lock_released_at: Mapped[datetime | None] = mapped_column(DateTime)
    lock_waited_s: Mapped[float] = mapped_column(Float, default=0.0)
    config_hash: Mapped[str | None] = mapped_column(String(16))
    liveness_hash: Mapped[str | None] = mapped_column(String(16))
    fuse_mode: Mapped[str | None] = mapped_column(String(16))
    liveness_run_id: Mapped[int | None] = mapped_column(Integer)
    backup: Mapped[str | None] = mapped_column(Text)          # JSON
    plan: Mapped[str | None] = mapped_column(Text)            # JSON: хост → яруси, темп
    lanes: Mapped[str | None] = mapped_column(Text)           # JSON: хост → запити, зупинки
    batches: Mapped[str | None] = mapped_column(Text)         # JSON: список пакетів
    per_host: Mapped[str | None] = mapped_column(Text)        # JSON: хост → наслідки
    per_tier: Mapped[str | None] = mapped_column(Text)        # JSON
    fuse: Mapped[str | None] = mapped_column(Text)            # JSON: спрацювання ночі
    identity: Mapped[str | None] = mapped_column(Text)        # JSON: дозбір по смугах
    active_before: Mapped[int | None] = mapped_column(Integer)
    active_after: Mapped[int | None] = mapped_column(Integer)
    # JSON {"before"|"after": {active, listings, price_events}} — звірка ночі: дозбір
    # identity LUN/flombu — це збір стрічки (нові оголошення, події ціни), рецензія E9.
    totals: Mapped[str | None] = mapped_column(Text)
    liquidity_before: Mapped[str | None] = mapped_column(Text)  # JSON
    liquidity_after: Mapped[str | None] = mapped_column(Text)   # JSON
    message: Mapped[str | None] = mapped_column(Text)


class NightHold(OpsBase):
    """Хост, чию нічну смугу зупиняли блокування дві ночі поспіль: чекає рішення
    власника (інтеграція, D47: «повторилось наступної ночі — чекати рішення»).
    Знімає `cli.py night unhold --host …`; поки тримається — смуги хоста немає,
    сторож нагадує (watchdog.check_night)."""

    __tablename__ = "night_holds"

    host: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(8), default="held")      # held | clear
    since: Mapped[datetime] = mapped_column(DateTime, default=_now)
    reason: Mapped[str | None] = mapped_column(Text)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime)
    cleared_by: Mapped[str | None] = mapped_column(String(32))


SUCCESS_STATUSES = ("ok", "partial")

# Скільки чекати зайняту ops.db. Фоновий запис сайту ставить собі коротший
# тайм-аут на свою транзакцію й повертає це значення назад (web/deferred.py).
BUSY_TIMEOUT_MS = 30_000

engine = create_engine(OPS_DB_URL, future=True)


@event.listens_for(engine, "connect")
def _tune_ops_sqlite(dbapi_connection, _record) -> None:
    """Те саме, що для основної бази: телеметрію пишуть кілька процесів одразу."""
    cur = dbapi_connection.cursor()
    try:
        cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA journal_mode = WAL")
    finally:
        cur.close()
OpsSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# Для яких рушіїв схему ops.db уже перевірено в цьому процесі — і скільки таблиць
# тоді знала модель (Блок 2, D49). init_ops() викликається перед кожним
# зверненням до ops.db: на кожен запит того, хто ввійшов (перевірка сесії), і
# 11 разів на /api/status. Кожен раз — create_all і перевірка колонок, по 3
# PRAGMA на таблицю: з новими таблицями Блоку 2 — 396 PRAGMA на один
# /api/status із кукою. Схема за життя процесу не змінюється, тож досить
# одного разу. Замір D49 (M4, копія Етапу 0): /api/status 44 → 29 мс,
# перевірка сесії 1,24 → 0,15 мс, сторінка /status 2,0 → 0,7 мс. Ключ — сам об'єкт рушія (тести підставляють свій на
# tmp_path — для нього перевірка пройде заново) і кількість таблиць моделі:
# модуль із новою таблицею, імпортований пізніше (команди cli.py імпортують
# модулі всередині команди), теж отримає свою таблицю, як і досі.
_ready: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_ready_lock = threading.Lock()


def init_ops(*, force: bool = False) -> None:
    """Створює відсутні таблиці й колонки ops.db — один раз на процес і рушій.

    `force=True` — перевірити заново (тести, що видаляють таблиці).
    """
    tables = len(OpsBase.metadata.tables)
    current = engine
    if not force and _ready.get(current) == tables:
        return
    with _ready_lock:
        if not force and _ready.get(current) == tables:
            return
        OpsBase.metadata.create_all(current)
        _add_missing_columns()
        _ready[current] = tables


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


def start_cycle(trigger: str, host: str | None = None) -> int:
    init_ops()
    with ops_session() as s:
        c = CycleRecord(trigger=trigger, host=host, status="running", pid=os.getpid())
        s.add(c)
        s.flush()
        return c.id


def finish_cycle(cycle_id: int, **fields) -> None:
    with ops_session() as s:
        c = s.get(CycleRecord, cycle_id)
        if c is None:
            return
        c.finished_at = _now()
        for key, value in fields.items():
            if hasattr(c, key):
                setattr(c, key, value)


def last_cycles(limit: int = 10) -> list[CycleRecord]:
    init_ops()
    with ops_session() as s:
        return list(s.scalars(
            select(CycleRecord).order_by(CycleRecord.started_at.desc()).limit(limit)))


def last_success_at() -> datetime | None:
    """Коли закінчився останній цикл, що справді щось зібрав."""
    init_ops()
    with ops_session() as s:
        return s.scalar(
            select(func.max(CycleRecord.finished_at))
            .where(CycleRecord.status.in_(SUCCESS_STATUSES)))


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


# Підпис кроку в серцебитті (runner.py пише його перед кожним кроком циклу).
STEP_NOTE = "крок: "


def current_cycle(max_age_s: float) -> tuple[bool, str | None]:
    """(чи йде цикл збору, назва кроку) — для журналу часу сайту й зонда (Блок 2).

    «Йде» — є запис циклу «running», молодший за `max_age_s`: цикл, що впав, не
    закриває свого запису, і без стелі віку вважався б живим вічно. Крок —
    із серцебиття, куди диригент перед кожним кроком пише «крок: <назва>».
    Лише читання: два дешеві запити до ops.db.
    """
    since = _now() - timedelta(seconds=max_age_s)
    with ops_session() as s:
        running = s.scalar(select(func.count()).select_from(CycleRecord).where(
            CycleRecord.status == "running", CycleRecord.started_at >= since)) or 0
        hb = s.get(Heartbeat, 1)
        note = hb.note if hb is not None else None
    step = None
    if running and note and note.startswith(STEP_NOTE):
        step = note[len(STEP_NOTE):][:96]
    return bool(running), step


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
        func.sum(RunRecord.llm_agreed), func.sum(RunRecord.llm_disagreed),
        func.sum(RunRecord.llm_uncomparable),
    )
    if hours:
        stmt = stmt.where(RunRecord.started_at >= _now() - timedelta(hours=hours))
    with ops_session() as s:
        calls, tin, tout, cost, passed, failed, agreed, disagreed, blind = s.execute(stmt).one()
    return {"calls": int(calls or 0), "in_tokens": int(tin or 0),
            "out_tokens": int(tout or 0), "cost_usd": round(float(cost or 0), 4),
            "passed": int(passed or 0), "failed": int(failed or 0),
            "agreed": int(agreed or 0), "disagreed": int(disagreed or 0),
            "uncomparable": int(blind or 0)}


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
