"""Централізована конфігурація агрегатора."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_URL = os.getenv("DB_URL", f"sqlite:///{DATA_DIR / 'realty.db'}")

# --- Цільова локація: СУВОРО Івано-Франківськ ---------------------------------
CITY_UK = "Івано-Франківськ"
# Форми назви міста, які трапляються в різних джерелах (для гео-фільтра).
CITY_ALIASES = (
    "івано-франківськ",
    "івано-франківська",  # родовий відмінок у slug/адресах
    "ивано-франковск",
    "ivano-frankivsk",
    "ivano-frankovsk",
    "if",
)
# Bounding box міста (+ найближчі передмістя, що фактично є частиною ринку).
BBOX = {"south": 48.86, "west": 24.60, "north": 48.99, "east": 24.83}

# Ідентифікатори джерел, з'ясовані на етапі верифікації (див. probes/).
DOMRIA_STATE_ID = 15  # Івано-Франківська область
DOMRIA_CITY_ID = 15   # м. Івано-Франківськ
LUN_CITY_CODE = "if"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# --- Мережа -------------------------------------------------------------------
# Ліміт на ОДНУ фазу запиту (з'єднання, очікування чергового шматка відповіді).
# Сам по собі він не обмежує запит цілком: сайт, що віддає по байту раз на
# 20 секунд, ніколи його не перевищить. Для цього є REQUEST_TIMEOUT нижче.
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
# Пауза між запитами до одного хоста (секунди). Тримаємо ввічливий темп:
# він і безпечніший для джерела, і стабільніший для нас.
DEFAULT_DELAY = float(os.getenv("REQUEST_DELAY", "1.2"))
# Необов'язковий проксі (звичайна інфраструктура, вимикається порожнім значенням).
PROXY_URL = os.getenv("PROXY_URL") or None
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # кеш відповідей, секунди
CACHE_DIR = DATA_DIR / "cache"

# --- Ліміти часу ----------------------------------------------------------------
# Три рівні, і кожен — механізм у коді, а не домовленість:
#   запит   — один зовнішній запит від початку до кінця;
#   джерело — одне джерело в регулярному прогоні (окремий процес, який
#             вбивається, якщо не вклався — разом із браузером);
#   прогін  — увесь цикл: збір, перелік, перевірка, дублі, якість.
# 10.09.2026 прогін завис на 9 днів: браузер перестав відповідати, а операція,
# яка чекала на нього, не мала жодного ліміту. Планувальник не стартує новий
# прогін, поки живий старий, тож тиша тривала, доки хтось не помітив.
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "45"))
# Операція браузера — це кілька кроків (відкрити сторінку, дочекатися карток,
# зачекати, поки домалюється, забрати HTML), тому її стеля ширша за один запит.
BROWSER_OP_TIMEOUT = float(os.getenv("BROWSER_OP_TIMEOUT", "120"))
SOURCE_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT", str(30 * 60)))
# Звичайний цикл триває 30–46 хв (заміряно 09.09), інтервал — 3 год. Дві
# години — із запасом на повільний день і з гарантією, що цикл закінчиться
# до наступного старту.
RUN_TIMEOUT = float(os.getenv("RUN_TIMEOUT", str(120 * 60)))


# Власні стелі джерел. OLX іде через браузер і дотягує сторінку деталей для
# карток без кімнат: на Fedora звичайний прогін — 25 хв (заміряно 19.09, на
# MacBook — 15), тож загальні 30 хв — впритул.
SOURCE_TIMEOUTS = {"olx": 45 * 60}


def source_timeout(name: str) -> float:
    """Ліміт для конкретного джерела: SOURCE_TIMEOUT_OLX=… у .env перекриває все."""
    default = SOURCE_TIMEOUTS.get(name, SOURCE_TIMEOUT)
    return float(os.getenv(f"SOURCE_TIMEOUT_{name.upper()}", default))

# --- LLM-фолбек ---------------------------------------------------------------
LLM_ENABLED = os.getenv("LLM_FALLBACK", "1") not in ("0", "false", "False", "")
# Найдешевша й найшвидша модель — для вилучення полів із тексту цього досить.
# Дорожчі моделі для парсингу не використовуємо.
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5")
LLM_MAX_CALLS = int(os.getenv("LLM_MAX_CALLS", "25"))   # запобіжник витрат на прогін
# Стеля очищеного тексту в одному запиті — головний важіль вартості.
LLM_MAX_CHARS = int(os.getenv("LLM_MAX_CHARS", "2500"))

# --- Валюта -------------------------------------------------------------------
# Курс підтягується з НБУ; це значення — резерв, якщо API недоступне.
FALLBACK_USD_UAH = float(os.getenv("USD_UAH_RATE", "42.0"))
NBU_RATE_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"


@dataclass(frozen=True)
class SourceConfig:
    """Налаштування темпу й обсягу збору для одного джерела.

    `max_pages` — стеля щоденного (інкрементального) прогону.
    `full_pages` — стеля повного історичного збору; 0 означає «до кінця».
    `supports_recency` — чи джерело віддає найновіші оголошення першими. Лише
    для таких має сенс рання зупинка: побачили сторінку без новинок — далі
    йдуть уже відомі записи.
    """

    name: str
    enabled: bool = True
    max_pages: int = 5
    delay: float = DEFAULT_DELAY
    needs_browser: bool = False
    full_pages: int = 0
    full_delay: float | None = None
    supports_recency: bool = False
    extra: dict = field(default_factory=dict)

    def paced(self, mode: str) -> "SourceConfig":
        """Копія з лімітами й темпом під конкретний режим прогону."""
        if mode != "full":
            return self
        return replace(self, max_pages=self.full_pages or 10_000,
                       delay=self.full_delay or self.delay)


SOURCES: dict[str, SourceConfig] = {
    # Пошук RIA типово віддає спадний realty_id, тобто найновіші першими —
    # перевірено, окремий параметр сортування не потрібен.
    "domria": SourceConfig("domria", max_pages=8, delay=0.8,
                           full_pages=450, full_delay=0.9, supports_recency=True),
    # LUN не має робочого параметра сортування за датою: у стрічці новинки
    # перемішані зі старими, тому рання зупинка тут була б хибною.
    "lun": SourceConfig("lun", max_pages=10, delay=1.5,
                        full_pages=240, full_delay=1.6),
    # flombu ігнорує гео-фільтр і віддає всю область, тому міських лотів на
    # сторінці мало — обходимо всю видачу, вона невелика.
    "flombu": SourceConfig("flombu", max_pages=35, delay=1.0,
                           full_pages=40, full_delay=1.0),
    "olx": SourceConfig("olx", max_pages=5, delay=2.5, needs_browser=True,
                        full_pages=25, full_delay=2.8, supports_recency=True),
    # Каталог планувань забудовника: тематичні розділи, дат немає.
    "blago": SourceConfig("blago", max_pages=6, delay=1.5, full_pages=6),
}


# Скільки оголошень джерело віддає загалом — заміряно на повному прогоні.
# Потрібно, щоб дашборд показував, скільки ще лишилось зібрати.
EXPECTED_TOTALS: dict[str, int] = {
    "domria": 8488,
    "lun": 5475,
    "olx": 1000,     # більше OLX за раз не показує
    "flombu": 27,    # стільки там є саме по місту
    "blago": 90,
}


def enabled_sources() -> list[str]:
    only = os.getenv("SOURCES")
    if only:
        return [s.strip() for s in only.split(",") if s.strip() in SOURCES]
    return [n for n, c in SOURCES.items() if c.enabled]
