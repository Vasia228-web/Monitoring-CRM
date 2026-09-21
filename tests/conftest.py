"""Тести ніколи не працюють із робочою базою.

До 19.09.2026 веб-тести читали й ПИСАЛИ справжню `data/realty.db`: кожен
прогін додавав чотири фальшиві скарги «дані не збігаються» й накручував
перегляди карток, від яких залежить черга перевірки актуальності. Знайдено
після перенесення бази на Fedora: 70 із 74 скарг у базі — від тестів.

Тепер до першого імпорту `realty` тести отримують КОПІЮ бази (через SQLite
backup API) у тимчасовій теці й працюють лише з нею. Якщо робочої бази немає
(свіжа машина), копія порожня — тести, яким потрібні справжні дані, це покажуть.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="realty-tests-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)


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
