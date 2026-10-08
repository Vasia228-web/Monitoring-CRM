"""Конфіги в теці `config/`: один TOML-файл на тему, сувора перевірка, без значень у коді.

Навіщо. Промт 11 вимагає, щоб усі пороги, списки й назви (райони, ЖК, підписи
знятих оголошень, цілі швидкодії, ліміти) жили в конфігах, а не в коді. Досі
кожен блок робив це по-своєму: `data/analytics_settings.json` і план Блоку 2
(`data/speed_settings.json`) лежать поза git, мовчки беруть типові значення й
ігнорують невідомі ключі — помилку в назві ключа там не видно ніколи. Конвенція
одна для всіх блоків (інтеграційне рішення «config_convention», D47 п. 1):

  * тека `config/` у корені репозиторію, у git; один файл на тему
    (`speed.toml`, згодом `liveness.toml`, `places/districts.toml` …);
  * у коді немає значень за замовчуванням: відсутній чи невідомий ключ або
    неправильний тип — `ConfigError`, а не тиха підстановка;
  * довгоживучий процес (сайт) перечитує файл за mtime не частіше ніж раз на
    60 с (`get`); кроки циклу й нічні роботи читають на старті (`load`);
  * у записи прогонів пишеться хеш ЧИННОЇ версії — того самого читання, з
    якого взято значення (`load_with_hash`, `get_with_hash`), щоб будь-яку
    цифру «до/після» можна було прив'язати до версії порогів;
  * перекриття — лише змінна REALTY_CONFIG_DIR (тести й експерименти); у
    журналі — попередження, а `cli.py config check` без --allow-override
    з перекриттям не проходить.

Схема теми — заморожений dataclass без значень за замовчуванням. Обмеження
значень (min/max/choices) — у `field(metadata=…)`; це не значення, а межі.
Перевірка всіх файлів: `cli.py config check` (у процедурі розгортання ДО
перезапуску служб) і тест `tests/test_configs_valid.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
import tomllib
import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ENV_DIR = "REALTY_CONFIG_DIR"
# Не частіше ніж раз на хвилину: stat файлу дешевий, але сайт на слабкому
# ноутбуці не має витрачати на це ні запиту (інтеграція, конвенція п. 3).
RELOAD_INTERVAL_S = 60.0


class ConfigError(Exception):
    """Конфіг відсутній, не читається або не відповідає схемі теми."""


# Попередження про перекриття — один раз на теку, а не на кожен запит сайту.
_warned_override: set[str] = set()


def override_dir() -> str | None:
    """Значення REALTY_CONFIG_DIR, якщо перекриття діє."""
    return os.environ.get(ENV_DIR, "").strip() or None


def config_dir() -> Path:
    """Тека конфігів: `config/` у репозиторії або REALTY_CONFIG_DIR (лише тести).

    Змінну читає й .env (realty/config.py викликає load_dotenv), тож рядок
    REALTY_CONFIG_DIR у .env на Fedora тихо підмінив би конфіг із git для всіх
    служб. Тому перекриття не мовчить: попередження в журналі, а `config check`
    без --allow-override його не пропускає.
    """
    override = override_dir()
    if not override:
        return ROOT / "config"
    if override not in _warned_override:
        _warned_override.add(override)
        log.warning("Конфіги читаються з перекриття %s=%s, а не з config/ у git",
                    ENV_DIR, override)
    return Path(override)


def path_for(name: str) -> Path:
    return config_dir() / f"{name}.toml"


# --- Схеми тем ----------------------------------------------------------------------------
# Кожне поле — обов'язкове. Межі значень — у metadata: min, max (для чисел і
# довжини списків — окремо min_len), choices (для рядків і елементів списків).


# Які межі бувають. Одруківка в назві межі (`mn=1`, `maxx=9`) тихо вимкнула б
# перевірку — тому невідома назва ламає імпорт модуля, а не проходить мовчки.
LIMIT_KEYS = frozenset({"min", "max", "min_len", "choices", "prefix"})


def _limits(**kw) -> dict:
    bad = set(kw) - LIMIT_KEYS
    if bad:
        raise TypeError(f"невідомі межі {sorted(bad)} (відомі: {sorted(LIMIT_KEYS)})")
    return {"metadata": kw}


@dataclass(frozen=True)
class SpeedTargets:
    server_p95_ms: int = field(**_limits(min=1))
    tab_switch_ms: int = field(**_limits(min=1))
    button_ms: int = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedGzip:
    min_bytes: int = field(**_limits(min=0))
    level: int = field(**_limits(min=1, max=9))


@dataclass(frozen=True)
class SpeedGenerations:
    poll_s: float = field(**_limits(min=0.5))


@dataclass(frozen=True)
class SpeedListMemo:
    enabled: bool
    max_keys: int = field(**_limits(min=1))
    max_age_s: float = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedAnalytics:
    max_age_h: float = field(**_limits(min=0.1))
    km_memo_keys: int = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedDeferred:
    views_flush_s: float = field(**_limits(min=1))
    busy_timeout_ms: int = field(**_limits(min=0))
    session_touch: bool
    max_buffer_rows: int = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedOpenCheck:
    enabled: bool
    wait_max_s: float = field(**_limits(min=0))
    poll_s: float = field(**_limits(min=0.1))
    job_timeout_s: float = field(**_limits(min=10))
    drain_budget_s: float = field(**_limits(min=0))
    launch_grace_s: float = field(**_limits(min=1))
    launcher: str = field(**_limits(choices=("auto", "systemd", "subprocess")))

    def problems(self) -> list[str]:
        # Процес перевірки бере нові завдання лише до drain_budget_s, а
        # останнє має встигнути до job_timeout_s (TimeoutStartSec) — інакше
        # systemd вбиває процес посеред завдання.
        if self.drain_budget_s >= self.job_timeout_s:
            return [f"drain_budget_s: {self.drain_budget_s} має бути меншим за "
                    f"job_timeout_s ({self.job_timeout_s})"]
        return []


@dataclass(frozen=True)
class SpeedSqlite:
    cache_kb: int = field(**_limits(min=0))


@dataclass(frozen=True)
class SpeedStatusPoll:
    status_s: float = field(**_limits(min=1))
    blocks_s: float = field(**_limits(min=1))
    dedup_s: float = field(**_limits(min=1))
    panels_s: float = field(**_limits(min=1))
    pause_hidden: bool


@dataclass(frozen=True)
class SpeedTimings:
    enabled: bool
    flush_s: float = field(**_limits(min=1))
    sample_polls: int = field(**_limits(min=1))
    retention_days: int = field(**_limits(min=1))
    cleanup_every_h: float = field(**_limits(min=1))
    roles: tuple[str, ...] = field(**_limits(choices=("owner", "friend")))


@dataclass(frozen=True)
class SpeedRum:
    enabled: bool
    roles: tuple[str, ...] = field(**_limits(choices=("owner", "friend")))
    retention_days: int = field(**_limits(min=1))
    max_body_bytes: int = field(**_limits(min=256))
    max_resources: int = field(**_limits(min=0))
    max_per_min: int = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedProbe:
    base_url: str = field(**_limits(prefix="http://"))
    urls: tuple[str, ...] = field(**_limits(min_len=1, prefix="/"))
    repeats: int = field(**_limits(min=1))
    pause_s: float = field(**_limits(min=0))
    timeout_s: float = field(**_limits(min=1))
    phase_wait_max_min: float = field(**_limits(min=0))
    phase_poll_s: float = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedSummary:
    server_window_h: float = field(**_limits(min=1))
    rum_window_days: float = field(**_limits(min=1))
    recent: int = field(**_limits(min=1))
    max_rows: int = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedTxnWatch:
    enabled: bool


@dataclass(frozen=True)
class SpeedConfig:
    """`config/speed.toml` — Блок 2: цілі, вимірювання, кеші (план b2-speed)."""

    targets: SpeedTargets
    gzip: SpeedGzip
    generations: SpeedGenerations
    list_memo: SpeedListMemo
    analytics: SpeedAnalytics
    deferred: SpeedDeferred
    open_check: SpeedOpenCheck
    sqlite: SpeedSqlite
    status_poll: SpeedStatusPoll
    timings: SpeedTimings
    rum: SpeedRum
    probe: SpeedProbe
    summary: SpeedSummary
    txn_watch: SpeedTxnWatch


@dataclass(frozen=True)
class LivenessRun:
    opened_recheck_minutes: float = field(**_limits(min=0))
    budget_minutes: float = field(**_limits(min=1))
    apply_batch_rows: int = field(**_limits(min=1, max=200))
    max_consecutive_blocks: int = field(**_limits(min=1))
    unknown_backoff_hours: tuple[float, ...] = field(**_limits(min=0, min_len=1))
    absent_backoff_hours: tuple[float, ...] = field(**_limits(min=0, min_len=1))
    reseen_grace_hours: float = field(**_limits(min=0))
    reseen_ignore_before: str
    opened_priority_hours: float = field(**_limits(min=0))
    canary_fresh_hours: float = field(**_limits(min=1))

    def problems(self) -> list[str]:
        from datetime import datetime

        try:
            datetime.fromisoformat(self.reseen_ignore_before)
        except ValueError:
            return [f"reseen_ignore_before: {self.reseen_ignore_before!r} — не дата ISO (UTC)"]
        return []


@dataclass(frozen=True)
class LivenessRepeat404:
    # Один 404 ніколи не знімає (рішення власника 1, D46): серія — від двох.
    count: int = field(**_limits(min=2))
    min_interval_hours: float = field(**_limits(min=1))
    feed_presence_hours: float = field(**_limits(min=1))
    # Серія вже набрала `count`, а оголошення існує чи перевірка не відповіла:
    # наступні 404-перевірки — через стільки годин (останнє значення — далі завжди).
    after_count_hours: tuple[float, ...] = field(**_limits(min=1, min_len=1))


@dataclass(frozen=True)
class LivenessRemovedSample:
    window_days: float = field(**_limits(min=1))
    min_gap_days: float = field(**_limits(min=0))


@dataclass(frozen=True)
class LivenessFuse:
    mode: str = field(**_limits(choices=("literal", "tiered")))
    share: float = field(**_limits(min=0, max=1))
    # Лише tiered, пул випадкових і контрольних (D56): wilson95 — тримати, коли нижня
    # межа 95% інтервалу Вілсона частки «знято» понад `share`; raw — сама частка.
    share_test: str = field(**_limits(choices=("wilson95", "raw")))
    min_checked: int = field(**_limits(min=1))
    hinted_share: float = field(**_limits(min=0, max=1))
    hinted_min_checked: int = field(**_limits(min=1))
    # Лише tiered: пул сліпого обходу й нічного догону — n для оцінки (межа —
    # hosts.*.sweep_share; D56).
    sweep_min_checked: int = field(**_limits(min=1))
    canary_trip_min: int = field(**_limits(min=1))
    # «Справжніх знять» контрольних DOM.RIA на хост за прогін, що не тримають (D56).
    canary_genuine_max: int = field(**_limits(min=0))
    window_hours: float = field(**_limits(min=0))
    # Лише tiered: пул випадкових і контрольних, що в прогоні не набрав min_checked, —
    # разом із такими перевірками за стільки годин (і в кроці циклу, D56).
    random_window_hours: float = field(**_limits(min=0))


@dataclass(frozen=True)
class LivenessAlerts:
    coverage_overdue_share: float = field(**_limits(min=0, max=1))
    unrecognized_share: float = field(**_limits(min=0, max=1))
    unrecognized_min_checked: int = field(**_limits(min=1))
    repeat404_return_share: float = field(**_limits(min=0, max=1))
    repeat404_min_removed: int = field(**_limits(min=1))
    snapshot_stale_hours: dict[str, float]


@dataclass(frozen=True)
class LivenessSnapshot:
    shrink_guard: float = field(**_limits(min=0, max=1))
    min_size: int = field(**_limits(min=1))
    max_candidates_per_run: int = field(**_limits(min=1))
    interval_hours: dict[str, float]


@dataclass(frozen=True)
class LivenessReport:
    coverage_days: float = field(**_limits(min=1))
    windows_days: tuple[int, ...] = field(**_limits(min=1, min_len=1))
    return_window_days: float = field(**_limits(min=1))


@dataclass(frozen=True)
class LivenessUi:
    unconfirmed_label: str
    unconfirmed_hint: str


@dataclass(frozen=True)
class LivenessRiaPage:
    state_marker: str
    state_path: tuple[str, ...] = field(**_limits(min_len=1))
    id_field: str
    status_field: str
    active_field: str
    archive_field: str
    archive_status: str
    active_status: str
    banner_class: str
    banner_text: str
    removed_requires_no_redirect: bool
    deleted_at_ts_field: str
    deleted_at_field: str
    source_tz: str
    api_card_url: str = field(**_limits(prefix="https://"))
    api_deleted_keys: tuple[str, ...] = field(**_limits(min_len=1))
    api_repair_url: str = field(**_limits(prefix="https://"))

    def problems(self) -> list[str]:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        out: list[str] = []
        for name in ("state_marker", "banner_class", "banner_text", "id_field",
                     "status_field", "archive_status", "active_status"):
            if not getattr(self, name).strip():
                out.append(f"{name}: порожньо")
        try:
            ZoneInfo(self.source_tz)
        except (ZoneInfoNotFoundError, ValueError):
            out.append(f"source_tz: невідомий часовий пояс {self.source_tz!r}")
        if "{id}" not in self.api_card_url:
            out.append("api_card_url: шаблон без {id}")
        if "{beautiful_url}" not in self.api_repair_url:
            out.append("api_repair_url: шаблон без {beautiful_url}")
        return out


@dataclass(frozen=True)
class LivenessCapture:
    enabled: bool
    ria_place: dict[str, str]
    ria_seller: dict[str, str]
    ria_profile: str
    ria_profile_prefix: str
    # Хост → стеля тіла, байт: уночі ключ, якому бракує доказів Блоків 3/4, смуга хоста
    # питає GET замість HEAD — класифікація та сама (лише за кодом), тіло — лише гачкам
    # доказів (інтеграція, конфлікт 4: «для rieltor у M3 замість HEAD робимо GET»; E11, D60).
    body_hosts: dict[str, int]

    def problems(self) -> list[str]:
        import re

        out: list[str] = []
        rx = re.compile(r"^(?:realty|data)(?:\.[\w]+)+\??$")
        for table in ("ria_place", "ria_seller"):
            for name, path in getattr(self, table).items():
                if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                    out.append(f"{table}.{name}: ім'я ключа — малі латинські й _")
                if not rx.match(path):
                    out.append(f"{table}.{name}: шлях {path!r} — realty.… чи data.…")
        if self.ria_profile and not rx.match(self.ria_profile):
            out.append(f"ria_profile: шлях {self.ria_profile!r} — realty.… чи data.…")
        for host, size in self.body_hosts.items():
            if not 10_000 <= size <= 5_000_000:
                out.append(f"body_hosts.{host}: {size} — стеля тіла 10 000…5 000 000 байт")
        return out


@dataclass(frozen=True)
class LivenessOlx:
    id_url: str = field(**_limits(prefix="https://"))
    id_url_enabled: bool

    def problems(self) -> list[str]:
        return [] if "{id}" in self.id_url else ["id_url: шаблон без {id}"]


# Перевірки існування для правила повторного 404 (realty/liveness/existence.py).
EXISTENCE_STRATEGIES = ("feed", "ria_api", "olx_id_url")
SNAPSHOT_STRATEGY_PREFIX = "snapshot:"


@dataclass(frozen=True)
class LivenessHost:
    family: str
    checkable: bool
    reason: str
    method: str = field(**_limits(choices=("HEAD", "GET", "")))
    delay: float = field(**_limits(min=0))
    pace_source: str
    probe: str = field(**_limits(choices=("canonical", "fetch", "")))
    signature: str = field(**_limits(choices=("code", "ria_page", "")))
    max_bytes: int = field(**_limits(min=0))
    removed_statuses: tuple[int, ...] = field(**_limits(min=100, max=599))
    not_found_statuses: tuple[int, ...] = field(**_limits(min=100, max=599))
    existence: tuple[str, ...]
    recheck_days: float = field(**_limits(min=0))
    sweep_per_run: int = field(**_limits(min=0))
    # Випадкові ключі на прогін (ярус random, D56) — З порції sweep_per_run: сліпий
    # обхід бере sweep_per_run мінус узяті випадкові, запитів на прогін не більшає.
    random_per_run: int = field(**_limits(min=0))
    # Межа частки «знято» пулу обходу хоста (tiered; D56) — за виміряною звичайною.
    sweep_share: float = field(**_limits(min=0, max=1))
    hinted_cap_per_run: int = field(**_limits(min=0))
    removed_sample_per_run: int = field(**_limits(min=0))
    canaries_per_run: int = field(**_limits(min=0))
    # Чия стрічка робить ключ контрольним (D56): свіжа поява лише в рядку цих джерел
    # (власний збирач сайту; rieltor — lun), а не будь-якої копії ключа.
    canary_sources: tuple[str, ...]

    def problems(self) -> list[str]:
        out: list[str] = []
        if not self.family.strip():
            out.append("family: порожньо")
        if self.random_per_run > self.sweep_per_run:
            out.append(f"random_per_run: {self.random_per_run} — понад sweep_per_run "
                       f"({self.sweep_per_run}): випадкові беруться з порції сліпого обходу")
        if self.checkable and self.canaries_per_run > 0 and not self.canary_sources:
            out.append("canary_sources: порожньо, а canaries_per_run > 0 — контрольних не буде")
        # НЕДОТОРКАНЕ ПРАВИЛО (рішення власника 1, D46): один 404 — «не знайдено».
        if 404 in self.removed_statuses:
            out.append("removed_statuses: 404 не може бути явним сигналом «знято» — лише "
                       "повторний 404 з перевіркою існування (repeat_404)")
        both = set(self.removed_statuses) & set(self.not_found_statuses)
        if both:
            out.append(f"код(и) {sorted(both)} і в removed_statuses, і в not_found_statuses")
        bad = {c for c in (*self.removed_statuses, *self.not_found_statuses)
               if 200 <= c < 400 or c in (401, 403, 429) or c >= 500}
        if bad:
            out.append(f"коди {sorted(bad)} не можуть бути вироком (живе, блокування чи збій)")
        for name in self.existence:
            if name not in EXISTENCE_STRATEGIES and not (
                    name.startswith(SNAPSHOT_STRATEGY_PREFIX)
                    and name[len(SNAPSHOT_STRATEGY_PREFIX):].strip()):
                out.append(f"existence: невідома стратегія {name!r} (відомі: "
                           f"{', '.join(EXISTENCE_STRATEGIES)}, snapshot:<джерело>)")
        if self.checkable:
            for name in ("method", "probe", "signature"):
                if not getattr(self, name):
                    out.append(f"{name}: порожньо для хоста, що перевіряється")
            if self.signature == "ria_page" and (self.method != "GET" or self.max_bytes <= 0):
                out.append("signature = ria_page потребує method = GET і max_bytes > 0")
            if self.delay <= 0:
                out.append("delay: має бути > 0 для хоста, що перевіряється")
            if self.recheck_days <= 0:
                out.append("recheck_days: має бути > 0 для хоста, що перевіряється")
        elif not self.reason.strip():
            out.append("reason: поясніть, чому хост не перевіряється")
        return out


@dataclass(frozen=True)
class LivenessConfig:
    """`config/liveness.toml` — Блок 1 (перевірка актуальності), крок E8 (D52).

    Хости, паузи, підписи «знято», правило повторного 404 і перевірка існування,
    графіки повторів, порції ярусів на прогін, вибірка знятих, запобіжник
    (обидва тлумачення — `fuse.mode`), тривоги сторожа, позначка Благо на сайті.
    `run.opened_recheck_minutes` — спільний із перевіркою при відкритті (Блок 2, D50).
    """

    run: LivenessRun
    repeat_404: LivenessRepeat404
    removed_sample: LivenessRemovedSample
    fuse: LivenessFuse
    alerts: LivenessAlerts
    snapshot: LivenessSnapshot
    report: LivenessReport
    ui: LivenessUi
    ria_page: LivenessRiaPage
    capture: LivenessCapture
    olx: LivenessOlx
    hosts: dict[str, LivenessHost]

    def problems(self) -> list[str]:
        from .config import SOURCES

        out: list[str] = []
        families: dict[str, str] = {}
        for host, spec in self.hosts.items():
            if host != host.strip().lower() or host.startswith(("www.", "m.")):
                out.append(f"hosts.{host}: хост — малими літерами, без www./m.")
            unknown = [s for s in spec.canary_sources if s not in SOURCES]
            if unknown:
                out.append(f"hosts.{host}.canary_sources: невідомі джерела {unknown} "
                           f"(відомі: {', '.join(sorted(SOURCES))})")
            if spec.family in families:
                out.append(f"hosts.{host}: сімейство «{spec.family}» уже має хост "
                           f"{families[spec.family]}")
            families[spec.family] = host
        if not any(h.checkable for h in self.hosts.values()):
            out.append("hosts: жоден хост не перевіряється")
        if not self.ui.unconfirmed_label.strip():
            out.append("ui.unconfirmed_label: порожньо")
        for host in self.capture.body_hosts:
            spec = self.hosts.get(host)
            # GET замість HEAD — лише там, де вердикт дає сам код (signature = code):
            # тоді класифікація та сама, а тіло йде лише гачкам доказів (E11, D60).
            if spec is None or not spec.checkable or spec.signature != "code":
                out.append(f"capture.body_hosts.{host}: хоста, що перевіряється за кодом "
                           f"(signature = code), немає в hosts")
        for name in ("alerts.snapshot_stale_hours", "snapshot.interval_hours"):
            table = self.alerts.snapshot_stale_hours if name.startswith("alerts") \
                else self.snapshot.interval_hours
            for source, hours in table.items():
                if hours <= 0:
                    out.append(f"{name}.{source}: має бути > 0")
        out += self._budget_problems()
        return out

    def _budget_problems(self) -> list[str]:
        """Порції хостів умістяться в стелю прогону, стеля — у ліміт кроку циклу.

        Кожен ключ — щонайменше одна пауза хоста (`delay` — нижня межа: відповідь
        і перевірки існування — понад неї), тож (контрольні + підказані + вибірка
        знятих + сліпий обхід) × delay ≤ 80% стелі; стеля мережевої фази — на 5 хв
        менша за ліміт кроку «перевірка актуальності» (runner.TASK_TIMEOUTS):
        застосування, зведення й запас на запити, що вже в дорозі (рецензія E8, D52).
        """
        from .runner import TASK_TIMEOUTS

        out: list[str] = []
        budget_s = self.run.budget_minutes * 60
        step_min = TASK_TIMEOUTS["verify"] / 60
        if self.run.budget_minutes > step_min - 5:
            out.append(f"run.budget_minutes: {self.run.budget_minutes:g} — понад ліміт кроку "
                       f"циклу мінус 5 хв ({step_min:g} − 5)")
        for host, spec in self.hosts.items():
            if not spec.checkable:
                continue
            keys = (spec.canaries_per_run + spec.hinted_cap_per_run
                    + spec.removed_sample_per_run + spec.sweep_per_run)
            need = keys * spec.delay
            if need > 0.8 * budget_s:
                out.append(f"hosts.{host}: {keys} ключів × {spec.delay:g} с = {need:.0f} с — "
                           f"понад 80% стелі прогону ({0.8 * budget_s:.0f} с)")
        return out


def _regex_problems(where: str, pattern: str, *, groups: tuple[str, ...] = ()) -> list[str]:
    """Регулярний вираз із конфігу компілюється й має потрібні іменовані групи.

    Зламаний вираз інакше вилетів би `re.error` лише на першому розборі — у
    кроці циклу чи в запиті сайту, а не в `cli.py config check` до розгортання.
    """
    import re

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return [f"{where}: регулярний вираз не компілюється ({e})"]
    missing = [g for g in groups if g not in rx.groupindex]
    return [f"{where}: у виразі немає групи (?P<{g}>…)" for g in missing]


@dataclass(frozen=True)
class LinkFamily:
    """Одне сімейство посилань (сайт) у `config/links.toml`."""

    # Ім'я → вираз для ШЛЯХУ (після %-декодування, регістр збережено); перший
    # збіг виграє. Група `id` обов'язкова; інші іменовані групи (slug, loc, cat,
    # kind) — частини для шаблонів адрес і для `require`.
    id_regex: dict[str, str]
    # Параметр query → вираз значення: id, що живе лише в query (DOM.RIA
    # realtyId, Благо planning_id). Решта query відкидається повністю.
    query_id: dict[str, str]
    # Сторінки того самого сайту, що не є оголошенням (пошук, ЖК, каталог).
    non_listing_regex: dict[str, str]
    # Група → дозволені значення: інакше це не квартира на продаж (not_flat).
    require: dict[str, tuple[str, ...]]
    # Шаблони адрес; береться ПЕРШИЙ, для якого відомі всі частини.
    canonical_url: tuple[str, ...]
    # Шаблони адреси для запиту перевірки; порожньо — не перевіряється.
    fetch_url: tuple[str, ...]


@dataclass(frozen=True)
class LinksHosts:
    strip_prefixes: tuple[str, ...]
    family: dict[str, str]
    subdomain_family: dict[str, str]
    own_suffixes: tuple[str, ...]
    reject: dict[str, str]
    short_links: tuple[str, ...]


@dataclass(frozen=True)
class LinksUnwrap:
    max_depth: int = field(**_limits(min=0, max=10))
    max_decode: int = field(**_limits(min=0, max=5))
    scheme: str
    app_link: str
    android_app_split: str
    first_url_in_text: str
    trailing_punct: str


@dataclass(frozen=True)
class LinksOlx:
    alphabet: str
    numeric_digits: int = field(**_limits(min=1, max=20))
    case_lost_regex: str


@dataclass(frozen=True)
class LinksBare:
    property_prefixed: str
    olx_numeric_prefixed: str
    numeric: str
    olx_token: str
    olx_id_prefix: str
    number_families: tuple[str, ...]
    family_key: str
    family_alias: dict[str, str]


@dataclass(frozen=True)
class LinksReindex:
    batch_rows: int = field(**_limits(min=1, max=200))


@dataclass(frozen=True)
class LinksConfig:
    """`config/links.toml` — формати посилань джерел (крок E6, D51).

    Спільний для Блоків 1, 3 і 5 (інтеграція, конфлікт «один ключ сайт:id»):
    розбір посилання, ключ `listings.site_key`, канонічна адреса й адреса
    перевірки. Код — `realty/links.py`.
    """

    listing_families: tuple[str, ...]
    hosts: LinksHosts
    unwrap: LinksUnwrap
    olx: LinksOlx
    bare: LinksBare
    reindex: LinksReindex
    families: dict[str, LinkFamily]

    def problems(self) -> list[str]:
        out: list[str] = []
        known = set(self.families)
        for fam in self.listing_families:
            if fam not in known:
                out.append(f"listing_families: сімейства «{fam}» немає в [families]")
        for where, mapping in (("hosts.family", self.hosts.family),
                               ("hosts.subdomain_family", self.hosts.subdomain_family)):
            for host, fam in mapping.items():
                if fam not in known:
                    out.append(f"{where}.{host}: сімейства «{fam}» немає в [families]")
        alphabet = self.olx.alphabet
        if len(alphabet) != 62 or len(set(alphabet)) != 62 or not alphabet.isalnum():
            out.append("olx.alphabet: потрібні 62 різні літери й цифри")
        for name in ("scheme", "app_link", "android_app_split", "first_url_in_text"):
            out += _regex_problems(f"unwrap.{name}", getattr(self.unwrap, name))
        out += _regex_problems("unwrap.app_link", self.unwrap.app_link, groups=("inner",))
        out += _regex_problems("unwrap.android_app_split", self.unwrap.android_app_split,
                               groups=("scheme", "rest"))
        out += _regex_problems("olx.case_lost_regex", self.olx.case_lost_regex, groups=("id",))
        for name in ("property_prefixed", "olx_numeric_prefixed", "numeric"):
            out += _regex_problems(f"bare.{name}", getattr(self.bare, name), groups=("num",))
        out += _regex_problems("bare.olx_token", self.bare.olx_token)
        for fam in self.bare.number_families:
            if fam not in known:
                out.append(f"bare.number_families: сімейства «{fam}» немає в [families]")
        out += _regex_problems("bare.family_key", self.bare.family_key, groups=("fam", "id"))
        for alias, fam in self.bare.family_alias.items():
            if fam not in known:
                out.append(f"bare.family_alias.{alias}: сімейства «{fam}» немає в [families]")
        for fam, spec in self.families.items():
            if not spec.id_regex:
                out.append(f"families.{fam}.id_regex: порожньо")
            for name, rx in spec.id_regex.items():
                out += _regex_problems(f"families.{fam}.id_regex.{name}", rx, groups=("id",))
            for name, rx in {**spec.query_id, **spec.non_listing_regex}.items():
                out += _regex_problems(f"families.{fam}.{name}", rx)
            for tpl in (*spec.canonical_url, *spec.fetch_url):
                if "{id}" not in tpl:
                    out.append(f"families.{fam}: шаблон {tpl!r} без {{id}}")
        return out


@dataclass(frozen=True)
class PrivacyPhone:
    enabled: bool
    replacement: str
    fields: tuple[str, ...] = field(**_limits(choices=("description", "title")))
    mobile_codes: tuple[str, ...] = field(**_limits(min_len=1))
    area_first_digits: str
    national_shapes: tuple[str, ...] = field(**_limits(min_len=1))
    international_any_shape: bool
    separators: str
    max_sep_run: int = field(**_limits(min=1, max=5))
    plus_inside_international: bool
    mask_chars: str
    min_mask_chars: int = field(**_limits(min=2))
    min_mask_chars_full_international: int = field(**_limits(min=2))
    max_mask_national_positions: int = field(**_limits(min=10, max=16))
    short_local_shapes: tuple[str, ...]
    short_local_keyword_regex: str
    link_regex: dict[str, str]

    def problems(self) -> list[str]:
        import re

        out: list[str] = []
        if any(ch.isdigit() for ch in self.replacement):
            # Ідемпотентність: заміна не має давати нового «номера» в тексті.
            out.append("replacement: у заміні не може бути цифр")
        if not self.replacement.strip():
            out.append("replacement: порожньо")
        for code in self.mobile_codes:
            if not re.fullmatch(r"\d\d", code):
                out.append(f"mobile_codes: {code!r} — потрібні дві цифри після 0")
        if not re.fullmatch(r"\d+", self.area_first_digits):
            out.append("area_first_digits: лише цифри")
        for shape in (*self.national_shapes, *self.short_local_shapes):
            if not re.fullmatch(r"\d+(?:-\d+)*", shape):
                out.append(f"форма {shape!r} — числа через дефіс")
        for shape in self.national_shapes:
            if sum(int(x) for x in shape.split("-")) != 10:
                out.append(f"national_shapes: {shape!r} — разом має бути 10 цифр")
        if any(ch.isdigit() or ch.isalpha() for ch in self.separators):
            out.append("separators: лише розділові знаки й пробіли")
        if "+" in self.separators:
            out.append("separators: «+» — лише через plus_inside_international (між цифрами +380)")
        if any(ch.isdigit() for ch in self.mask_chars) or not self.mask_chars:
            out.append("mask_chars: без цифр і не порожньо")
        if set(self.mask_chars) & set(self.separators):
            out.append("mask_chars: символ маски не може бути роздільником")
        if self.min_mask_chars_full_international > self.min_mask_chars:
            out.append("min_mask_chars_full_international: не більше за min_mask_chars")
        out += _regex_problems("short_local_keyword_regex", self.short_local_keyword_regex)
        for name, rx in self.link_regex.items():
            out += _regex_problems(f"link_regex.{name}", rx)
        return out


@dataclass(frozen=True)
class PrivacyScan:
    top_patterns: int = field(**_limits(min=1))
    loose_gap_max: int = field(**_limits(min=1, max=5))


@dataclass(frozen=True)
class PrivacyApply:
    batch_rows: int = field(**_limits(min=1, max=200))
    max_rows: int = field(**_limits(min=1))
    backup_max_age_h: float = field(**_limits(min=0.1))


# --- Нічний диригент (`cli.py night`, E9, D53) -------------------------------------------

# Роботи реєстру — у порядку пріоритету всередині смуги хоста (config/night.toml,
# jobs.order). Перші вісім — яруси перевірки актуальності (realty/liveness/queue.py);
# далі — дозбір (NIGHT_POST_JOBS), строго ПІСЛЯ всіх ярусів Блоку 1 (інтеграція,
# конфлікт 3): olx_tabs і olx_detail — смуга рендерів OLX (Блоки 3/4, E11, D60),
# identity — колишній realty-identity.timer: картки DOM.RIA, а в LUN і flombu — ОДИН
# прохід стрічки за ніч (identity + place_raw + seller_evidence, лише туди, де порожньо).
NIGHT_JOBS = ("canary", "held", "legacy_404", "onetime_reseen", "onetime_hinted",
              "onetime_blind", "rm_sample", "overdue", "olx_tabs", "olx_detail", "identity")
NIGHT_POST_JOBS = ("olx_tabs", "olx_detail", "identity")
IDENTITY_SOURCES = ("domria", "lun", "flombu")
# Джерела спільного нічного проходу стрічки (night.toml [feed]).
FEED_SOURCES = ("lun", "flombu")
# Хост смуги рендерів OLX (liveness.toml hosts) — робіт olx_tabs / olx_detail.
OLX_RENDER_HOST = "olx.ua"


def hhmm_minutes(text: str) -> int | None:
    """«ГГ:ХХ» (місцевий час машини) → хвилини від півночі; не той формат — None."""
    import re

    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


@dataclass(frozen=True)
class NightWindow:
    start: str
    stop_requests: str
    release_lock: str

    def problems(self) -> list[str]:
        out: list[str] = []
        mins = {}
        for name in ("start", "stop_requests", "release_lock"):
            mins[name] = hhmm_minutes(getattr(self, name))
            if mins[name] is None:
                out.append(f"{name}: {getattr(self, name)!r} — час «ГГ:ХХ»")
        if not out and not mins["start"] < mins["stop_requests"] < mins["release_lock"]:
            out.append("має бути start < stop_requests < release_lock у межах однієї доби")
        return out

    def minutes(self) -> tuple[int, int, int]:
        return (hhmm_minutes(self.start), hhmm_minutes(self.stop_requests),
                hhmm_minutes(self.release_lock))


@dataclass(frozen=True)
class NightLock:
    poll_seconds: float = field(**_limits(min=1))
    min_work_minutes: float = field(**_limits(min=1))


@dataclass(frozen=True)
class NightBackup:
    max_age_hours: float = field(**_limits(min=1))
    # Вікно з одноразовими роботами (M2/M3: тисячі повернень і знять) — бекап, якщо
    # останній успішний старший за стільки годин (рецензія E9, D53).
    onetime_max_age_hours: float = field(**_limits(min=0.5))
    timeout_minutes: float = field(**_limits(min=1))

    def problems(self) -> list[str]:
        if self.onetime_max_age_hours > self.max_age_hours:
            return ["onetime_max_age_hours: не більше за max_age_hours"]
        return []


@dataclass(frozen=True)
class NightLanes:
    batch_minutes: float = field(**_limits(min=0.1))
    poll_seconds: float = field(**_limits(min=0.1))
    kill_grace_seconds: float = field(**_limits(min=1))
    block_share: float = field(**_limits(min=0, max=1))
    block_min_requests: int = field(**_limits(min=1))


@dataclass(frozen=True)
class NightJobs:
    order: tuple[str, ...] = field(**_limits(min_len=1, choices=NIGHT_JOBS))
    # Контрольних на хост у КОЖНОМУ наступному пакеті вікна (перший пакет — повна порція
    # liveness hosts.*.canaries_per_run; рішення власника 08.10, D55/D56).
    canaries_per_batch: int = field(**_limits(min=0))
    onetime_seed: int
    rm_sample_per_host: dict[str, int]
    identity_sources: dict[str, str]

    def problems(self) -> list[str]:
        out: list[str] = []
        if len(set(self.order)) != len(self.order):
            out.append("order: роботи повторюються")
        if "canary" in self.order and self.order[0] != "canary":
            # Запобіжник за контрольними (≥2 «знято» серед відомо живих) має побачити
            # зламаний підпис у ПЕРШОМУ пакеті ночі, а не після тисяч запитів.
            out.append("order: canary — першою (контрольні — у першому пакеті ночі)")
        if "identity" in self.order and self.order[-1] != "identity":
            out.append("order: identity — останньою (пріоритет Блоку 1 над дозбором; "
                       "інтеграція, конфлікт 3)")
        post = [i for i, job in enumerate(self.order) if job in NIGHT_POST_JOBS]
        tiers = [i for i, job in enumerate(self.order) if job not in NIGHT_POST_JOBS]
        if post and tiers and min(post) < max(tiers):
            out.append("order: дозбір (olx_tabs, olx_detail, identity) — після всіх ярусів "
                       "перевірки актуальності (Блок 1 > дозбір; інтеграція, конфлікт 3)")
        if "olx_detail" in self.order and "olx_tabs" in self.order and \
                self.order.index("olx_tabs") > self.order.index("olx_detail"):
            out.append("order: olx_tabs — перед olx_detail (вкладка «Приватні» ставить "
                       "приватних першими в чергу сторінок деталей; E11, D60)")
        for host, n in self.rm_sample_per_host.items():
            if n < 0:
                out.append(f"rm_sample_per_host.{host}: має бути ≥ 0")
        for host, source in self.identity_sources.items():
            if source not in IDENTITY_SOURCES:
                out.append(f"identity_sources.{host}: {source!r} — не з {list(IDENTITY_SOURCES)}")
        if len(set(self.identity_sources.values())) != len(self.identity_sources):
            out.append("identity_sources: одне джерело — в одній смузі")
        return out


@dataclass(frozen=True)
class NightReport:
    liquidity: bool
    # Типовий час одного запиту смуги, с (`night --dry-run`, доки немає заміру ночі):
    # темп — старт-до-старту, тож справжній крок = max(темп, затримка відповіді).
    typical_request_seconds: dict[str, float]

    def problems(self) -> list[str]:
        return [f"typical_request_seconds.{h}: має бути > 0"
                for h, v in self.typical_request_seconds.items() if not v > 0]


@dataclass(frozen=True)
class NightFeed:
    """Спільний нічний прохід стрічки LUN і flombu — робота identity їхніх смуг (E11, D60).

    Не збір: нових оголошень не додає, ціни, last_seen і стан актуальності не чіпає;
    пише лише identity (якщо NULL), place_raw і seller_evidence (нові ключі) і
    seller_profile (якщо NULL) — після кожної сторінки, з місця зупинки."""

    # Прохід — лише якщо стільки АКТУАЛЬНИХ рядків джерела ще без доказів (identity,
    # позначки стрічки в place_raw і seller_evidence) — як FULL_PASS_MIN дозбору
    # identity (D42) і план Блоку 3 («поки ≥50 активних LUN без доказів»).
    min_missing_active: int = field(**_limits(min=1))
    # Після завершеного проходу — не раніше ніж через стільки годин, а якщо він закрив
    # менше за low_gain рядків — через cooldown_low_gain_days (D42: прохід LUN двічі
    # за ніч дав 3 783 і 80 записів).
    cooldown_hours: float = field(**_limits(min=1))
    cooldown_low_gain_days: float = field(**_limits(min=1))
    low_gain: int = field(**_limits(min=0))
    # Стеля сторінок проходу за джерелом.
    max_pages: dict[str, int]
    # Незавершений прохід продовжується з наступної сторінки, якщо почався не давніше
    # за стільки годин; інакше — новий із першої (стрічка за дні зсувається).
    resume_max_age_hours: float = field(**_limits(min=1))
    # Типовий час сторінки, с (запит + розбір на N3540) — для `night --dry-run`.
    typical_page_seconds: dict[str, float]

    def problems(self) -> list[str]:
        out = []
        for name in ("max_pages", "typical_page_seconds"):
            table = getattr(self, name)
            out += [f"{name}.{s}: невідоме джерело (лише {list(FEED_SOURCES)})"
                    for s in table if s not in FEED_SOURCES]
            missing = [s for s in FEED_SOURCES if s not in table]
            if missing:
                out.append(f"{name}: бракує {missing}")
        out += [f"max_pages.{s}: має бути 1…1000" for s, n in self.max_pages.items()
                if not 1 <= n <= 1000]
        out += [f"typical_page_seconds.{s}: має бути > 0"
                for s, v in self.typical_page_seconds.items() if not v > 0]
        return out


@dataclass(frozen=True)
class NightOlxRender:
    """Смуга рендерів OLX (Chromium) — у нічній смузі olx.ua, ПІСЛЯ перевірок Блоку 1:
    вкладки пошуку «Приватні»/«Бізнес», потім одна черга сторінок деталей для Блоків 3
    і 4 (E11, D60). Темп — той самий темп смуги olx.ua (max(liveness delay, OLX
    full_delay) = 2,8 с старт-до-старту), блокування — ті самі «5 поспіль»."""

    # Стеля рендерів Chromium за вікно (вкладки + сторінки деталей разом).
    max_per_window: int = field(**_limits(min=0))
    detail_per_window: int = field(**_limits(min=0))
    private_tab_max_pages: int = field(**_limits(min=0, max=25))
    # Вкладки «Бізнес» (зрізи кімнати × ціна) — раз на стільки діб.
    business_tab_every_days: float = field(**_limits(min=1))
    business_tab_max_pages: int = field(**_limits(min=1, max=25))
    # Карток на сторінці видачі OLX — оцінка сторінок вкладки «Бізнес» для `--dry-run`.
    cards_per_page: int = field(**_limits(min=1))
    settle_ms: int = field(**_limits(min=0, max=10_000))
    # Стеля одного рендера, с (сторож убиває браузер), і запас до stop_requests, за
    # який новий рендер уже не починаємо: рендер, почате до межі, має закінчитись до
    # примусової зупинки смуги (stop_requests + kill_grace) — це перевіряє схема.
    render_timeout_seconds: float = field(**_limits(min=5))
    deadline_margin_seconds: float = field(**_limits(min=0))
    # Браузер перезапускається після стількох рендерів (пам'ять Chromium росте).
    restart_every: int = field(**_limits(min=1))
    # MemAvailable нижче — рендери зупиняються до кінця вікна (Linux).
    min_mem_available_mb: int = field(**_limits(min=0))
    block_resources: tuple[str, ...] = field(**_limits(
        choices=("image", "media", "font", "stylesheet", "other")))
    launch_args: tuple[str, ...]
    # Сторінка 200 без змісту, але з таким текстом — капча/відмова: рахується як
    # блокування (у «5 поспіль»), а не як «оголошення без чипа».
    captcha_markers: tuple[str, ...]
    # Невдалий рендер ключа (не 200, сторінка без змісту) — повтор не раніше ніж через
    # стільки годин (і точно не цієї ночі).
    failed_retry_hours: float = field(**_limits(min=12))
    # Типовий час одного рендера, с — для `night --dry-run` (ops: 1 074 с на 158
    # сторінок деталей ≈ 6,8 с; план Блоку 3).
    typical_seconds: float = field(**_limits(min=0.1))

    def problems(self) -> list[str]:
        out = []
        if self.private_tab_max_pages + self.detail_per_window > self.max_per_window:
            out.append("private_tab_max_pages + detail_per_window — понад max_per_window")
        for arg in self.launch_args:
            if not arg.startswith("--"):
                out.append(f"launch_args: {arg!r} — прапорець Chromium «--…»")
        if any(not m.strip() for m in self.captcha_markers):
            out.append("captcha_markers: порожній рядок")
        return out


@dataclass(frozen=True)
class NightConfig:
    """`config/night.toml` — нічний диригент (`cli.py night`, E9, D53).

    Вікна (місцевий час машини), замок циклу, бекап на старті ночі, смуги хостів і
    пакети застосування, порядок робіт реєстру. Темп кожного хоста — НЕ тут, а в
    config/liveness.toml (`policy.pace(mode="night")` = max(delay, SOURCES.full_delay);
    інтеграція, конфлікт 4); запобіжник — fuse.* там само. Дозбір доказів Блоків 3/4
    (E11, D60): прохід стрічки LUN/flombu — [feed], рендери OLX — [olx_render]; що
    саме брати зі сторінок — config/seller.toml і config/places/rules.toml.
    """

    windows: tuple[NightWindow, ...] = field(**_limits(min_len=1))
    lock: NightLock
    backup: NightBackup
    lanes: NightLanes
    jobs: NightJobs
    report: NightReport
    feed: NightFeed
    olx_render: NightOlxRender

    def problems(self) -> list[str]:
        out: list[str] = []
        spans = sorted((w.minutes(), i) for i, w in enumerate(self.windows))
        for (a, i), (b, j) in zip(spans, spans[1:]):
            if a[2] > b[0]:
                out.append(f"windows[{i}] і windows[{j}] перекриваються (замок до "
                           f"{self.windows[i].release_lock} пізніше за старт "
                           f"{self.windows[j].start})")
        for (start, stop, release), i in spans:
            # Після стелі запитів: смуги дочекаються запитів у дорозі (kill_grace), далі
            # останній пакет і звільнення замка — до release_lock (≥2 хв запасу).
            if (release - stop) * 60 < self.lanes.kill_grace_seconds + 120:
                out.append(f"windows[{i}]: між stop_requests і release_lock менше за "
                           f"kill_grace_seconds + 2 хв")
            if (stop - start) <= self.lock.min_work_minutes:
                out.append(f"windows[{i}]: вікно коротше за lock.min_work_minutes")
        # Рендер, почате за deadline_margin до stop_requests, мусить закінчитись до
        # примусової зупинки смуги (stop_requests + kill_grace) з 10 с запасу: інакше
        # диригент убиває смугу посеред рендера (нічого не губиться, але й не пишеться).
        r = self.olx_render
        if r.render_timeout_seconds > r.deadline_margin_seconds + \
                self.lanes.kill_grace_seconds - 10:
            out.append(f"olx_render.render_timeout_seconds: {r.render_timeout_seconds:g} с — "
                       f"понад deadline_margin_seconds + lanes.kill_grace_seconds − 10 с")
        out += self._liveness_problems()
        return out

    def _liveness_problems(self) -> list[str]:
        """Хости смуг — ті самі, що в config/liveness.toml (і перевіряються)."""
        try:
            hosts = load("liveness").hosts
        except ConfigError:
            return []                              # помилку liveness.toml покаже його перевірка
        out: list[str] = []
        for table, keys in (("jobs.rm_sample_per_host", self.jobs.rm_sample_per_host),
                            ("jobs.identity_sources", self.jobs.identity_sources),
                            ("report.typical_request_seconds",
                             self.report.typical_request_seconds)):
            for host in keys:
                spec = hosts.get(host)
                if spec is None or not spec.checkable:
                    out.append(f"{table}.{host}: такого хоста, що перевіряється, немає "
                               f"в config/liveness.toml")
        if {"olx_tabs", "olx_detail"} & set(self.jobs.order):
            spec = hosts.get(OLX_RENDER_HOST)
            if spec is None or not spec.checkable:
                out.append(f"jobs.order: рендери OLX — у смузі {OLX_RENDER_HOST}, а такого "
                           f"хоста, що перевіряється, немає в config/liveness.toml")
        return out


@dataclass(frozen=True)
class PrivacyConfig:
    """`config/privacy.toml` — телефони в описах і назвах (рішення власника 5, D46; E6, D51)."""

    phone: PrivacyPhone
    scan: PrivacyScan
    apply: PrivacyApply


# --- Райони й ЖК (Блок 4, крок E10, D57) ------------------------------------------------------
# Три файли в config/places/: довідник районів (districts.toml), довідник ЖК
# (complexes.toml) і правила (rules.toml). Ключі — стабільні латинські слаги: вони
# стоять в адресах (?district=…) і в базі (listings.district_key), тож перейменування
# назви ключа не змінює. Узгодженість ДОВІДНИКА між файлами (псевдонім веде до однієї
# сутності, район ЖК існує, id DOM.RIA не дублюються) перевіряє PlacesComplexesConfig
# через realty/places/directory.py — так `config check` зупиняє розгортання з колізією.

PLACE_AREAS = ("city", "hromada", "outside")
PLACE_IGNORE_KINDS = ("landmark", "poi", "street", "city", "complex", "region")
PLACE_COMPLEX_KINDS = ("named", "address", "umbrella")
PLACE_DISTRICT_TIERS = ("complex_src", "addr", "src", "complex_weak", "coords")
PLACE_COMPLEX_TIERS = ("src_id", "src_name", "coords", "text")
PLACE_KEY_MAX = 48
PLACE_COMPLEX_KEY_MAX = 64


def _slug_problems(where: str, key: str, limit: int) -> list[str]:
    import re

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key or ""):
        return [f"{where}: ключ {key!r} — лише a-z, 0-9 і дефіс (стабільний слаг)"]
    if len(key) > limit:
        return [f"{where}: ключ {key!r} довший за {limit}"]
    return []


@dataclass(frozen=True)
class PlaceDistrict:
    key: str
    name: str
    # city — місто; hromada — село міської громади (рішення власника 4, D46: окремий
    # «район» з позначкою «громада»); outside — поза громадою (Лисець, аномалія «інша
    # область»): видно в «усе», не видно в «тільки місто».
    area: str = field(**_limits(choices=PLACE_AREAS))
    # Батьківський район — лише за доказом (rules.link: ≥80% при n≥5; D57); "" — немає.
    parent: str
    # false — склад не звірено з КАТОТТГ (села громади, Етап 0); показується як є.
    verified: bool
    aliases: tuple[str, ...]
    ria_ids: tuple[int, ...]
    lun_ids: tuple[int, ...]

    def problems(self) -> list[str]:
        out = _slug_problems("key", self.key, PLACE_KEY_MAX)
        if not self.name.strip():
            out.append("name: порожня назва")
        if self.parent == self.key:
            out.append("parent: район не може бути батьком сам собі")
        return out


@dataclass(frozen=True)
class PlaceIgnore:
    """Відома назва, що НЕ є районом (орієнтир, POI, вулиця, місто): не стає районом і
    не потрапляє в «нерозпізнані» на /status."""

    name: str
    kind: str = field(**_limits(choices=PLACE_IGNORE_KINDS))


@dataclass(frozen=True)
class PlacesDistrictsConfig:
    """`config/places/districts.toml` — райони міста, села громади, ігноровані назви (E10, D57)."""

    district: tuple[PlaceDistrict, ...] = field(**_limits(min_len=1))
    ignore: tuple[PlaceIgnore, ...]

    def problems(self) -> list[str]:
        out: list[str] = []
        keys = [d.key for d in self.district]
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            out.append(f"district: ключі повторюються: {', '.join(dup)}")
        known = set(keys)
        by_key = {d.key: d for d in self.district}
        for d in self.district:
            if d.parent and d.parent not in known:
                out.append(f"district {d.key}: parent {d.parent!r} — такого району немає")
            elif d.parent and by_key[d.parent].parent:
                out.append(f"district {d.key}: parent {d.parent!r} сам має батька — "
                           f"лише один рівень")
            elif d.parent and by_key[d.parent].area != d.area:
                out.append(f"district {d.key}: area {d.area!r} ≠ area батька "
                           f"{by_key[d.parent].area!r}")
        return out


@dataclass(frozen=True)
class PlaceComplex:
    key: str
    name: str
    # Район ЖК — лише за доказом (rules.link: ≥80% при n≥5; голоси — у коментарі над
    # записом); "" — не прив'язано (видно на /status як «ЖК без району»).
    district: str
    # named — звичайний; address — «ЖК вул. …» DOM.RIA (збіг лише за id і точною назвою,
    # текстовий ступінь їх не шукає); umbrella — «житловий район», усередині якого є
    # конкретні ЖК (поступається їм у квартирі, протиріччям не вважається).
    kind: str = field(**_limits(choices=PLACE_COMPLEX_KINDS))
    within: str           # ключ парасольки або ""
    group: str            # спільна група черг (Family Plaza / Family Plaza 2) або ""
    ria_ids: tuple[int, ...]
    lun_ids: tuple[int, ...]
    aliases: tuple[str, ...]

    def problems(self) -> list[str]:
        out = _slug_problems("key", self.key, PLACE_COMPLEX_KEY_MAX)
        if not self.name.strip():
            out.append("name: порожня назва")
        if self.key.startswith("_"):
            out.append("key: «_…» зарезервовано (_none, _unknown)")
        if self.within == self.key:
            out.append("within: ЖК не може бути всередині себе")
        return out


@dataclass(frozen=True)
class PlacesComplexesConfig:
    """`config/places/complexes.toml` — ЖК: написання, id DOM.RIA і LUN, район (E10, D57)."""

    # id ЖК DOM.RIA з кількома різними назвами: для них ЖК визначається за назвою, а не
    # за id (Етап 0: ria:6420 SHEPIT/Стожари, ria:8732 Афини/River Stone, ria:7845 …).
    ria_id_by_name: tuple[int, ...]
    complex: tuple[PlaceComplex, ...] = field(**_limits(min_len=1))

    def problems(self) -> list[str]:
        out: list[str] = []
        keys = [c.key for c in self.complex]
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            out.append(f"complex: ключі повторюються: {', '.join(dup)}")
        by_key = {c.key: c for c in self.complex}
        for c in self.complex:
            if c.within and c.within not in by_key:
                out.append(f"complex {c.key}: within {c.within!r} — такого ЖК немає")
            elif c.within and by_key[c.within].kind != "umbrella":
                out.append(f"complex {c.key}: within {c.within!r} — не парасолька "
                           f"(kind = umbrella)")
            if c.kind == "umbrella" and c.within:
                out.append(f"complex {c.key}: парасолька не буває всередині іншої")
        out += self._directory_problems()
        return out

    def _directory_problems(self) -> list[str]:
        """Перевірка довідника цілком (з districts.toml і rules.toml): колізії псевдонімів,
        райони ЖК, id. Помилки самих тих файлів покаже їхня власна перевірка."""
        try:
            districts = load("places/districts")
            rules = load("places/rules")
        except ConfigError:
            return []
        from .places.directory import DirectoryError, Directory

        try:
            Directory.build(districts, self, rules)
        except DirectoryError as e:
            return [f"довідник: {line}" for line in e.problems]
        return []


@dataclass(frozen=True)
class PlacesNormalize:
    # Префікси, що прибираються перед порівнянням назв, — окремо для районів і для ЖК.
    # «житловий район» — НЕ префікс ЖК: «Житловий район Manhattan» — парасолька, а не
    # ЖК Manhattan (D57).
    district_prefixes: tuple[str, ...]
    complex_prefixes: tuple[str, ...]
    strip_suffixes: tuple[str, ...]
    quotes: str
    # Пари «латинська літера + кирилична двійниця» («ii», «aа»): у змішаному слові
    # літери зводяться до письма більшості (Пасiчна → Пасічна, Comfогt → Comfort).
    homoglyphs: tuple[str, ...] = field(**_limits(min_len=1))

    def problems(self) -> list[str]:
        return [f"homoglyphs: {p!r} — дві літери: латинська, потім кирилична"
                for p in self.homoglyphs if len(p) != 2]


@dataclass(frozen=True)
class PlacesTiers:
    district: tuple[str, ...] = field(**_limits(min_len=1, choices=PLACE_DISTRICT_TIERS))
    complex: tuple[str, ...] = field(**_limits(min_len=1, choices=PLACE_COMPLEX_TIERS))
    # Слабкі ступені вмикаються лише після нічного проходу LUN і заміру точності (E11).
    coords_enabled: bool
    text_enabled: bool


@dataclass(frozen=True)
class PlacesCoords:
    precise_geo: tuple[str, ...] = field(**_limits(min_len=1))
    district_k: int = field(**_limits(min=1))
    district_radius_m: float = field(**_limits(min=1))
    district_min_refs: int = field(**_limits(min=1))
    district_agree: float = field(**_limits(min=0.5, max=1))
    complex_radius_m: float = field(**_limits(min=1))
    complex_min_refs: int = field(**_limits(min=1))
    complex_sources: tuple[str, ...]
    complex_markets: tuple[str, ...] = field(**_limits(choices=("primary", "secondary",
                                                                  "unknown")))
    validate_exclude_m: float = field(**_limits(min=0))
    validate_sample: int = field(**_limits(min=10))
    min_precision_district: float = field(**_limits(min=0, max=1))
    min_precision_complex: float = field(**_limits(min=0, max=1))


@dataclass(frozen=True)
class PlacesText:
    fields: tuple[str, ...] = field(**_limits(min_len=1, choices=("title", "description")))
    markers: tuple[str, ...] = field(**_limits(min_len=1))
    min_precision: float = field(**_limits(min=0, max=1))


@dataclass(frozen=True)
class PlacesLink:
    min_share: float = field(**_limits(min=0.5, max=1))
    min_n: int = field(**_limits(min=1))


@dataclass(frozen=True)
class PlacesSecondary:
    # [] — «не в ЖК» вимкнено (рецензія E10: точність 59% — тимчасово, до перевіреної
    # вибірки власника; D57).
    markets: tuple[str, ...] = field(**_limits(choices=("primary", "secondary", "unknown")))
    forbid_zhk_words: bool


@dataclass(frozen=True)
class PlacesAddr:
    # Мітка села LUN відкидається, якщо стільки оголошень DOM.RIA ІНШИХ квартир того самого
    # будинку кладуть його в міський район (рецензія E10: Фізкультурна 27 — «Крихівці»
    # LUN проти 176 «Бам» DOM.RIA).
    village_veto_min_ria: int = field(**_limits(min=1))


@dataclass(frozen=True)
class PlacesReassign:
    # `places reassign --apply` відмовляє без успішного бекапу, свіжішого за це (хв).
    backup_max_age_min: float = field(**_limits(min=1))


@dataclass(frozen=True)
class PlacesArea:
    unknown_counts_as_city: bool


@dataclass(frozen=True)
class PlacesGuard:
    # Чи можна кроку змінювати НЕПОРОЖНІ ключі (ні: зміна лише рахується як
    # would_change і видна на /status; інтеграція, конфлікт 13).
    allow_changes: bool

    def problems(self) -> list[str]:
        if self.allow_changes:
            return ["allow_changes: true — лише за рішенням власника (D57); код такого "
                    "режиму не має"]
        return []


@dataclass(frozen=True)
class PlacesAssign:
    batch_rows: int = field(**_limits(min=1, max=200))


@dataclass(frozen=True)
class PlacesStatus:
    unknown_limit: int = field(**_limits(min=1))
    unknown_min_count: int = field(**_limits(min=1))
    examples: int = field(**_limits(min=0, max=10))


@dataclass(frozen=True)
class PlacesAudit:
    core_kinds: tuple[str, ...] = field(**_limits(min_len=1))
    complex_min_audits: int = field(**_limits(min=1))


@dataclass(frozen=True)
class PlacesSources:
    lun_geo_types: tuple[str, ...] = field(**_limits(min_len=1))
    # flombu: місто чи ні — за координатами (BBOX, як normalize.in_ivano_frankivsk), а не
    # за текстом адреси (внутрішнє рішення D47 п. 8; збір flombu 27 → ~400, D57).
    flombu_city_by_coords: bool


@dataclass(frozen=True)
class PlacesLabels:
    hromada: str
    outside: str
    unknown_district: str
    unknown_complex: str
    none_complex: str
    area_city: str
    area_all: str
    how: dict[str, str]


@dataclass(frozen=True)
class PlacesRulesConfig:
    """`config/places/rules.toml` — нормалізація назв, драбина ступенів, пороги (E10, D57)."""

    normalize: PlacesNormalize
    tiers: PlacesTiers
    coords: PlacesCoords
    text: PlacesText
    link: PlacesLink
    secondary: PlacesSecondary
    addr: PlacesAddr
    area: PlacesArea
    guard: PlacesGuard
    reassign: PlacesReassign
    assign: PlacesAssign
    status: PlacesStatus
    audit: PlacesAudit
    sources: PlacesSources
    labels: PlacesLabels

    def problems(self) -> list[str]:
        known = (set(PLACE_DISTRICT_TIERS) | set(PLACE_COMPLEX_TIERS)
                 | {"secondary", "addr_area"})
        return [f"labels.how.{k}: невідомий ступінь" for k in self.labels.how
                if k not in known]


@dataclass(frozen=True)
class CyclePlaces:
    timeout_min: float = field(**_limits(min=1, max=60))
    # Крок «райони й ЖК» у циклі — лише після перегляду вибірки власником (`places
    # sample` → перегляд → ручний `places assign` → true; рецензія E10, D57): ключі
    # «лише туди, де порожньо» потім не виправити без `places reassign`.
    enabled: bool


@dataclass(frozen=True)
class CycleConfig:
    """`config/cycle.toml` — стелі НОВИХ кроків циклу (інтеграція, конфлікт 11; E10, D57).

    Старі кроки — як і досі, runner.TASK_TIMEOUTS (їх не переносили, щоб не міняти
    робочого). Зламаний файл не зупиняє циклу: крок без стелі не запускається (журнал).
    """

    places: CyclePlaces


# --- Тривоги на два рівні й щоденне зведення (хвиля W3, D58) ------------------------------

ALERT_LEVELS = ("critical", "warning")


def _tz_problems(where: str, name: str) -> list[str]:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return [f"{where}: невідомий часовий пояс {name!r}"]
    return []


@dataclass(frozen=True)
class AlertsDigest:
    # «ГГ:ХХ» місцевого часу (`timezone`): перший запуск сторожа після цього часу шле
    # зведення за цю місцеву дату, якщо його ще не надіслано.
    at: str
    timezone: str
    max_chars: int = field(**_limits(min=500, max=4000))
    # Скільки годин зниклі попередження лишаються в стані сторожа (для зведення).
    keep_resolved_hours: float = field(**_limits(min=24))
    # Найдовше вікно «з попереднього зведення» (пропущені дні не роздувають розділи).
    max_window_hours: float = field(**_limits(min=24))

    def problems(self) -> list[str]:
        out = [] if hhmm_minutes(self.at) is not None else [f"at: {self.at!r} — час «ГГ:ХХ»"]
        return out + _tz_problems("timezone", self.timezone)


@dataclass(frozen=True)
class AlertsSite:
    local_url: str = field(**_limits(prefix="http://"))
    check_public: bool
    public_path: str = field(**_limits(prefix="/"))
    timeout_s: float = field(**_limits(min=1, max=60))
    fail_runs: int = field(**_limits(min=1))


@dataclass(frozen=True)
class AlertsIntegrity:
    enabled: bool
    at: str
    max_seconds: float = field(**_limits(min=1, max=240))
    databases: tuple[str, ...] = field(**_limits(min_len=1, choices=("realty", "ops")))

    def problems(self) -> list[str]:
        return [] if hhmm_minutes(self.at) is not None else [f"at: {self.at!r} — час «ГГ:ХХ»"]


@dataclass(frozen=True)
class AlertsUnits:
    repeat_minutes: float = field(**_limits(min=1))
    log_lines: int = field(**_limits(min=0, max=40))


@dataclass(frozen=True)
class AlertsConfig:
    """`config/alerts.toml` — тривоги на два рівні (рішення власника 08.10, D55 п. 6; D58).

    `levels` — префікс ключа тривоги → critical (одразу) чи warning (у щоденне зведення);
    ключ без явного рівня — critical. `actions` — рядок «що робити» критичного
    повідомлення ({arg} — частина ключа після «:»). Решта — зведення, сайт, цілісність
    бази, впалі служби (`cli.py alert unit-failed`).
    """

    levels: dict[str, str]
    actions: dict[str, str]
    digest: AlertsDigest
    site: AlertsSite
    integrity: AlertsIntegrity
    units: AlertsUnits

    def problems(self) -> list[str]:
        import re

        out: list[str] = []
        for key, level in self.levels.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*(?::[a-z0-9][a-z0-9_.@-]*)?", key):
                out.append(f"levels.{key}: префікс — малі латинські, цифри, «-», «_», "
                           f"щонайбільше одне «:»")
            if level not in ALERT_LEVELS:
                out.append(f"levels.{key}: {level!r} — не з переліку {list(ALERT_LEVELS)}")
        for key, text in self.actions.items():
            if key not in self.levels:
                out.append(f"actions.{key}: такого префікса немає в [levels]")
            if not text.strip():
                out.append(f"actions.{key}: порожньо")
        return out


@dataclass(frozen=True)
class SampleRun:
    per_source: int = field(**_limits(min=1, max=1000))
    canaries_per_host: int = field(**_limits(min=0, max=100))
    # Темп хостів: "cycle" — `delay` хоста, як крок циклу; "night" — не швидше за повний
    # збір джерела (policy.pace).
    pace: str = field(**_limits(choices=("cycle", "night")))
    max_minutes: float = field(**_limits(min=1, max=90))
    lock_wait_minutes: float = field(**_limits(min=0, max=60))
    seed: int


@dataclass(frozen=True)
class SampleVerdict:
    max_removed_share: float = field(**_limits(min=0, max=1))
    min_checked: int = field(**_limits(min=1))
    examples: int = field(**_limits(min=0, max=100))
    # Тривога за прогоном тримається до наступного, але не довше за стільки діб.
    alert_days: float = field(**_limits(min=1))


@dataclass(frozen=True)
class SampleHints:
    enabled: bool


@dataclass(frozen=True)
class SampleConfig:
    """`config/sample.toml` — контрольна вибірка Блоку 1 (`cli.py liveness sample`, D58).

    Випадкові актуальні оголошення кожного джерела + контрольні (відомо живі) — тим
    самим підписом, що й перевірка актуальності, під замком циклу; стану оголошень НЕ
    змінює. Межі запобіжника (`share`, `min_checked`) — з liveness.toml [fuse].
    """

    run: SampleRun
    verdict: SampleVerdict
    hints: SampleHints


# --- Тип продавця: ДОКАЗИ (Блок 3, крок E11, D60) -------------------------------------------
# Лише те, що потрібно збирачам і нічним роботам, щоб ЗБЕРІГАТИ сирі докази
# (listings.seller_evidence, seller_profile — лише туди, де порожньо). Класи, правила,
# пороги й інтерфейс — наступні кроки Блоку 3 (E12–E13), разом із кодом, що їх читає.
# Імена, телефони, аватари, адреси профілів, пошта — ніколи: схема не пропускає шляхів
# через такі поля (ownerPhoneId flombu — теж: похідний від телефону).
SELLER_FORBIDDEN_SEGMENTS = frozenset({
    "phone", "phones", "viberphone", "hiddenphones", "phonesinfo_phone", "name", "avatar",
    "tgusername", "imageurl", "url", "email", "ownerphoneid", "lastlogindate", "bankid",
    "diia"})
SELLER_TAB_KEYS = ("private", "business")


def _evidence_key_problems(where: str, name: str) -> list[str]:
    import re

    return ([] if re.fullmatch(r"[a-z][a-z0-9_]{1,31}", name)
            else [f"{where}: ім'я ключа {name!r} — малі латинські, цифри й _ (≤32)"])


def _evidence_path_problems(where: str, path: str) -> list[str]:
    """Шлях у JSON джерела: «a.b» — скаляр; «a?» — лише наявність; «a[].b» — чи є
    хоч один елемент списку a з істинним b (лише логічне)."""
    import re

    out = []
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\[\])?(?:\.[A-Za-z_]\w*(?:\[\])?)*\??", path) \
            or path.rstrip("?").endswith("[]") or path.count("[]") > 1:
        out.append(f"{where}: шлях {path!r} — «a.b», «a?» чи «a[].b»")
    segs = [s.replace("[]", "").lower() for s in path.rstrip("?").split(".")]
    bad = [s for s in segs if s in SELLER_FORBIDDEN_SEGMENTS]
    if bad:
        out.append(f"{where}: шлях {path!r} веде до {bad} — імена, телефони, контакти не "
                   f"зберігаємо")
    return out


@dataclass(frozen=True)
class SellerFeed:
    """Поля об'єкта стрічки (LUN — payload, flombu — attributes JSON:API)."""

    fields: dict[str, str]
    # Позначка «стрічку з цим оголошенням бачили» (дата) — пишеться завжди: лише з нею
    # порожній доказ означає «джерело нічого не сказало», а не «не дивились».
    checked_key: str

    def problems(self) -> list[str]:
        out = _evidence_key_problems("checked_key", self.checked_key)
        for name, path in self.fields.items():
            out += _evidence_key_problems(f"fields.{name}", name)
            out += _evidence_path_problems(f"fields.{name}", path)
        if self.checked_key in self.fields:
            out.append("checked_key: збігається з ключем fields")
        return out


