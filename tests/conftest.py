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

# Як після розгортання (`cli.py db migrate`, Блок 2 E4, D50): нові таблиці,
# колонки й індекси схеми — у КОПІЇ. Інакше тести з копією робочої бази падали б
# на кожному новому полі моделі, а запити сайту йшли б без індексів.
from realty.db import migrate as _migrate  # noqa: E402
_migrate()

# Перевірка при відкритті квартири (Блок 2, крок E5, D50): сайт ставить
# завдання в чергу й запускає окремий процес `cli.py lookup check`. У тестах
# процес НЕ запускається (він ходив би в мережу й писав у базу поза тестом):
# запуск лише записується, а тест, якому треба, виконує завдання сам тим самим
# кодом (realty.lookup.opened.run_job).
from realty.web import livecheck as _livecheck  # noqa: E402

LAUNCHED: list[int] = []


def _record_launch(job_id: int) -> str:
    LAUNCHED.append(int(job_id))
    return "test"


_livecheck.launch = _record_launch

# Замки й прапорець перевірки при відкритті — у тимчасовій теці, а не в data/
# репозиторію: на Mac розробника data/COLLECTOR_OFF існує (базу перенесено на
# Fedora), і кожне завдання закривалось би «skipped», а тест, що бере замок
# циклу, не має чіпати справжній data/cycle.lock.
from realty.lookup import opened as _opened  # noqa: E402

_opened.DRAIN_LOCK = _TMP / "lookup.lock"
_opened.CYCLE_LOCK = _TMP / "cycle.lock"
_opened.DISABLED_FLAG = _TMP / "COLLECTOR_OFF"

# Сторож (хвиля W3, D58): у тестах він не ходить на 127.0.0.1:8000 (там може працювати
# сайт розробника — результат залежав би від машини) і не читає всю копію бази
# PRAGMA quick_check на кожному watchdog.run. Тести цих перевірок підставляють свої.
# Черга недоставленого й журнал впалих служб — у тимчасовій теці, а не в data/.
from realty import watchdog as _watchdog  # noqa: E402

_watchdog.SITE_PROBE = lambda url, timeout: (True, "HTTP 200 (тест)")
_watchdog.INTEGRITY_CHECK = lambda path, deadline: "ok"
_watchdog.OUTBOX_PATH = _TMP / "alerts_outbox.json"
_watchdog.UNITS_PATH = _TMP / "unit_failures.json"
_watchdog.JOURNAL_TAIL = lambda unit, lines: []


@pytest.fixture(autouse=True)
def _fresh_livecheck():
    """Диспетчер перевірок — один на процес; у кожного тесту своя ops.db, тож
    номери завдань і «щойно запущений процес» попереднього тесту тут чужі."""
    _livecheck.LIVE.reset()
    yield
    _livecheck.LIVE.reset()

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
