"""Тести ніколи не працюють із робочою базою.

До 19.09.2026 веб-тести читали й ПИСАЛИ справжню `data/realty.db`: кожен
прогін додавав чотири фальшиві скарги «дані не збігаються» й накручував
перегляди карток, від яких залежить черга перевірки актуальності. Знайдено
після перенесення бази на Fedora: 70 із 74 скарг у базі — від тестів.

Тепер до першого імпорту `realty` тести отримують КОПІЮ бази (через SQLite
backup API) у тимчасовій теці й працюють лише з нею. Якщо робочої бази немає
(свіжа машина), копія порожня — тести, яким потрібні справжні дані, це покажуть.

І мережа: тести не виходять за межі цієї машини (D45, інцидент 1 — тестовий
Chromium сам довантажив ~77 ресурсів із CDN джерела). Будь-яке з'єднання не на
127.0.0.1/::1, запит httpx на чужий хост і запуск браузера Playwright падають
(`realty.netguard`), а кожна спроба — навіть проковтнута через
`except Exception` — валить тест, у якому сталася. Свідомі винятки — лише
позначками: `allow_browser` (локальний Chromium; його запити назовні однаково
обриваються) і `allow_network` (справжня мережа; таких тестів зараз немає).
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="realty-tests-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

# Заборона мережі вмикається раніше за будь-який імпорт коду системи: модуль
# може торкнутись мережі вже під час імпорту чи збору тестів.
sys.path.insert(0, str(ROOT))
from realty import netguard  # noqa: E402

# Дочірні процеси тестів (штучне джерело в test_timeouts) вмикають ту саму
# заборону за змінною й дописують спроби у файл — його перевіряє кожен тест.
_CHILD_LOG = _TMP / "netguard-children.log"
os.environ[netguard.ENV_FLAG] = "1"
os.environ[netguard.ENV_LOG] = str(_CHILD_LOG)
netguard.install()


def _copy(name: str) -> Path:
    target = _TMP / name
    source = ROOT / "data" / name
    if source.exists():
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    return target


def _repair(path: Path) -> Path:
    """Копію приводимо до схеми робочої бази на Fedora після 21.09.2026.

    База на MacBook лишається недоторканою й досі має бите посилання
    `price_events → listings_legacy`. Лагодимо лише КОПІЮ — тим самим
    ремонтом, що й робочу базу, — інакше з увімкненою перевіркою ключів
    тести писали б у схему, якої ніде в роботі вже немає.
    """
    if path.exists() and path.stat().st_size:
        import sys
        sys.path.insert(0, str(ROOT))
        from realty.schema_repair import repair
        rep = repair(path)
        assert rep.ok, f"ремонт копії бази не вдався: {rep.problems}"
    return path


# Змінні оточення мають бути виставлені ДО імпорту realty.config: там
# DB_URL читається один раз, а load_dotenv наявних змінних не перекриває.
os.environ["DB_URL"] = f"sqlite:///{_repair(_copy('realty.db'))}"
os.environ["OPS_DB_URL"] = f"sqlite:///{_copy('ops.db')}"

# Як сайт на старті (lifespan → init_db): доливаємо в копію нові колонки схеми.
# Інакше тести з копією робочої бази падали б на кожному новому полі моделі.
from realty.db import init_db as _init_db  # noqa: E402
_init_db()

# Курс НБУ: справжній `usd_uah_rate` ходить на bank.gov.ua під час кожного
# прогону конвеєра (to_uah). Досі тести тихо робили цей запит — заборона мережі
# його виявила. У тестах курс — резервний із конфігу: результат не залежить ні
# від мережі, ні від дня.
import functools  # noqa: E402

from realty import normalize as _normalize  # noqa: E402


@functools.lru_cache(maxsize=1)
def _offline_rate() -> float:
    return _normalize.FALLBACK_USD_UAH


_normalize.usd_uah_rate = _offline_rate


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_browser: тест запускає локальний Chromium; запити браузера "
                   "на нелокальні адреси однаково обриваються й валять тест")
    config.addinivalue_line(
        "markers", "allow_network: тест свідомо ходить у зовнішню мережу "
                   "(зараз таких немає; кожен такий тест — рішення, а не звичка)")


def _child_attempts_since(offset: int) -> list[str]:
    try:
        with _CHILD_LOG.open(encoding="utf-8") as fh:
            fh.seek(offset)
            return [line.rstrip("\n") for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def _child_log_size() -> int:
    try:
        return _CHILD_LOG.stat().st_size
    except FileNotFoundError:
        return 0


@pytest.fixture(autouse=True)
def _no_external_network(request):
    """Тест, що спробував вийти в мережу, падає — навіть якщо код проковтнув виняток."""
    network = request.node.get_closest_marker("allow_network") is not None
    browser = network or request.node.get_closest_marker("allow_browser") is not None
    start, child_start = netguard.mark(), _child_log_size()
    with netguard.permit(network=network, browser=browser):
        yield
    leaked = netguard.take_since(start) + _child_attempts_since(child_start)
    if leaked:
        pytest.fail("Тест намагався вийти в зовнішню мережу (заборонено, D45):\n  "
                    + "\n  ".join(leaked[:20]), pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    """Спроби поза тестами (під час імпорту чи збору) теж не мають пройти тихо."""
    stray = netguard.attempts()
    if stray:
        print("\nЗаблоковані спроби вийти в мережу поза тестами:\n  " + "\n  ".join(stray[:20]))
        session.exitstatus = 1