@dataclass(frozen=True)
class SellerOlx:
    """Сторінка оголошення OLX: чип «Приватна особа»/«Бізнес» (листовий вузол без
    двокрапки в контейнері параметрів — той самий, що читає збір: sources/olx.DETAIL),
    прапорці, «Тип угоди», ключ профілю."""

    chip_key: str
    chip_values: dict[str, str]
    flag_values: dict[str, str]
    deal_key: str
    deal_param: str
    deal_values: dict[str, str]
    profile_selector: str
    # Префікс ключа профілю → регулярний вираз з ОДНІЄЮ групою (slug / піддомен).
    profile_patterns: dict[str, str]
    checked_key: str

    def problems(self) -> list[str]:
        import re

        out = []
        for name in ("chip_key", "deal_key", "checked_key"):
            out += _evidence_key_problems(name, getattr(self, name))
        for text, code in self.chip_values.items():
            if code not in SELLER_TAB_KEYS:
                out.append(f"chip_values.{text}: {code!r} — не з {list(SELLER_TAB_KEYS)}")
        for text, key in self.flag_values.items():
            out += _evidence_key_problems(f"flag_values.{text}", key)
        for text, code in self.deal_values.items():
            out += _evidence_key_problems(f"deal_values.{text}", code)
        for prefix, pattern in self.profile_patterns.items():
            if not re.fullmatch(r"olx:[a-z]+:", prefix):
                out.append(f"profile_patterns.{prefix}: префікс «olx:<вид>:»")
            problems = _regex_problems(f"profile_patterns.{prefix}", pattern)
            if not problems and re.compile(pattern).groups != 1:
                problems = [f"profile_patterns.{prefix}: рівно одна група (slug)"]
            out += problems
        return out


