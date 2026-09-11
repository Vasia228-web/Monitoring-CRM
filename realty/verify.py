"""Перевірка, чи оголошення ще живе.

Без цього база лише зростає: продана квартира лишається в ній назавжди як
активна, і будь-яка статистика рахується по суміші живих і знятих оголошень.

НЕДОТОРКАНЕ ПРАВИЛО: з продажу знімаємо тільки за явним 404/410. Будь-яка
інша відповідь — 403, таймаут, обрив мережі, капча, порожнеча — означає «не
достукались», а не «знято». Одного разу це вже врятувало базу, коли ноутбук
втратив мережу посеред прогону.

Сигнал у кожного сайту свій — заміряно на живих і завідомо мертвих посиланнях
(`probes/p_liveness.py`):

    dom.ria.com  404/410 проти 200
    rieltor.ua   410 проти 200
    flombu.com   404 проти 200
    olx.ua       404/410 проти 200 — але ТІЛЬКИ методом HEAD: на GET
                 CloudFront віддає 403 і живому, і мертвому
    blagodeveloper.com  сигналу немає взагалі: і живе, і вигадане планування
                 дають 200, тому цей сайт не перевіряємо

Черги нарізані по ХОСТАХ, а не по джерелах. LUN агрегує OLX, тож 2105 його
посилань ведуть на olx.ua; окремі черги «lun» і «olx» били б в один сайт
удвічі частіше за задумане. Обмеження темпу діє на рівні сайту, тому й черга
має бути на рівні сайту.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import case, select

from .db import session_scope
from .fetcher import Fetcher
from .models import Listing

log = logging.getLogger(__name__)

GONE_CODES = {404, 410}
BLOCKED_CODES = {401, 403, 429}
# Скільки відмов поспіль від одного сайту терпіти, перш ніж лишити його в
# спокої до наступного прогону. Рахується по хосту: блокування rieltor.ua не
# має зупиняти перевірку dom.ria.com.
MAX_CONSECUTIVE_BLOCKS = 5


@dataclass(frozen=True)
class HostRule:
    """Правило для одного сайту: пауза між запитами до нього."""

    delay: float


# Паузи підібрані під кожен сайт окремо. Сумарне навантаження на КОЖЕН сайт
# від паралельної роботи не зростає — черги незалежні, бо й ліміти незалежні.
HOSTS: dict[str, HostRule] = {
    "dom.ria.com": HostRule(delay=1.0),
    # 3.0, а не менше: заміряно, що при 1.8 с сайт починає віддавати 403 після
    # п'яти запитів поспіль, а при 3.0 с — нуль відмов на тих самих посиланнях
    # (`probes/p_rieltor_403.py`). Стара спільна конфігурація мала 1.5 с, тобто
    # ще агресивніше; це не було видно лише тому, що черга сюди не доходила.
    # Власна пауза на сайт — саме те, заради чого черги нарізані по хостах:
    # вона нікого, крім rieltor.ua, не сповільнює.
    "rieltor.ua": HostRule(delay=3.0),
    "olx.ua": HostRule(delay=2.0),
    "flombu.com": HostRule(delay=1.2),
}


def host_key(url: str) -> str:
    """Канонічний хост: `www.` відкидаємо, бо ліміт у сайту спільний."""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_checkable(url: str) -> bool:
    return host_key(url) in HOSTS


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def classify(code: int) -> bool | None:
    """`True` — живе, `False` — знято, `None` — не зрозуміло, не чіпаємо."""
    if code in GONE_CODES:
        return False
    if 200 <= code < 400:
        return True
    return None          # 0, 403, 429, 5xx — збій або блокування, не висновок


# --- Черга --------------------------------------------------------------------

@dataclass
class Candidate:
    listing_id: int
    url: str
    source: str
    host: str


@dataclass
class HostResult:
    host: str
    codes: dict[int, int] = field(default_factory=dict)   # listing_id -> код
    requests: int = 0
    blocked: int = 0
    stopped_early: bool = False


def _order():
    """Порядок обходу черги.

    Спершу ті, кого не пробували жодного разу; далі — за кількістю поспіль
    незрозумілих відповідей (безнадійні відходять у кінець), і вже потім за
    давністю спроби. Ключове тут — сортувати за СПРОБОЮ, а не за перевіркою:
    інакше оголошення, яке стабільно віддає 403, вічно лишається першим у
    черзі й не пускає туди решту.
    """
    return (
        case((Listing.last_attempt.is_(None), 0), else_=1),
        Listing.check_failures.asc(),
        Listing.last_attempt.asc(),
    )


def collect(session, limit_per_host: int, hosts: list[str] | None = None,
            ids: list[int] | None = None) -> dict[str, list[Candidate]]:
    """Набирає кандидатів і розкладає їх по чергах хостів.

    `ids` задає точковий список — так працює підтвердження кандидатів,
    знайдених різницею снапшотів: черга тоді складається саме з них.
    """
    stmt = select(Listing.id, Listing.original_url, Listing.source)
    if ids is not None:
        stmt = stmt.where(Listing.id.in_(ids))
    else:
        stmt = stmt.where(Listing.is_active.is_(True)).order_by(*_order())

    queues: dict[str, list[Candidate]] = {}
    wanted = set(hosts) if hosts else set(HOSTS)
    for listing_id, url, source in session.execute(stmt):
        host = host_key(url)
        if host not in wanted or host not in HOSTS:
            continue
        queue = queues.setdefault(host, [])
        if ids is None and len(queue) >= limit_per_host:
            continue
        queue.append(Candidate(listing_id, url, source, host))
        if ids is None and all(len(q) >= limit_per_host for q in queues.values()) \
                and len(queues) == len(wanted):
            break
    return queues


def _run_host(queue: list[Candidate], fetcher: Fetcher) -> HostResult:
    """Обходить чергу одного сайту послідовно, з його власною паузою.

    У базу нічого не пише: повертає коди, а рішення приймає головний потік.
    Так уся робота з SQLite лишається однопотоковою, і паралельність не
    коштує нам жодного ризику пошкодити дані.
    """
    host = queue[0].host
    rule = HOSTS[host]
    result = HostResult(host=host)
    consecutive = 0
    for item in queue:
        code = fetcher.probe(item.url, delay=rule.delay)
        result.codes[item.listing_id] = code
        result.requests += 1
        if code in BLOCKED_CODES:
            result.blocked += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_BLOCKS:
                result.stopped_early = True
                log.warning("%s відмовляє (%d поспіль) — зупиняємо чергу цього сайту",
                            host, consecutive)
                break
        else:
            consecutive = 0
    return result


def verify_batch(limit: int = 200, sources: list[str] | None = None,
                 http: Fetcher | None = None, browser=None,
                 ids: list[int] | None = None) -> dict:
    """Перевіряє порцію оголошень. `limit` — на кожен сайт, не на всіх разом.

    `browser` лишився в сигнатурі для сумісності викликів і навмисно не
    використовується: заміряно, що HEAD дає ті самі відповіді, що й Chromium,
    тож тримати браузер заради статусу немає причин.
    """
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "requests": 0, "blocked": 0, "blocked_sources": [],
             "by_source": {}, "by_host": {}}

    hosts = None
    if sources:
        # Назви джерел, передані ззовні, перетворюємо на хости: у LUN їх два.
        with session_scope() as s:
            urls = s.scalars(select(Listing.original_url)
                             .where(Listing.source.in_(sources)).limit(4000)).all()
        hosts = sorted({host_key(u) for u in urls} & set(HOSTS))
        if not hosts:
            return stats

    own = http is None
    fetcher = http or Fetcher(delay=1.0, use_cache=False, label="verify")
    try:
        with session_scope() as s:
            queues = collect(s, limit_per_host=limit, hosts=hosts, ids=ids)
        if not queues:
            return stats

        # По одному потоку на сайт. Навантаження на кожен окремий сайт при
        # цьому не зростає — зростає лише сумарна пропускна здатність.
        with ThreadPoolExecutor(max_workers=len(queues)) as pool:
            results = list(pool.map(lambda q: _run_host(q, fetcher), queues.values()))

        codes: dict[int, int] = {}
        for r in results:
            codes.update(r.codes)
            stats["requests"] += r.requests
            stats["blocked"] += r.blocked
            stats["by_host"][r.host] = {"requests": r.requests, "blocked": r.blocked,
                                        "stopped_early": r.stopped_early}
        _apply(codes, stats)
    finally:
        if own:
            fetcher.close()
    return stats


def _apply(codes: dict[int, int], stats: dict) -> None:
    """Застосовує коди до бази — в одному потоці й одній транзакції."""
    now = _now()
    with session_scope() as s:
        rows = s.scalars(select(Listing).where(Listing.id.in_(codes))).all()
        for row in rows:
            code = codes[row.id]
            verdict = classify(code)
            bucket = stats["by_source"].setdefault(
                row.source, {"checked": 0, "alive": 0, "delisted": 0, "unknown": 0})
            stats["checked"] += 1
            bucket["checked"] += 1

            # Спроба фіксується завжди — саме вона рухає чергу далі.
            row.last_attempt = now

            if verdict is None:
                row.check_failures = (row.check_failures or 0) + 1
                stats["unknown"] += 1
                bucket["unknown"] += 1
                continue

            row.check_failures = 0
            # А ось «перевірено» — лише за зрозумілої відповіді: на цій даті
            # тримається інтервал для аналізу виживання.
            row.last_checked = now
            if verdict is False:
                if row.is_active:
                    row.is_active = False
                    row.delisted_at = now
                    stats["delisted"] += 1
                    bucket["delisted"] += 1
                    log.info("Знято з продажу (HTTP %d): %s", code, row.original_url[:90])
            else:
                if not row.is_active:
                    row.is_active = True
                    row.delisted_at = None
                    stats["restored"] += 1
                stats["alive"] += 1
                bucket["alive"] += 1


def sweep_after_full_run(source: str, seen_ids: set[str]) -> int:
    """Після повного обходу джерела все, чого не бачили, — зняте з продажу.

    Застосовне лише тоді, коли джерело віддало ВСЮ свою видачу за один
    прогін: якщо оголошення в ній не було, воно там більше не публікується.
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
            row.last_attempt = _now()
            marked += 1
    if marked:
        log.info("%s: після повного обходу знято з продажу %d оголошень", source, marked)
    return marked
