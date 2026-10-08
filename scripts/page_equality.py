#!/usr/bin/env python
"""Рівність сторінок «до/після»: еталон від поточного коду і порівняння з ним.

  python scripts/page_equality.py snapshot --db КОПІЯ.db [--ops OPS.db] --out ТЕКА
  python scripts/page_equality.py compare  --db КОПІЯ.db [--ops OPS.db] --golden ТЕКА

Навіщо. Блок 2 промту 11 прискорює сайт кешами, індексами й іншим порядком
обчислень, а власник поставив умову: «ті самі дані, ті самі фільтри, та сама
пагінація; перевір, що вміст сторінок до і після збігається». Прототип Блоку 2
показав, що новий індекс може непомітно змінити вивід (позначка «найдешевше»
перескочила на інше джерело при рівних медіанах). Тому вміст перевіряє машина:
еталон знімається з ПОТОЧНОГО коду до будь-яких змін, після кожної зміни —
повне порівняння; очікувано — 0 розбіжностей.

Відтворюваність (без неї порівняння ловило б шум, а не зміни):
  * лише приватні копії: скрипт сам копіює байти --db і --ops у тимчасову
    теку (оригінал SQLite навіть не відкриває) і щоразу рендерить зі свіжої
    копії. Сторінка квартири збільшує лічильники переглядів, сайт закриває
    «завислі» прогони — ці записи не переходять у наступний прогін;
  * заморожений годинник: «зараз» — наступна повна година після найпізнішої
    позначки часу в даних (або --now); записаний у manifest, і порівняння
    бере його звідти. Часовий пояс — Europe/Kyiv;
  * .env не читається, змінні, що впливають на вивід, прибрані; тека data/
    підмінена приватною (alerts.json, public_url тощо не беруться з машини);
    пороги якості й налаштування аналітики — копії, збережені поруч з еталоном;
  * процеси з чужої машини вважаються мертвими (ops._process_alive → False), а
    «завислі» прогони закриваються один раз ДО рендеру — тож вивід не залежить
    від порядку адрес;
  * мережа заборонена (realty/netguard.py), сторінки квартир — лише з
    verify=0, дочірні процеси заборонені; PYTHONHASHSEED=0. Якщо рендер хоч
    раз спробував вийти в мережу (навіть коли код проковтнув помилку й
    підставив запасне значення), еталон не пишеться, а порівняння не
    зараховується — код виходу 2;
  * вхід вимкнено (AUTH_* порожні) — так сайт рендериться в розробці.

У manifest записано, з якого коду знято еталон: HEAD, перелік незакомічених
файлів застосунку (realty/, cli.py) і хеш їхньої різниці з HEAD — а також хеші
конфігів config/ того самого дерева коду: розбіжність «до/після» тоді можна
прив'язати до зміни коду чи порогів.

Фікстура: у копії «в обробку» беруться --in-progress квартир за фіксованим
seed (на копії Етапу 0 позначених 0, і вкладка «В обробці» була б порожньою).

Нормалізація однакова для еталону й порівняння (і повторно застосовується до
еталону) і прибирає лише те, що змінюється без зміни даних — див. NORMALIZATION.
Маскуємо якомога менше: те, що виводиться з даних і замороженого «зараз»
(«оновлено сьогодні», вік серцебиття воркера), лишається в порівнянні — саме
там видно застарілий кеш. Додати правило пізніше можна без нового еталону
(compare застосовує поточні правила й до еталону); прибрати — лише знявши
еталон заново.
Порівнюються код відповіді, Content-Type, Location і нормалізоване тіло.

Коди виходу: 0 — усе збіглось; 1 — є розбіжності; 2 — помилка входів.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import gzip
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import pkgutil
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from urllib.parse import urlencode

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
FORMAT = 1
TIMEZONE = "Europe/Kyiv"
DEFAULT_SEED = 20261007
PLACEHOLDER = "⟨нормалізовано⟩"

# Змінні оточення, від яких залежить вивід сайту. Прибираються, щоб еталон не
# залежав від машини: на Mac і на Fedora вони різні.
CLEARED_ENV = ("SOURCES", "DEDUP_RULES", "DEDUP_GEO_VETO_M", "DEDUP_PRICE_GAP",
               "DEDUP_NEWBUILD_ANY", "WORKER_IDLE_MIN", "WORKER_DOWN_MIN",
               "USD_UAH_RATE", "PUBLIC_DOMAIN", "CACHE_TTL", "PROXY_URL",
               "REALTY_CONFIG_DIR", "ANTHROPIC_API_KEY")

# --- Нормалізація -------------------------------------------------------------------------

# generated_at — мить відповіді (у Блоці 2 може стати часом побудови кешу);
# решта — ключі майбутніх кешів Блоку 2, яких у виводі ще немає. НЕ маскуються:
# age_min (вік серцебиття воркера) і «оновлено …» у шапці списку — при
# замороженому «зараз» і сталих даних вони детерміновані.
VOLATILE_JSON_KEYS = ("generated_at", "built_at", "age_seconds", "cache_age_s")
HTML_RULES = [
    # Позначені блоки, які Блок 2 додає навмисно: маячок RUM, банер перевірки при
    # відкритті, панель «Швидкість» і пауза опитування на /status (D50); Блок 1 (E8,
    # D52) — панель «Зняті оголошення» на /status і позначка «актуальність не
    # підтверджена» (Благо) у списку й на картці; Блок 5 (E14, D59) — поле пошуку
    # у верхній панелі й плашка «Знайдено за посиланням». Решта сторінки, зокрема
    # наявні панелі й рядки опитування, порівнюється повністю.
    (re.compile(r"<!--(rum|live-check|speed-panel|status-poll|liveness-panel|unconfirmed|"
                r"places-panel|find-box|find-hl)-->.*?<!--/\1-->", re.S), ""),
    # Одноразові токени (зараз їх немає; з'являться — не шумітимуть).
    (re.compile(r'(\snonce=")[^"]*(")'), rf"\g<1>{PLACEHOLDER}\g<2>"),
    (re.compile(r'(name="csrf[\w-]*"\s+value=")[^"]*(")', re.I), rf"\g<1>{PLACEHOLDER}\g<2>"),
]
NORMALIZATION = [
    f"JSON: значення ключів {', '.join(VOLATILE_JSON_KEYS)} на будь-якій глибині → "
    f"«{PLACEHOLDER}»; JSON переформатовано (indent=1, порядок ключів збережено)",
    'не маскуються: age_min і <div class="updated">оновлено …</div> (заморожене «зараз»)',
    "HTML: блоки <!--rum-->, <!--live-check-->, <!--speed-panel-->, <!--status-poll-->, "
    "<!--liveness-panel-->, <!--unconfirmed-->, <!--places-panel-->, <!--find-box--> і "
    "<!--find-hl--> (…<!--/назва-->) вирізано",
    "HTML: nonce=\"…\" і приховані csrf-поля → плейсхолдер",
]


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: (PLACEHOLDER if k in VOLATILE_JSON_KEYS and v is not None else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def normalize(body: bytes, content_type: str) -> bytes:
    """Ідемпотентна: normalize(normalize(x)) == normalize(x)."""
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind == "application/json":
        try:
            obj = json.loads(body)
        except ValueError:
            return body
        return (json.dumps(_scrub(obj), ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    if kind.startswith("text/"):
        text = body.decode("utf-8", errors="surrogateescape")
        for pattern, repl in HTML_RULES:
            text = pattern.sub(repl, text)
        return text.encode("utf-8", errors="surrogateescape")
    return body


# --- Входи --------------------------------------------------------------------------------


def _sha256(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _db_files(path: Path) -> list[Path]:
    """Файл бази й непорожній -wal: без журналу копія могла б бути неповною."""
    wal = path.with_name(path.name + "-wal")
    return [path] + ([wal] if wal.exists() and wal.stat().st_size else [])


def _private_copy(src: Path, dst: Path) -> dict:
    """Байтова копія (оригінал SQLite не відкривається) + швидка перевірка цілісності."""
    files = _db_files(src)
    digest = _sha256(*files)
    shutil.copyfile(src, dst)
    if len(files) > 1:
        shutil.copyfile(files[1], dst.with_name(dst.name + "-wal"))
    con = sqlite3.connect(dst)
    try:
        check = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if check != "ok":
        raise SystemExit(f"{src}: копія не пройшла quick_check ({check})")
    return {"path": str(src), "sha256": digest, "bytes": sum(f.stat().st_size for f in files)}


_TIME_COLUMNS = {
    "main": [("listings", "last_seen"), ("listings", "first_seen"), ("listings", "last_checked"),
             ("listings", "last_attempt"), ("listings", "quality_checked_at"),
             ("price_events", "observed_at"), ("check_events", "checked_at"),
             ("data_reports", "created_at")],
    "ops": [("runs", "started_at"), ("runs", "finished_at"), ("heartbeat", "beat_at"),
            ("cycles", "started_at"), ("cycles", "finished_at"), ("backups", "created_at"),
            ("dedup_audits", "at"), ("dedup_samples", "at")],
}


def _data_now(main: Path, ops: Path) -> _dt.datetime:
    """Наступна повна година після найпізнішої позначки часу (UTC) у копіях.

    Так «зараз» залежить лише від даних: два знімки тієї самої копії однакові,
    і жодна подія в даних не опиняється «в майбутньому».
    """
    latest = None
    for kind, path in (("main", main), ("ops", ops)):
        if not path.exists():                      # копії ops.db може не бути
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table, col in _TIME_COLUMNS[kind]:
                try:
                    v = con.execute(f'SELECT max("{col}") FROM "{table}"').fetchone()[0]
                except sqlite3.Error:
                    continue
                if not isinstance(v, str) or len(v) < 19:
                    continue
                try:
                    t = _dt.datetime.fromisoformat(v[:19])
                except ValueError:
                    continue
                latest = t if latest is None or t > latest else latest
        finally:
            con.close()
    if latest is None:
        raise SystemExit("у копіях немає жодної позначки часу — задайте --now")
    return latest.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(hours=1)


def _parse_now(value: str) -> _dt.datetime:
    """--now у UTC; зона, якщо вказана, переводиться в UTC без зони (як у базі)."""
    t = _dt.datetime.fromisoformat(value)
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t


def _pick_side_file(explicit: str | None, name: str, db: Path) -> Path | None:
    for cand in (explicit, db.parent / name, SCRIPT_ROOT / "data" / name):
        if cand and Path(cand).is_file():
            return Path(cand)
    if explicit:
        raise SystemExit(f"{explicit}: файлу немає")
    return None


# Що з дерева коду впливає на вивід сайту: сам застосунок (з шаблонами) і cli.py.
APP_PATHS = ("realty/", "cli.py")


def _git(root: Path) -> dict:
    """HEAD і доказ, що код застосунку збігається з ним (або чим саме відрізняється).

    Лічильника «брудних» файлів мало: незакомічений тест і змінений шаблон
    виглядали б однаково. Тому окремо — перелік файлів застосунку, що
    відрізняються від HEAD, і sha256 їхньої різниці (для нових файлів — вмісту).
    """
    def git(*a) -> str:
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True,
                              text=True, timeout=60, check=True).stdout

    try:
        head = git("rev-parse", "HEAD").strip()
        dirty = [x for x in git("status", "--porcelain").splitlines() if x.strip()]
        app_status = [x for x in git("status", "--porcelain", "--untracked-files=all",
                                     "--", *APP_PATHS).splitlines() if x.strip()]
        h = hashlib.sha256(git("diff", "HEAD", "--", *APP_PATHS).encode("utf-8"))
        for line in sorted(app_status):
            if line.startswith("??"):
                rel = line[3:].strip()
                h.update(f"\n?? {rel}\n".encode("utf-8"))
                h.update(_sha256(root / rel).encode("ascii"))
        return {"root": str(root), "git_head": head or None, "dirty_files": len(dirty),
                "app_dirty": sorted(app_status), "app_diff_sha256": h.hexdigest()}
    except (OSError, subprocess.SubprocessError):
        return {"root": str(root), "git_head": None, "dirty_files": None,
                "app_dirty": None, "app_diff_sha256": None}


# --- Фікстура й набір адрес ---------------------------------------------------------------


def _choose_in_progress(db: Path, n: int, seed: int) -> list[int]:
    if n <= 0:
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM properties ORDER BY id")]
    finally:
        con.close()
    return sorted(random.Random(seed + 1).sample(ids, min(n, len(ids))))


def _apply_in_progress(db: Path, property_ids: list[int], now_utc: _dt.datetime) -> int:
    if not property_ids:
        return 0
    con = sqlite3.connect(db)
    try:
        marks = ",".join("?" * len(property_ids))
        cur = con.execute(
            f"UPDATE listings SET in_progress = 1, in_progress_at = ? "
            f"WHERE property_id IN ({marks})",
            [now_utc.strftime("%Y-%m-%d %H:%M:%S.%f"), *property_ids])
        con.commit()
        return cur.rowcount
    finally:
        con.close()


SORTS = ["price_desc", "price_asc", "rooms_desc", "rooms_asc",
         "sqm_desc", "sqm_asc", "date_desc", "date_asc"]
ROOMS = ["1", "2", "3", "4+"]
CONDITIONS = ["renovated", "needs_repair", "unknown"]
MARKETS = ["primary", "secondary", "unknown"]


def _u(path: str, params: dict | None = None) -> str:
    return f"{path}?{urlencode(params)}" if params else path


def build_urls(db: Path, *, seed: int, properties: int | None) -> list[tuple[str, str]]:
    """(адреса, група). Набір фіксований і залежить лише від копії та seed."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sources = [r[0] for r in con.execute(
            "SELECT DISTINCT source FROM listings WHERE source IS NOT NULL ORDER BY source")]
        prop_ids = [r[0] for r in con.execute("SELECT id FROM properties ORDER BY id")]
        biggest = [r[0] for r in con.execute(
            "SELECT property_id FROM listings WHERE property_id IS NOT NULL "
            "GROUP BY property_id ORDER BY count(*) DESC, property_id LIMIT 10")]
        redirects = [r[0] for r in con.execute(
            "SELECT old_id FROM property_redirects ORDER BY old_id")]
        # Район і ЖК (Блок 4, E10, D57): найчастіші значення row_* на копії, якщо колонки є.
        cols = {r[1] for r in con.execute('PRAGMA table_info("listings")')}
        top_districts, top_complexes = [], []
        if "row_district" in cols:
            top_districts = [r[0] for r in con.execute(
                "SELECT row_district FROM listings WHERE row_district IS NOT NULL "
                "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 6")]
            top_complexes = [tuple(r) for r in con.execute(
                "SELECT row_district, row_complex FROM listings WHERE row_complex IS NOT NULL "
                "AND row_complex != '_none' AND row_district IS NOT NULL "
                "GROUP BY 1, 2 ORDER BY count(*) DESC, 1, 2 LIMIT 4")]
    finally:
        con.close()

    out: list[tuple[str, str]] = []

    def add(group, path, params=None):
        out.append((_u(path, params), group))

    # --- Список «/»: кожне значення кожного фільтра, пари, що взаємодіють, краї.
    singles = ([{"rooms": r} for r in ROOMS] + [{"condition": c} for c in CONDITIONS]
               + [{"market": m} for m in MARKETS] + [{"source": s} for s in sources]
               + [{"sort": s} for s in SORTS + ["bogus"]] + [{"all_ads": "1"}]
               + [{"per_page": p} for p in ("100", "200", "7")]
               + [{"price_min": "50000"}, {"price_max": "80000"},
                  {"price_min": "50000", "price_max": "80000"},
                  {"price_min": "90000", "price_max": "60000"},       # «від» > «до»
                  {"price_min": "", "price_max": ""}, {"price_min": "abc"}])
    pairs = ([{"rooms": r, "condition": c} for r in ROOMS for c in CONDITIONS]
             + [{"condition": c, "market": m} for c in CONDITIONS for m in MARKETS]
             + [{"rooms": r, "market": m} for r in ROOMS for m in MARKETS]
             + [{"sort": s, "all_ads": "1"} for s in SORTS]
             + [{"source": s, "sort": o} for s in sources for o in ("price_asc", "date_desc")]
             + [{"rooms": r, "per_page": "100"} for r in ROOMS])
    combos = [{"rooms": "2", "condition": "renovated", "market": "secondary",
               "price_min": "40000", "price_max": "120000", "sort": "sqm_asc"},
              {"source": "olx" if "olx" in sources else sources[0], "rooms": "1",
               "all_ads": "1", "per_page": "100"},
              {"condition": "needs_repair", "market": "primary", "sort": "date_asc",
               "per_page": "200"}]
    edges = [{"page": "1"}, {"page": "0"}, {"page": "-1"}, {"page": "abc"}, {"page": "9999"},
             {"per_page": "200", "page": "9999"},
             {"rooms": "4+", "condition": "unknown", "market": "unknown", "page": "9999"}]
    for page in (None, "2", "3"):
        add("list", "/", {"page": page} if page else None)
    for params in singles:
        for page in (None, "2", "3"):
            add("list", "/", {**params, **({"page": page} if page else {})})
    for params in pairs:
        for page in (None, "2"):
            add("list", "/", {**params, **({"page": page} if page else {})})
    for params in combos:
        for page in (None, "2", "3"):
            add("list", "/", {**params, **({"page": page} if page else {})})
    for params in edges:
        add("list", "/", params)

    # --- Район, ЖК, місцевість і «Райони й ЖК» (Блок 4, E10, D57).
    place_params = ([{"district": d} for d in top_districts]
                    + [{"district": d, "complex": c} for d, c in top_complexes]
                    + [{"district": "_unknown"}, {"complex": "_none"}, {"area": "city"},
                       {"district": "_unknown", "area": "city"},
                       {"district": "nemaie", "complex": "nemaie"}])
    if top_districts:
        for params in place_params:
            for page in (None, "2"):
                add("list", "/", {**params, **({"page": page} if page else {})})
        add("list", "/", {"district": top_districts[0], "rooms": "2", "all_ads": "1"})
        add("processing", "/processing", {"district": top_districts[0]})
        for params in ({}, {"area": "city"}, {"rooms": "2"}, {"all_ads": "1"},
                       {"district": top_districts[0]}):
            add("places", "/places", params or None)

    # --- «В обробці»: та сама збірка списку з умовою in_progress.
    for page in (None, "2", "3"):
        add("processing", "/processing", {"page": page} if page else None)
    for params in ([{"rooms": r} for r in ROOMS] + [{"condition": c} for c in CONDITIONS]
                   + [{"market": m} for m in MARKETS] + [{"sort": s} for s in SORTS]
                   + [{"all_ads": "1"}, {"per_page": "100"}, {"source": sources[0]}]
                   + combos[:1]):
        add("processing", "/processing", params)

    # --- «Аналітика» й її API: усі 80 комбінацій фільтрів.
    grid = [{k: v for k, v in (("rooms", r), ("condition", c), ("market", m)) if v}
            for r in ("", "1", "2", "3", "4") for c in ("", *CONDITIONS)
            for m in ("", *MARKETS)]
    for params in grid:
        add("analytics", "/analytics", params or None)
    for params in grid:
        add("api-analytics", "/api/analytics/segments", params or None)

    # --- Сторінки квартир: вибірка за seed (у порядку id) + найбільші + переадресації
    # + неіснуюча. Лише verify=0 — інакше сторінка ходила б на сайт джерела.
    n = len(prop_ids) if properties is None else min(properties, len(prop_ids))
    sample = sorted(random.Random(seed).sample(prop_ids, n))
    redirect_sample = sorted(random.Random(seed + 2).sample(redirects, min(5, len(redirects))))
    missing = max([*prop_ids, *redirects, 0]) + 1000
    extra = [p for p in biggest if p not in set(sample)]
    for pid in sample + extra + redirect_sample + [missing]:
        add("property", f"/property/{pid}", {"verify": "0"})
    api_ids = sample[:50] + extra + redirect_sample + [missing]
    for pid in api_ids:
        add("api-property", f"/api/analytics/property/{pid}")
    for pid in api_ids:
        add("api-prices", f"/api/properties/{pid}/prices")

    # --- JSON API списків і зведення.
    for params in ({}, {"min_sources": "2"}, {"min_sources": "3", "limit": "1000"},
                   {"rooms": "1"}, {"rooms": "4+"}, {"condition": "renovated", "market": "primary"},
                   {"market": "secondary", "limit": "50"}):
        add("api-properties", "/api/properties", params or None)
    for params in ([{}, {"limit": "1000"}] + [{"sort": s} for s in SORTS]
                   + [{"source": s} for s in sources]
                   + [{"rooms": "2", "condition": "renovated"},
                      {"price_min": "50000", "price_max": "80000"},
                      {"in_progress": "true"}, {"in_progress": "false", "limit": "1000"},
                      {"market": "unknown", "rooms": "4+"}]):
        add("api-listings", "/api/listings", params or None)
    add("api-stats", "/api/stats")

    # --- Стан системи.
    add("status", "/status")
    if top_districts:
        add("api-listings", "/api/listings", {"district": top_districts[0], "limit": "1000"})
    for path, params in (("/api/status", None), ("/api/status/reports", None),
                         ("/api/status/reports", {"limit": "500"}), ("/api/status/dedup", None),
                         ("/api/status/runs", None), ("/api/status/runs", {"limit": "100"})):
        add("api-status", path, params)
    return out