@dataclass(frozen=True)
class SellerOlxTabs:
    """Вкладки пошуку OLX «Приватні»/«Бізнес» — членство оголошень (Блок 3, E11, D60)."""

    param: str
    values: dict[str, str]
    # Підпис вкладки, що має бути АКТИВНОЮ на відрендереній сторінці: інакше параметр
    # не спрацював, і членства не пишемо (живої проби параметра ще не було).
    labels: dict[str, str]
    active_selector: str
    card_selector: str
    # Картки з такою причиною показу (search_reason=…) у членство не беремо.
    skip_card_reasons: tuple[str, ...]
    # Ключі доказу в seller_evidence: вкладка й дата.
    tab_key: str
    tab_at_key: str
    rooms_param: str
    rooms: tuple[str, ...] = field(**_limits(min_len=1))
    price_from_param: str
    price_to_param: str
    price_bands_usd: tuple[tuple[int, ...], ...] = field(**_limits(min_len=1))
    # Мітка вкладки йде в seller_evidence лише якщо на оголошеннях, де відомі і вкладка,
    # і чип сторінки деталей, збіг ≥ chip_min_agreement при n ≥ chip_min_n.
    chip_min_agreement: float = field(**_limits(min=0.5, max=1))
    chip_min_n: int = field(**_limits(min=1))
    member_max_age_days: float = field(**_limits(min=1))

    def problems(self) -> list[str]:
        out = []
        for name in ("values", "labels"):
            if set(getattr(self, name)) != set(SELLER_TAB_KEYS):
                out.append(f"{name}: ключі мають бути рівно {list(SELLER_TAB_KEYS)}")
        for name in ("tab_key", "tab_at_key"):
            out += _evidence_key_problems(name, getattr(self, name))
        for i, band in enumerate(self.price_bands_usd):
            if len(band) != 2 or band[0] < 0 or (band[1] and band[1] <= band[0]):
                out.append(f"price_bands_usd[{i}]: [від, до] (до = 0 — без стелі)")
        return out


