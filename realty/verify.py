"""Перевірка, чи оголошення ще живе.

Без цього база лише зростає: продана квартира лишається в ній назавжди як
активна, і будь-яка статистика рахується по суміші живих і знятих оголошень.

Сигнал у кожного сайту свій — перевірено на неіснуючих ідентифікаторах:

    DIM.RIA    404 на сторінці оголошення (JSON-API тут не годиться: він
               віддає 200 навіть на вигаданий id)
    rieltor    410 Gone
    flombu     404 після слідування редіректам
    OLX        404, потрібен браузер
    blago      сигналу немає: і живе, і вигадане планування дають 200,
               тому це джерело не перевіряємо взагалі
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select

from .config import SOURCES
from .db import session_scope
from .fetcher import BrowserFetcher, Fetcher
from .models import Listing

log = logging.getLogger(__name__)

# Джерела, для яких перевірка має сенс.
CHECKABLE = {"domria", "lun", "flombu", "olx"}
BROWSER_SOURCES = {"olx"}
GONE_CODES = {404, 410}
BLOCKED_CODES = {401, 403, 429}
# Скільки поспіль відмов від джерела терпіти, перш ніж припинити його чіпати.
# rieltor.ua (через нього перевіряється LUN) починає віддавати 403 після
# кількох десятків швидких запитів — довбати його далі безглуздо й шкідливо.
MAX_CONSECUTIVE_BLOCKS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def classify(code: int) -> bool | None:
    """`True` — живе, `False` — знято, `None` — не зрозуміло, не чіпаємо."""
    if code in GONE_CODES:
        return False
    if 200 <= code < 400:
        return True
    return None          # 0, 403, 429, 5xx — збій або блокування, не висновок


def verify_batch(limit: int = 200, sources: list[str] | None = None,
                 http: Fetcher | None = None,
                 browser: BrowserFetcher | None = None) -> dict:
    """Перевіряє найдавніше перевірені оголошення.

    Знімає з продажу тільки за явним 404/410. Будь-яка інша відповідь —
    привід нічого не робити: блокування чи збій мережі не мають вимикати
    живі оголошення.
    """
    names = [n for n in (sources or CHECKABLE) if n in CHECKABLE]
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "skipped_blocked": 0, "blocked_sources": []}
    blocks: dict[str, int] = {}
    own_http = http is None
    own_browser = browser is None
    http = http or Fetcher(delay=1.0, use_cache=False, label="verify")

    try:
        with session_scope() as s:
            rows = s.scalars(
                select(Listing)
                .where(Listing.source.in_(names))
                .order_by(Listing.last_checked.is_(None).desc(),
                          Listing.last_checked.asc())
                .limit(limit)
            ).all()

            for row in rows:
                if blocks.get(row.source, 0) >= MAX_CONSECUTIVE_BLOCKS:
                    stats["skipped_blocked"] += 1
                    continue
                needs_browser = row.source in BROWSER_SOURCES
                if needs_browser:
                    browser = browser or BrowserFetcher(delay=2.5, label="verify")
                    code = browser.probe(row.original_url)
                else:
                    delay = SOURCES[row.source].delay if row.source in SOURCES else 1.0
                    code = http.probe(row.original_url, delay=delay)

                verdict = classify(code)
                stats["checked"] += 1
                if code in BLOCKED_CODES:
                    blocks[row.source] = blocks.get(row.source, 0) + 1
                    if blocks[row.source] == MAX_CONSECUTIVE_BLOCKS:
                        stats["blocked_sources"].append(row.source)
                        log.warning("%s відмовляє (%d поспіль) — припиняємо перевірку",
                                    row.source, code)
                else:
                    blocks[row.source] = 0

                # Позначку часу ставимо лише за зрозумілої відповіді: інакше
                # заблоковані оголошення пішли б у кінець черги неперевіреними.
                if verdict is None:
                    stats["unknown"] += 1
                    continue
                row.last_checked = _now()
                if verdict is False:
                    if row.is_active:
                        row.is_active = False
                        row.delisted_at = _now()
                        stats["delisted"] += 1
                        log.info("Знято з продажу: %s", row.original_url[:90])
                    stats["alive"] += 0
                else:
                    if not row.is_active:
                        row.is_active = True
                        row.delisted_at = None
                        stats["restored"] += 1
                    stats["alive"] += 1
    finally:
        if own_http:
            http.close()
        if own_browser and browser is not None:
            browser.close()
    return stats


def sweep_after_full_run(source: str, seen_ids: set[str]) -> int:
    """Після повного обходу джерела все, чого не бачили, — зняте з продажу.

    Найточніший спосіб: якщо джерело віддало всю свою видачу, а оголошення в
    ній не було, воно там більше не публікується.
    """
    marked = 0
    with session_scope() as s:
        rows = s.scalars(
            select(Listing).where(Listing.source == source, Listing.is_active.is_(True))
        ).all()
        for row in rows:
            if str(row.external_id) in seen_ids:
                continue
            row.is_active = False
            row.delisted_at = _now()
            row.last_checked = _now()
            marked += 1
    if marked:
        log.info("%s: після повного обходу знято з продажу %d оголошень", source, marked)
    return marked
