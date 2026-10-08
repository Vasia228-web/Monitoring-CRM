"""Перевірка, чи оголошення ще живе — тонка обгортка над realty/liveness (Блок 1, E8, D52).

Без цього база лише зростає: продана квартира лишається в ній назавжди як
активна, і будь-яка статистика рахується по суміші живих і знятих оголошень.

НЕДОТОРКАНЕ ПРАВИЛО (рішення власника 1, D46): з продажу знімаємо тільки за явним
сигналом — 410, явний банер/елемент сторінки (DOM.RIA: стан сторінки «archive» І
банер «Оголошення видалено…»), або 404, що повторився `repeat_404.count` разів з
інтервалом, І перевірка існування показала, що оголошення справді немає (інакше —
ремонт посилання). Один 404 — «не знайдено», не знято. Будь-яка інша відповідь —
403, таймаут, обрив мережі, капча, порожнеча — означає «не достукались», а не
«знято». Відсутність у стрічці чи переліку не знімає НІКОЛИ (`sweep_after_full_run`
прибрано).

Сигнал у кожного сайту свій (Етап 0, D45; config/liveness.toml):

    dom.ria.com  GET сторінки: 410, або 200 зі станом «archive» і банером m-sold;
                 HEAD тут сліпий — 18 з 18 знятих віддають 200
    olx.ua       HEAD: 410 (на GET CloudFront дає 403 і живому, і мертвому)
    rieltor.ua   HEAD: 410 (без www: www.rieltor.ua дає 404 живим)
    lun.ua, flombu.com  явного сигналу немає — лише повторний 404
    blagodeveloper.com  сигналу немає взагалі: і живе, і вигадане планування
                 ведуть на каталог, тому цей сайт не перевіряємо (позначка на сайті)

Одиниця перевірки — ключ «сайт:id» (listings.site_key): один запит на ключ, вердикт
— усім рядкам ключа (копіям LUN теж). Черги нарізані по ХОСТАХ, а не по джерелах:
LUN агрегує OLX і rieltor, і окремі черги «lun» і «olx» били б в один сайт удвічі
частіше за задумане.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from .db import session_scope
from .fetcher import Fetcher
from .liveness import engine, policy as pol, queue as lq
from .liveness.queue import (  # noqa: F401 — колишній публічний API модуля
    OLD_LISTING_DAYS, PRICE_DROP_WINDOW_DAYS, SNAPSHOT_COVERED, VIEW_WINDOW_DAYS, _now,
    _order,
)
from .liveness.signatures import classify_code, verdict_from_code
from .models import Listing

log = logging.getLogger(__name__)

BLOCKED_CODES = {401, 403, 429}


@dataclass(frozen=True)
class HostRule:
    """Правило сайту для сліпого обходу: пауза між запитами й розмір порції."""

    delay: float
    sweep_limit: int


def _cfg():
    """Чинний config/liveness.toml: перечитування за mtime не частіше ніж раз на 60 с.

    `is_checkable`/`host_key` кличуть на кожну адресу (зокрема фоновий потік сайту
    для перевірки при відкритті) — повний розбір TOML на кожен виклик був би зайвим.
    Прогін (`liveness.service.run`) бере конфіг одним читанням із хешем на старті.
    """
    return pol.current()


def _hosts() -> dict[str, HostRule]:
    cfg = pol.load()
    return {h: HostRule(delay=s.delay, sweep_limit=s.sweep_per_run)
            for h, s in pol.checkable_hosts(cfg).items()}


# Хости, що перевіряються, — з config/liveness.toml (знімок на імпорті модуля;
# кроки циклу читають конфіг на старті). blagodeveloper.com тут немає.
HOSTS: dict[str, HostRule] = _hosts()
MAX_CONSECUTIVE_BLOCKS = pol.load().run.max_consecutive_blocks


def host_key(url: str) -> str:
    """Хост політики для адреси (без www./m.; піддомени агенцій → домен сайту)."""
    cfg = _cfg()
    return pol.host_of_url(cfg, url) or pol._norm_netloc(url)


def is_checkable(url: str) -> bool:
    return pol.is_checkable(_cfg(), url)


def classify(code: int) -> bool | None:
    """`True` — живе, `False` — знято, `None` — не висновок (семантика HEAD за кодом).

    410 → False; 2xx/3xx → True; 404 → None (один 404 — не знято, D46); 0, 401,
    403, 429, 5xx → None. DOM.RIA у роботі класифікує сторінку, а не код.
    """
    return classify_code(code)


# --- Черга --------------------------------------------------------------------


@dataclass
class Candidate:
    listing_id: int
    url: str
    source: str
    host: str
    key: str = ""


@dataclass
class HostResult:
    host: str
    codes: dict[int, int] = field(default_factory=dict)   # listing_id -> код
    requests: int = 0
    blocked: int = 0
    stopped_early: bool = False


def _candidate(item: lq.WorkItem, prefer: set[int] | None = None) -> Candidate:
    pick = next((r for r in item.rows if prefer and r.id in prefer), item.rows[0])
    return Candidate(pick.id, item.url, pick.source, item.host, item.key)


def collect(session, limit_per_host: int | None = None,
            hosts: list[str] | None = None,
            ids: list[int] | None = None) -> dict[str, list[Candidate]]:
    """Кандидати сліпого обходу по чергах хостів (по одному на ключ «сайт:id»).

    `ids` — точковий список (як перевірка при відкритті): ключі цих рядків.
    Без `ids` — ярус sweep плану Блоку 1: лише ключі з актуальними рядками, яким
    настав строк, у порядку `_order()`, по `sweep_per_run` хоста (або `limit_per_host`).
    """
    cfg = _cfg()
    if ids is not None:
        items = lq.items_for_rows(session, cfg, ids, "explicit")
        prefer = set(ids)
        out: dict[str, list[Candidate]] = {}
        for item in items:
            if hosts and item.host not in hosts:
                continue
            out.setdefault(item.host, []).append(_candidate(item, prefer))
        return out
    plan = lq.plan_run(session, cfg, hosts=hosts, limit_per_host=limit_per_host,
                       tiers=(lq.TIER_SWEEP,))
    return {h: [_candidate(i) for i in items] for h, items in plan.by_host.items() if items}


def _run_host(queue: list[Candidate], fetcher) -> HostResult:
    """Обходить чергу одного сайту послідовно, з його власною паузою.

    Та сама смуга, що й у прогоні (liveness.engine.run_lane); у базу нічого не пише.
    """
    host = queue[0].host
    cfg = _cfg()
    with session_scope() as s:
        items = lq.items_for_rows(s, cfg, [c.listing_id for c in queue], "sweep")
    by_key = {i.key: i for i in items}
    ordered = []
    for c in queue:
        item = by_key.get(c.key) or lq.WorkItem(key=c.key or pol.row_key(c.listing_id),
                                                host=host, url=c.url, tier="sweep",
                                                rows=())
        ordered.append(lq.WorkItem(key=item.key, host=host, url=c.url, tier="sweep",
                                   rows=item.rows, streak404=item.streak404))
    from .liveness import existence

    ctx = existence.Context(cfg=cfg, now=_now(), snapshots={})
    outcomes, st = engine.run_lane(host, ordered, fetcher=fetcher, cfg=cfg, ctx=ctx)
    result = HostResult(host=host, requests=st.requests, blocked=st.blocked,
                        stopped_early=st.stopped_early)
    for c, oc in zip(queue, outcomes):
        if oc.verdict is not None:
            result.codes[c.listing_id] = oc.verdict.code
    return result


def verify_batch(limit: int | None = None, sources: list[str] | None = None,
                 http: Fetcher | None = None, browser=None,
                 ids: list[int] | None = None, reason: str = "sweep",
                 kind: str | None = None, started: float | None = None) -> dict:
    """Один прогін перевірки. `limit` — на кожен сайт, не на всіх разом.

    Без `ids` — крок циклу: яруси черги (контрольні, підказані, вибірка знятих,
    сліпий обхід) у межах порцій config/liveness.toml, запобіжник, застосування
    пакетами (liveness.service.run). З `ids` — точкова перевірка ключів цих
    оголошень (перевірка при відкритті, reason="opened").

    `kind` — явний вид прогону (`cli.py verify`, запущений не диригентом циклу, —
    «manual»: не закриває відкладених завдань і не переписує зведення /status);
    `started` — time.monotonic() старту процесу кроку (стеля часу — від нього).

    `browser` лишився в сигнатурі для сумісності й не використовується: HEAD дає ті
    самі відповіді, що й Chromium (OLX 222 з 222), а DOM.RIA читається GET.
    """
    from .liveness import service

    hosts = None
    if sources:
        cfg = _cfg()
        with session_scope() as s:
            urls = s.scalars(select(Listing.original_url)
                             .where(Listing.source.in_(sources))).all()
        hosts = sorted({h for u in urls if (h := pol.host_of_url(cfg, u))
                        and cfg.hosts[h].checkable})
        if not hosts:
            return {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
                    "requests": 0, "blocked": 0, "blocked_sources": [], "by_source": {},
                    "by_host": {}}
    kind = kind or ("cycle" if ids is None and limit is None and not sources else (
        "opened" if reason == "opened" else "manual" if ids is None else "explicit"))
    own = http is None
    fetcher = http or Fetcher(delay=1.0, use_cache=False, label="verify")
    try:
        return service.run(kind=kind, ids=ids, reason=reason, hosts=hosts,
                           limit_per_host=limit, fetcher=fetcher, scope=session_scope,
                           started=started)
    finally:
        if own:
            fetcher.close()


def _apply(codes: dict[int, int], stats: dict, reason: str = "sweep") -> None:
    """Застосувати ГОЛІ коди (семантика HEAD) до оголошень — тим самим шляхом запису.

    Для ручних перевірок і тестів: код → вердикт (410 — знято, 404 — не знайдено,
    2xx/3xx — живе, решта — не визначено) → запобіжник → пакети → події. Вердикт
    іде всім рядкам ключа «сайт:id». DOM.RIA у роботі так НЕ класифікується (там
    сторінка й банер) — лише через verify_batch.
    """
    from .liveness import apply as la

    cfg = _cfg()
    with session_scope() as s:
        rows = {r.id: r for r in lq.load_rows(s, where=Listing.id.in_(list(codes)))}
        items = lq.items_for_rows(s, cfg, list(codes), reason)
    by_key = {i.key: i for i in items}
    now = _now()
    outcomes, seen = [], set()
    for lid, code in codes.items():
        row = rows.get(lid)
        if row is None:
            continue
        key = lq.key_of(row)
        item = by_key.get(key)
        if item is None:
            # Хост не перевіряється (Благо) — лише запис спроби, як і раніше не було.
            continue
        if key in seen:
            continue
        seen.add(key)
        outcomes.append(engine.Outcome(item, verdict_from_code(code), now))
    rep = la.apply_outcomes(outcomes, cfg=cfg, scope=session_scope)
    for k in ("checked", "alive", "delisted", "restored", "unknown"):
        stats[k] = stats.get(k, 0) + getattr(rep, k)
    for source, b in rep.by_source.items():
        bucket = stats.setdefault("by_source", {}).setdefault(
            source, {"checked": 0, "alive": 0, "delisted": 0, "unknown": 0})
        for k in ("checked", "alive", "delisted", "unknown"):
            bucket[k] = bucket.get(k, 0) + b.get(k, 0)