@dataclass(frozen=True)
class SellerRieltor:
    """Картка rieltor.ua (LUN→rieltor): роль («Рієлтор»/«Власник»), агенція."""

    role_selector: str
    role_key: str
    # Лише ці тексти ролі зберігаються як є; будь-що інше — `role_other` (у вузлі ролі
    # могло б опинитись ім'я — тоді його не збережемо).
    role_values: tuple[str, ...] = field(**_limits(min_len=1))
    role_other: str
    agency_selector: str
    has_agency_key: str
    # Агенція — непрозорий id (хеш піддомену чи шляху посилання), не адреса: піддомен
    # агенції буває номером телефону (D52).
    agency_key: str
    agency_prefix: str
    checked_key: str

    def problems(self) -> list[str]:
        out = []
        for name in ("role_key", "has_agency_key", "agency_key", "checked_key"):
            out += _evidence_key_problems(name, getattr(self, name))
        if not self.agency_prefix.endswith(":"):
            out.append("agency_prefix: має закінчуватись «:»")
        return out


@dataclass(frozen=True)
class SellerProfile:
    """Непрозорий id профілю (OLX, агенція rieltor): хеш slug-а, без імен і адрес."""

    # Байтів хешу (blake2b) — довжина id у hex удвічі більша.
    hash_bytes: int = field(**_limits(min=6, max=16))
    # Slug із більшою кількістю цифр — схожий на телефон: не зберігаємо зовсім (хеш
    # номера — теж «похідне від телефону», як ownerPhoneId flombu).
    max_digits: int = field(**_limits(min=0, max=9))


