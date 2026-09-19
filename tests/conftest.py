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


# Змінні оточення мають бути виставлені ДО імпорту realty.config: там
# DB_URL читається один раз, а load_dotenv наявних змінних не перекриває.
os.environ["DB_URL"] = f"sqlite:///{_copy('realty.db')}"
os.environ["OPS_DB_URL"] = f"sqlite:///{_copy('ops.db')}"
