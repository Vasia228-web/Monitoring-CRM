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


@dataclass(frozen=True)
class SpeedOpenCheck:
    enabled: bool
    wait_max_s: float = field(**_limits(min=0))


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


@dataclass(frozen=True)
class SpeedRum:
    enabled: bool
    roles: tuple[str, ...] = field(**_limits(choices=("owner", "friend")))
    retention_days: int = field(**_limits(min=1))


@dataclass(frozen=True)
class SpeedProbe:
    urls: tuple[str, ...] = field(**_limits(min_len=1, prefix="/"))
    repeats: int = field(**_limits(min=1))
    pause_s: float = field(**_limits(min=0))
    timeout_s: float = field(**_limits(min=1))


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
    txn_watch: SpeedTxnWatch


# Реєстр тем: ім'я файлу без .toml (з підтекою, якщо є) → схема.
SCHEMAS: dict[str, type] = {
    "speed": SpeedConfig,
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
    return cls(**kwargs)


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