@dataclass(frozen=True)
class SellerCoverage:
    """Покриття доказами для `/api/status/night` і щоденного зведення."""

    # Група «джерело>сімейство сайту» → ключі seller_evidence, наявність будь-якого з
    # яких означає «докази є» (і кожен рахується окремо).
    groups: dict[str, tuple[str, ...]]
    labels: dict[str, str]

    def problems(self) -> list[str]:
        out = [f"labels: бракує підпису групи {g}" for g in self.groups if g not in self.labels]
        for g, keys in self.groups.items():
            if ">" not in g:
                out.append(f"groups.{g}: «джерело>сімейство»")
            for k in keys:
                out += _evidence_key_problems(f"groups.{g}", k)
        return out


@dataclass(frozen=True)
class SellerConfig:
    """`config/seller.toml` — Блок 3, тип продавця: поки що лише ДОКАЗИ (крок E11, D60).

    Що брати з об'єкта стрічки LUN і flombu, зі сторінки OLX (чип, «Тип угоди», профіль),
    з вкладок пошуку OLX і з картки rieltor.ua. Пишеться лише туди, де порожньо
    (seller_evidence — нові ключі, seller_profile — якщо NULL). DOM.RIA — config/liveness.toml
    [capture]. Класифікація — E12.
    """

    lun: SellerFeed
    flombu: SellerFeed
    olx: SellerOlx
    olx_tabs: SellerOlxTabs
    rieltor: SellerRieltor
    profile: SellerProfile
    coverage: SellerCoverage

    def problems(self) -> list[str]:
        keys = [self.lun.checked_key, self.flombu.checked_key, self.olx.checked_key,
                self.rieltor.checked_key]
        return ["checked_key: позначки джерел мають різнитися"] if len(set(keys)) < 4 else []