# --- Середовище рендеру -------------------------------------------------------------------


class _FrozenMeta(type):
    """isinstance(справжня_дата, FrozenDateTime) лишається True — інакше код на
    кшталт `isinstance(updated, datetime)` (analytics.cache) поводився б інакше."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, cls.__real__)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, cls.__real__)


def _frozen_classes(now_utc: _dt.datetime):
    aware = now_utc.replace(tzinfo=_dt.timezone.utc)
    local = aware.astimezone().replace(tzinfo=None)
    real_dt, real_date = _dt.datetime, _dt.date

    def real(name, cls):
        meth = getattr(cls, name)
        return staticmethod(lambda *a, **k: meth(*a, **k))

    class FrozenDateTime(real_dt, metaclass=_FrozenMeta):
        __real__ = real_dt

        def __new__(cls, *a, **k):                 # конструктор дає справжню дату
            return real_dt(*a, **k)

        @classmethod
        def now(cls, tz=None):
            return local if tz is None else aware.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return now_utc

        @classmethod
        def today(cls):
            return local

        fromtimestamp = real("fromtimestamp", real_dt)
        fromisoformat = real("fromisoformat", real_dt)
        strptime = real("strptime", real_dt)
        combine = real("combine", real_dt)
        fromordinal = real("fromordinal", real_dt)

    class FrozenDate(real_date, metaclass=_FrozenMeta):
        __real__ = real_date

        def __new__(cls, *a, **k):
            return real_date(*a, **k)

        @classmethod
        def today(cls):
            return local.date()

        fromtimestamp = real("fromtimestamp", real_date)
        fromisoformat = real("fromisoformat", real_date)
        fromordinal = real("fromordinal", real_date)

    shim = types.ModuleType("datetime")
    shim.__dict__.update({k: v for k, v in vars(_dt).items() if not k.startswith("__")})
    shim.datetime, shim.date = FrozenDateTime, FrozenDate
    return FrozenDateTime, FrozenDate, shim


def _freeze_clock(now_utc: _dt.datetime) -> list[str]:
    """Підміняє datetime/date в усіх модулях realty.* на заморожені."""
    fdt, fdate, shim = _frozen_classes(now_utc)
    patched = []
    for name, mod in sorted(sys.modules.items()):
        if mod is None or not (name == "realty" or name.startswith("realty.")):
            continue
        for attr, val in list(vars(mod).items()):
            if val is _dt.datetime:
                setattr(mod, attr, fdt)
            elif val is _dt.date:
                setattr(mod, attr, fdate)
            elif val is _dt:
                setattr(mod, attr, shim)
            else:
                continue
            patched.append(f"{name}.{attr}")
    return patched


def _load_netguard():
    """Заборона мережі з ЦЬОГО репозиторію — навіть коли рендериться старий код
    (--code), у якому realty/netguard.py ще немає."""
    spec = importlib.util.spec_from_file_location(
        "_page_equality_netguard", SCRIPT_ROOT / "realty" / "netguard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _refuse_processes(*a, **k):
    raise RuntimeError("page_equality: рендер не запускає дочірніх процесів")


def _prepare(args, work: Path, now_utc: _dt.datetime) -> dict:
    """Оточення, імпорт коду й заморожування. Повертає службові відомості."""
    os.environ.update({
        "DB_URL": f"sqlite:///{work / 'realty.db'}",
        "OPS_DB_URL": f"sqlite:///{work / 'ops.db'}",
        "AUTH_USER": "", "AUTH_PASSWORD": "", "FRIEND_USER": "", "FRIEND_PASSWORD": "",
        "LLM_FALLBACK": "0",
    })
    cleared = [k for k in CLEARED_ENV if os.environ.pop(k, None) is not None]

    import dotenv                                  # .env машини не має впливати на вивід
    dotenv.load_dotenv = lambda *a, **k: False

    netguard = _load_netguard()
    netguard.install()

    code = Path(args.code).resolve()
    sys.path.insert(0, str(code))
    config = importlib.import_module("realty.config")
    if Path(config.__file__).resolve().parent.parent != code:
        raise SystemExit(f"realty імпортовано не з {code}: {config.__file__}")
    data = work / "data"
    config.DATA_DIR = data                         # усе, що похідне від data/, — приватне
    config.CACHE_DIR = data / "cache"

    # Імпортуємо всі модулі заздалегідь: модуль, вперше імпортований посеред
    # запиту, лишився б із незамороженим годинником.
    realty = importlib.import_module("realty")
    failed = []
    for info in pkgutil.walk_packages(realty.__path__, "realty.",
                                      onerror=lambda name: failed.append(name)):
        try:
            importlib.import_module(info.name)
        except Exception as e:                     # noqa: BLE001
            failed.append(f"{info.name}: {type(e).__name__}: {e}")
    importlib.import_module("realty.web.app")
    configs = _config_hashes()
    patched = _freeze_clock(now_utc)

    ops = importlib.import_module("realty.ops")
    if hasattr(ops, "_process_alive"):
        ops._process_alive = lambda pid: False     # процеси з копії тут не живуть
    subprocess.Popen = _refuse_processes          # type: ignore[misc]
    # "_ng" — службове, у manifest не йде: після рендеру з нього беремо спроби мережі.
    return {"cleared_env": cleared, "import_failed": failed, "clock_patched": len(patched),
            "_ng": netguard, "_configs": configs}


def _config_hashes() -> dict | None:
    """Хеші конфігів config/ того дерева коду, що рендерить (у старому дереві
    модуля конфігів ще немає — тоді None). REALTY_CONFIG_DIR тут уже прибрано."""
    try:
        cf = importlib.import_module("realty.configfiles")
    except ImportError:
        return None
    out = {}
    for name in sorted(getattr(cf, "SCHEMAS", {})):
        try:
            out[name] = cf.config_hash(name)
        except Exception as e:                     # noqa: BLE001 — записати, не впасти
            out[name] = f"ПОМИЛКА: {type(e).__name__}: {e}"
    return out


def _network_attempts(env_info: dict) -> list[str]:
    """Спроби вийти в мережу під час рендеру — навіть проковтнуті кодом
    (`except Exception` із запасним значенням: тоді в еталоні тихо опинилось би
    запасне значення замість справжнього)."""
    attempts = env_info["_ng"].attempts()
    if attempts:
        print("ПОМИЛКА: рендер намагався вийти в мережу — результат не зараховано:",
              *attempts[:20], sep="\n  ")
    return attempts


def _render(urls: list[str], on_each=None) -> list[dict]:
    from fastapi.testclient import TestClient

    from realty import ops
    from realty.web.app import app

    results = []
    with TestClient(app, raise_server_exceptions=False, follow_redirects=False) as client:
        if hasattr(ops, "reap_stale_runs"):
            ops.reap_stale_runs()                  # один раз до рендеру — порядок не важить
        for i, url in enumerate(urls):
            t0 = time.perf_counter()
            r = client.get(url)
            ms = (time.perf_counter() - t0) * 1000
            ctype = r.headers.get("content-type", "")
            body = normalize(r.content, ctype)
            results.append({"url": url, "status": r.status_code, "content_type": ctype,
                            "location": r.headers.get("location"),
                            "sha256": hashlib.sha256(body).hexdigest(),
                            "bytes": len(body), "ms": round(ms, 1), "_body": body})
            if on_each:
                on_each(i, len(urls))
    return results


def _progress(i: int, n: int) -> None:
    if (i + 1) % 100 == 0 or i + 1 == n:
        print(f"  … {i + 1}/{n}", flush=True)


def _ext(ctype: str) -> str:
    kind = ctype.split(";")[0].strip().lower()
    return {"application/json": "json", "text/html": "html"}.get(kind, "txt")


def _slug(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_")[:90] or "root"


# --- Команди -------------------------------------------------------------------------------


def _setup_work(args) -> Path:
    if args.work:
        Path(args.work).mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="page-eq-", dir=args.work or None))


def _copy_inputs(args, work: Path) -> dict:
    db = Path(args.db).resolve()
    if not db.is_file():
        raise SystemExit(f"{db}: бази немає")
    inputs = {"db": _private_copy(db, work / "realty.db")}
    ops_src = Path(args.ops).resolve() if args.ops else next(
        (p for p in (db.parent / "ops_copy.db", db.parent / "ops.db") if p.is_file()), None)
    if ops_src is not None:
        inputs["ops"] = _private_copy(ops_src, work / "ops.db")
    else:
        inputs["ops"] = None                       # порожня ops.db — таблиці створить сайт
    (work / "data").mkdir()
    return inputs


def cmd_snapshot(args) -> int:
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out}: тека не порожня — еталон не перезаписую")
    work = _setup_work(args)
    started = time.perf_counter()
    try:
        inputs = _copy_inputs(args, work)
        (out / "pages").mkdir(parents=True)
        (out / "inputs").mkdir()
        db = Path(args.db).resolve()
        for key, name in (("quality_thresholds", "quality_thresholds.json"),
                          ("analytics_settings", "analytics_settings.json")):
            src = _pick_side_file(getattr(args, key), name, db)
            if src is None:
                inputs[key] = None
                continue
            shutil.copyfile(src, out / "inputs" / name)
            shutil.copyfile(src, work / "data" / name)
            inputs[key] = {"from": str(src), "sha256": _sha256(src), "stored": f"inputs/{name}"}

        now_utc = (_parse_now(args.now) if args.now
                   else _data_now(work / "realty.db", work / "ops.db"))
        fixture_ids = _choose_in_progress(work / "realty.db", args.in_progress, args.seed)
        marked = _apply_in_progress(work / "realty.db", fixture_ids, now_utc)
        pairs = build_urls(work / "realty.db", seed=args.seed,
                           properties=None if args.properties == "all" else int(args.properties))
        if args.limit_per_group:
            # Швидкий режим для тесту самого інструмента: перші N адрес кожної групи.
            taken: dict[str, int] = {}
            kept = []
            for url, group in pairs:
                taken[group] = taken.get(group, 0) + 1
                if taken[group] <= args.limit_per_group:
                    kept.append((url, group))
            pairs = kept
        code_info = _git(Path(args.code).resolve())
        if code_info["app_dirty"]:
            print("УВАГА: код застосунку відрізняється від HEAD — еталон описує саме цей "
                  "стан (у manifest: code.app_dirty і code.app_diff_sha256):",
                  *code_info["app_dirty"], sep="\n  ")
        env_info = _prepare(args, work, now_utc)
        inputs["config"] = env_info.pop("_configs")
        print(f"еталон: {len(pairs)} адрес, «зараз» = {now_utc.isoformat()} UTC", flush=True)
        results = _render([u for u, _ in pairs], _progress)
        attempts = _network_attempts(env_info)
        if attempts:
            return 2

        entries, gz_total = [], 0
        for i, ((url, group), res) in enumerate(zip(pairs, results)):
            name = f"pages/{i:04d}-{_slug(url)}.{_ext(res['content_type'])}.gz"
            blob = gzip.compress(res.pop("_body"), compresslevel=6, mtime=0)
            (out / name).write_bytes(blob)
            gz_total += len(blob)
            entries.append({**res, "group": group, "file": name})
        took = time.perf_counter() - started
        by_status: dict[str, int] = {}
        by_group: dict[str, int] = {}
        for e in entries:
            by_status[str(e["status"])] = by_status.get(str(e["status"]), 0) + 1
            by_group[e["group"]] = by_group.get(e["group"], 0) + 1
        manifest = {
            "format": FORMAT, "tool": "scripts/page_equality.py",
            "created_wall_clock": _dt.datetime.now().isoformat(timespec="seconds"),
            "code": code_info, "frozen_now_utc": now_utc.isoformat(), "timezone": TIMEZONE,
            "inputs": inputs,
            "environment": {"dotenv": "не читається",
                            **{k: v for k, v in env_info.items() if not k.startswith("_")},
                            "pythonhashseed": os.environ.get("PYTHONHASHSEED")},
            "network_attempts": attempts,
            "fixture": {"in_progress_properties": fixture_ids, "in_progress_listings": marked},
            "params": {"seed": args.seed, "properties": args.properties,
                       "in_progress": args.in_progress,
                       "limit_per_group": args.limit_per_group},
            "normalization": NORMALIZATION,
            "totals": {"urls": len(entries), "bytes": sum(e["bytes"] for e in entries),
                       "gz_bytes": gz_total, "seconds": round(took, 1),
                       "render_ms": round(sum(e["ms"] for e in entries)),
                       "by_status": by_status, "by_group": by_group},
            "urls": entries,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
        t = manifest["totals"]
        print(f"готово: {t['urls']} адрес, {t['bytes'] / 1e6:.1f} МБ нормалізованого вмісту "
              f"({t['gz_bytes'] / 1e6:.1f} МБ у gzip), {t['seconds']} с; коди: {by_status}")
        if env_info["import_failed"]:
            print("  модулі, що не імпортувались:", *env_info["import_failed"], sep="\n    ")
        return 0
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)


def cmd_compare(args) -> int:
    golden = Path(args.golden).resolve()
    manifest = json.loads((golden / "manifest.json").read_text())
    if manifest.get("format") != FORMAT:
        raise SystemExit(f"{golden}: формат еталону {manifest.get('format')} ≠ {FORMAT}")
    work = _setup_work(args)
    started = time.perf_counter()
    try:
        inputs = _copy_inputs(args, work)
        for key in ("db", "ops"):
            want = (manifest["inputs"].get(key) or {}).get("sha256")
            got = (inputs.get(key) or {}).get("sha256")
            if want != got and not args.allow_other_db:
                print(f"ПОМИЛКА: {key} не та, з якої знято еталон "
                      f"(еталон {str(want)[:12]}, зараз {str(got)[:12]}). "
                      f"Порівняння на іншій копії нічого не доводить; --allow-other-db — свідомо.")
                return 2
        for key, name in (("quality_thresholds", "quality_thresholds.json"),
                          ("analytics_settings", "analytics_settings.json")):
            if manifest["inputs"].get(key):
                shutil.copyfile(golden / manifest["inputs"][key]["stored"], work / "data" / name)

        now_utc = _dt.datetime.fromisoformat(manifest["frozen_now_utc"])
        _apply_in_progress(work / "realty.db",
                           manifest["fixture"]["in_progress_properties"], now_utc)
        entries = manifest["urls"]
        order = list(range(len(entries)))
        if args.order == "reverse":
            order.reverse()
        code_now = _git(Path(args.code).resolve())
        code_was = manifest.get("code", {})
        print(f"код еталону: HEAD {str(code_was.get('git_head'))[:12]}, змінених файлів "
              f"застосунку {len(code_was.get('app_dirty') or [])}; зараз: HEAD "
              f"{str(code_now['git_head'])[:12]}, змінених {len(code_now['app_dirty'] or [])}")
        env_info = _prepare(args, work, now_utc)
        configs_now, configs_was = env_info.pop("_configs"), manifest["inputs"].get("config")
        if configs_was is not None and configs_now != configs_was:
            for name in sorted(set(configs_now or {}) | set(configs_was)):
                was, now = configs_was.get(name), (configs_now or {}).get(name)
                if was != now:
                    print(f"конфіг {name}: еталон {str(was)[:12]} → зараз {str(now)[:12]} "
                          f"(різниця може бути від порогів, а не від коду)")
        print(f"порівняння: {len(entries)} адрес (порядок: {args.order}), "
              f"«зараз» = {now_utc.isoformat()} UTC", flush=True)
        rendered = _render([entries[i]["url"] for i in order], _progress)
        if _network_attempts(env_info):
            return 2
        now_by_index = {i: r for i, r in zip(order, rendered)}

        diffs = []
        for i, e in enumerate(entries):
            r = now_by_index[i]
            fields_ = [f for f in ("status", "content_type", "location") if e.get(f) != r[f]]
            # Еталон нормалізуємо ще раз поточними правилами (вони ідемпотентні):
            # правило, додане пізніше, діє з обох боків однаково.
            old = normalize(gzip.decompress((golden / e["file"]).read_bytes()),
                            e["content_type"])
            body_same = old == r["_body"]
            if fields_ or not body_same:
                diffs.append((e, r, fields_, old))
        took = time.perf_counter() - started
        print(f"різних: {len(diffs)} з {len(entries)}; {took:.0f} с")
        report = []
        for n, (e, r, fields_, old) in enumerate(diffs):
            head = f"=== {e['url']}  [{e['group']}]"
            for f in fields_:
                head += f"\n    {f}: еталон {e.get(f)!r} → зараз {r[f]!r}"
            lines = [head]
            if old != r["_body"]:
                a = old.decode("utf-8", errors="replace").splitlines()
                b = r["_body"].decode("utf-8", errors="replace").splitlines()
                delta = list(difflib.unified_diff(a, b, "еталон", "зараз", n=2, lineterm=""))
                lines += [ln if len(ln) <= 300 else ln[:300] + " …" for ln in delta[:args.lines]]
                if len(delta) > args.lines:
                    lines.append(f"    … ще {len(delta) - args.lines} рядків різниці")
            report.append("\n".join(lines))
            if n < args.show:
                print(report[-1])
        if len(diffs) > args.show:
            print(f"… і ще {len(diffs) - args.show} адрес із різницею"
                  + (f" (усі — у {args.report})" if args.report else ""))
        if args.report:
            Path(args.report).write_text("\n\n".join(report) + "\n", encoding="utf-8")
        return 1 if diffs else 0
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--db", required=True, help="копія realty.db (ніхто в неї не пише)")
        sp.add_argument("--ops", help="копія ops.db; без неї — ops_copy.db/ops.db поруч із --db")
        sp.add_argument("--code", default=str(SCRIPT_ROOT),
                        help="корінь коду, який рендерить (типово — цей репозиторій)")
        sp.add_argument("--work", help="де робити приватні копії (типово — системний temp)")
        sp.add_argument("--keep-work", action="store_true", help="не прибирати робочу теку")

    s = sub.add_parser("snapshot", help="зняти еталон")
    common(s)
    s.add_argument("--out", required=True, help="порожня тека для еталону")
    s.add_argument("--seed", type=int, default=DEFAULT_SEED)
    s.add_argument("--properties", default="300", help="скільки сторінок квартир, або all")
    s.add_argument("--in-progress", type=int, default=60,
                   help="скільки квартир позначити «в обробці» в копії (0 — нічого)")
    s.add_argument("--now", help="заморожений час UTC (ISO); типово — з даних")
    s.add_argument("--limit-per-group", type=int, default=0,
                   help="лише перші N адрес кожної групи (швидка перевірка інструмента)")
    s.add_argument("--quality-thresholds", dest="quality_thresholds",
                   help="пороги якості; типово — поруч із --db або data/ репозиторію")
    s.add_argument("--analytics-settings", dest="analytics_settings",
                   help="налаштування аналітики; типово — поруч із --db або data/")
    s.set_defaults(func=cmd_snapshot)

    c = sub.add_parser("compare", help="порівняти з еталоном")
    common(c)
    c.add_argument("--golden", required=True)
    c.add_argument("--show", type=int, default=10, help="скільки різниць показати повністю")
    c.add_argument("--lines", type=int, default=120, help="рядків різниці на адресу")
    c.add_argument("--report", help="файл для всіх різниць")
    c.add_argument("--order", choices=("golden", "reverse"), default="golden",
                   help="reverse — перевірка, що вивід не залежить від порядку адрес")
    c.add_argument("--allow-other-db", action="store_true")
    c.set_defaults(func=cmd_compare)

    args = p.parse_args()
    logging.basicConfig(level=logging.ERROR)
    return args.func(args)


def _pin_process() -> None:
    """Часовий пояс і PYTHONHASHSEED — до будь-якого імпорту коду системи.

    Порядок обходу множин залежить від seed хешування; щоб два прогони
    гарантовано рендерили однаково, процес перезапускається з PYTHONHASHSEED=0.
    """
    if os.environ.get("PYTHONHASHSEED") != "0":
        os.execve(sys.executable, [sys.executable, *sys.argv],
                  {**os.environ, "PYTHONHASHSEED": "0", "TZ": TIMEZONE})
    os.environ["TZ"] = TIMEZONE
    time.tzset()


if __name__ == "__main__":
    _pin_process()
    sys.exit(main())