# Реєстр тем: ім'я файлу без .toml (з підтекою, якщо є) → схема.
SCHEMAS: dict[str, type] = {
    "speed": SpeedConfig,
    "liveness": LivenessConfig,
    "links": LinksConfig,
    "privacy": PrivacyConfig,
    "night": NightConfig,
    "places/districts": PlacesDistrictsConfig,
    "places/complexes": PlacesComplexesConfig,
    "places/rules": PlacesRulesConfig,
    "cycle": CycleConfig,
    "alerts": AlertsConfig,
    "sample": SampleConfig,
    "seller": SellerConfig,
}


# --- Перевірка ----------------------------------------------------------------------------


def _type_name(value) -> str:
    return {bool: "логічне", int: "ціле", float: "дробове", str: "рядок",
            list: "список", dict: "таблиця"}.get(type(value), type(value).__name__)


def _convert(value, tp, where: str, errors: list[str]):
    """Значення з TOML → значення схеми; помилки дописує в `errors`."""
    origin = typing.get_origin(tp)
    if is_dataclass(tp):
        if not isinstance(value, dict):
            errors.append(f"{where}: очікувалась таблиця [{where}], а є {_type_name(value)}")
            return None
        return _build(tp, value, where, errors)
    if tp is bool:
        if type(value) is not bool:
            errors.append(f"{where}: очікувалось true/false, а є {_type_name(value)} {value!r}")
        return value
    if tp is int:
        # bool у Python — підклас int; true замість числа — помилка, а не 1.
        if type(value) is not int:
            errors.append(f"{where}: очікувалось ціле число, а є {_type_name(value)} {value!r}")
        return value
    if tp is float:
        if type(value) not in (int, float):
            errors.append(f"{where}: очікувалось число, а є {_type_name(value)} {value!r}")
            return value
        try:
            number = float(value)
        except OverflowError:
            # 400-значне ціле: інакше OverflowError проскочив би повз ConfigError,
            # і сайт (Watched ловить лише ConfigError) упав би на запиті.
            errors.append(f"{where}: число завелике ({str(value)[:20]}…)")
            return None
        if not math.isfinite(number):
            # nan і inf — чинний TOML, але будь-яке порівняння з nan хибне, тож
            # межі min/max їх не зупинили б; sleep(nan) падає, sleep(inf) — вічний.
            errors.append(f"{where}: очікувалось скінченне число, а є {value!r}")
            return None
        return number
    if tp is str:
        if not isinstance(value, str):
            errors.append(f"{where}: очікувався рядок, а є {_type_name(value)} {value!r}")
        return value
    if origin is tuple:
        args = typing.get_args(tp)
        if len(args) != 2 or args[1] is not Ellipsis:
            raise TypeError(f"схема {where}: підтримується лише tuple[T, ...]")
        if not isinstance(value, list):
            errors.append(f"{where}: очікувався список, а є {_type_name(value)} {value!r}")
            return value
        return tuple(_convert(v, args[0], f"{where}[{i}]", errors)
                     for i, v in enumerate(value))
    if origin is dict:
        key_t, val_t = typing.get_args(tp)
        if key_t is not str:
            raise TypeError(f"схема {where}: ключі словника — лише str")
        if not isinstance(value, dict):
            errors.append(f"{where}: очікувалась таблиця, а є {_type_name(value)}")
            return value
        return types.MappingProxyType({k: _convert(v, val_t, f"{where}.{k}", errors)
                                       for k, v in value.items()})
    raise TypeError(f"схема {where}: непідтримуваний тип {tp!r}")


def _check_limits(value, meta, where: str, errors: list[str]) -> None:
    if not meta or value is None:
        return
    items = value if isinstance(value, tuple) else (value,)
    if "min_len" in meta and isinstance(value, tuple) and len(value) < meta["min_len"]:
        errors.append(f"{where}: потрібно щонайменше {meta['min_len']} елемент(и)")
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            if "min" in meta and item < meta["min"]:
                errors.append(f"{where}: {item} менше за допустимий мінімум {meta['min']}")
            if "max" in meta and item > meta["max"]:
                errors.append(f"{where}: {item} більше за допустимий максимум {meta['max']}")
        if isinstance(item, str):
            if "choices" in meta and item not in meta["choices"]:
                errors.append(f"{where}: {item!r} — не з переліку {list(meta['choices'])}")
            if "prefix" in meta and not item.startswith(meta["prefix"]):
                errors.append(f"{where}: {item!r} має починатися з {meta['prefix']!r}")


def _build(cls, data: dict, where: str, errors: list[str]):
    hints = typing.get_type_hints(cls)
    names = [f.name for f in fields(cls)]
    prefix = f"{where}." if where else ""
    missing = [n for n in names if n not in data]
    unknown = sorted(k for k in data if k not in names)
    for n in missing:
        errors.append(f"{prefix}{n}: ключа немає (значень за замовчуванням у коді немає)")
    for k in unknown:
        errors.append(f"{prefix}{k}: невідомий ключ (схема знає: {', '.join(names)})")
    if missing or unknown:
        return None
    kwargs = {}
    for f in fields(cls):
        here = f"{prefix}{f.name}"
        value = _convert(data[f.name], hints[f.name], here, errors)
        _check_limits(value, f.metadata, here, errors)
        kwargs[f.name] = value
    if errors:
        return None
    value = cls(**kwargs)
    # Обмеження між полями однієї таблиці (метод `problems` схеми): межі
    # min/max бачать лише одне поле.
    check = getattr(value, "problems", None)
    if check is not None:
        errors.extend(f"{prefix}{msg}" for msg in check())
        if errors:
            return None
    return value


def _read(name: str) -> tuple[Path, dict]:
    path = path_for(name)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"{path}: файлу немає") from None
    except OSError as e:
        raise ConfigError(f"{path}: не читається ({e})") from None
    try:
        return path, tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path}: не UTF-8 ({e})") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: помилка TOML — {e}") from None


def _schema(name: str) -> type:
    try:
        return SCHEMAS[name]
    except KeyError:
        raise ConfigError(f"невідома тема конфігу «{name}» (відомі: "
                          f"{', '.join(sorted(SCHEMAS))})") from None


def _plain(value):
    """Перевірене значення → JSON-сумісне (dataclass → dict, кортеж → список)."""
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, (dict, types.MappingProxyType)):
        return {k: _plain(v) for k, v in value.items()}
    return value


def digest_of(value) -> str:
    """sha256 перевірених значень теми.

    Саме значень, а не байтів чи сирого TOML: коментар, порядок секцій або
    `30` замість `30.0` у дробовому полі версію не змінюють — це те саме число.
    """
    canon = json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _validated(name: str):
    schema = _schema(name)
    path, data = _read(name)
    errors: list[str] = []
    value = _build(schema, data, "", errors)
    if errors:
        raise ConfigError(f"{path}:\n  " + "\n  ".join(errors))
    return value, digest_of(value)


def load(name: str):
    """Тема `name` як заморожений dataclass; будь-яка невідповідність — ConfigError."""
    return _validated(name)[0]


def load_with_hash(name: str) -> tuple[object, str]:
    """(значення, хеш) з ОДНОГО читання файлу — для кроків циклу й нічних робіт.

    Хеш у записі прогону має описувати саме ту версію, з якою прогін
    працював; окремий виклик `config_hash` пізніше міг би прочитати вже
    змінений файл.
    """
    return _validated(name)


def config_hash(name: str) -> str:
    """Хеш файлу теми, що лежить на диску ЗАРАЗ (для `config check` і перевірок).

    Не для записів прогонів: сайт може ще працювати на попередній версії
    (перечитування раз на 60 с, або остання чинна після зламаної правки) —
    там беріть `get_with_hash`/`get_hash`, а кроки циклу — `load_with_hash`.
    """
    return _validated(name)[1]


# --- Для довгоживучих процесів -------------------------------------------------------------


def _stamp(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class Watched:
    """Тема для сайту: перечитується за mtime не частіше ніж раз на `interval_s`.

    Перше читання суворе (ConfigError). Якщо файл змінили й новий варіант
    не проходить схему, лишається попередній чинний конфіг і пишеться
    помилка в журнал: сайт не має падати через правку, яку `config check`
    мав зупинити ще до розгортання. Хеш (`digest`) завжди належить чинному
    значенню, а не файлу на диску.
    """

    def __init__(self, name: str, *, interval_s: float = RELOAD_INTERVAL_S,
                 clock=time.monotonic) -> None:
        self.name = name
        self.path = path_for(name)
        self.interval_s = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        # Позначка файлу — ДО читання: правка між читанням і stat інакше
        # записала б нову позначку поруч зі старим значенням і загубилась би
        # до наступної зміни файлу.
        self._stamp = _stamp(self.path)
        self._value, self._digest = load_with_hash(name)
        self._checked = clock()
        self.reloads = 0

    def get_with_hash(self) -> tuple[object, str]:
        now = self._clock()
        if now - self._checked < self.interval_s:
            return self._value, self._digest
        with self._lock:
            if now - self._checked < self.interval_s:
                return self._value, self._digest
            self._checked = now
            stamp = _stamp(self.path)
            if stamp == self._stamp:
                return self._value, self._digest
            try:
                self._value, self._digest = load_with_hash(self.name)
                self.reloads += 1
            except ConfigError as e:
                log.error("Конфіг %s змінено, але він не проходить перевірку — "
                          "лишаю попередній чинний.\n%s", self.name, e)
            self._stamp = stamp
            return self._value, self._digest

    def get(self):
        return self.get_with_hash()[0]

    @property
    def digest(self) -> str:
        """Хеш версії, що діє зараз (після перевірки за mtime, як і `get`)."""
        return self.get_with_hash()[1]


_watched: dict[tuple[str, str], Watched] = {}
# Невдале перше читання: (коли, помилка). Поки файл зламаний, кожен запит сайту
# інакше заново читав і розбирав би його — без обмеження «раз на 60 с».
_failed: dict[tuple[str, str], tuple[float, ConfigError]] = {}
_watched_lock = threading.Lock()
_clock = time.monotonic


def _watched_for(name: str) -> Watched:
    key = (str(config_dir()), name)
    with _watched_lock:
        w = _watched.get(key)
        if w is not None:
            return w
        failed = _failed.get(key)
        now = _clock()
        if failed is not None and now - failed[0] < RELOAD_INTERVAL_S:
            raise failed[1]
        try:
            w = _watched[key] = Watched(name, clock=_clock)
        except ConfigError as e:
            _failed[key] = (now, e)
            raise
        _failed.pop(key, None)
        return w


def get(name: str):
    """Чинний конфіг теми для довгоживучого процесу (див. `Watched`)."""
    return _watched_for(name).get()


def get_with_hash(name: str) -> tuple[object, str]:
    """(чинне значення, його хеш) — для записів сайту (web_timings тощо)."""
    return _watched_for(name).get_with_hash()


def get_hash(name: str) -> str:
    """Хеш версії, з якою сайт працює зараз (не файлу на диску)."""
    return get_with_hash(name)[1]


# --- Перевірка всієї теки (cli.py config check) --------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    name: str
    path: Path
    ok: bool
    message: str
    digest: str | None


def check_all() -> list[CheckResult]:
    """Кожен файл теки проходить свою схему, і для кожної схеми є файл."""
    base = config_dir()
    results: list[CheckResult] = []
    if not base.is_dir():
        return [CheckResult("—", base, False, "теки конфігів немає", None)]
    seen = set()
    for path in sorted(base.rglob("*.toml")):
        name = path.relative_to(base).with_suffix("").as_posix()
        seen.add(name)
        if name not in SCHEMAS:
            results.append(CheckResult(name, path, False,
                                       "немає схеми для цієї теми в realty/configfiles.py",
                                       None))
            continue
        try:
            digest = config_hash(name)
        except ConfigError as e:
            results.append(CheckResult(name, path, False, str(e), None))
        else:
            results.append(CheckResult(name, path, True, "ok", digest))
    for name in sorted(set(SCHEMAS) - seen):
        results.append(CheckResult(name, path_for(name), False,
                                   "файлу немає, а схема теми є", None))
    return results
