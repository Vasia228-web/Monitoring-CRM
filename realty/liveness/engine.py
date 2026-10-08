"""Мережева фаза перевірки: смуги хостів, класифікатор, правило повторного 404 (E8, D52).

Кожен хост — одна смуга (окремий потік) зі своєю паузою (`policy.pace`): навантаження
на кожен сайт не зростає від паралельності, зростає лише сумарна пропускна
здатність. Смуга лише питає й класифікує — у базу нічого не пише: результати
збираються в пам'яті, а застосовує їх головний потік пакетами ПІСЛЯ оцінки
запобіжника (apply.py; інтеграція, конфлікт 7).

Публічний API Блоку 1 (інтеграція, конфлікт 6): `check_keys(keys, reason, hooks)` —
вердикти для ключів без запису; `run(...)` — план → мережа → запобіжник →
застосування → запис прогону (ops.liveness_runs). `verify.verify_batch` — тонка
обгортка над `run`.

Гачки (`hooks`) — для доказів Блоків 3 і 4 з тіла тієї самої відповіді (один запит
на ключ): `hook(item, result, verdict) -> dict | None` у потоці смуги; повернений
словник {"place_raw": {...}, "seller_evidence": {...}, "seller_profile": "..."}
apply.py пише ЛИШЕ туди, де порожньо. Помилка гачка перевірки не зупиняє.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime

from ..fetcher import BLOCKING_CODES, ProbeResult
from . import existence, policy as pol, queue
from .signatures import NOT_FOUND, Verdict, classify

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    item: queue.WorkItem
    verdict: Verdict | None            # None — не дійшла черга (стеля часу, смугу зупинено)
    at: datetime
    capture: dict = field(default_factory=dict)


@dataclass
class LaneStats:
    host: str
    requests: int = 0
    blocked: int = 0
    stopped_early: bool = False
    skipped: int = 0
    seconds: float = 0.0
    signatures: dict = field(default_factory=dict)


class _Counting:
    """Обгортка фетчера смуги: рахує кожен запит (і перевірки існування теж).

    Тестовий фетчер старого вигляду — лише `probe(url, delay) -> код` — працює
    через `check` з семантикою HEAD (без тіла)."""

    def __init__(self, fetcher, stats: LaneStats) -> None:
        self._fetcher = fetcher
        self._stats = stats

    def check(self, url, method="HEAD", delay=None, max_bytes=0) -> ProbeResult:
        self._stats.requests += 1
        if hasattr(self._fetcher, "check"):
            return self._fetcher.check(url, method=method, delay=delay, max_bytes=max_bytes)
        code = self._fetcher.probe(url, delay=delay)
        return ProbeResult(code=int(code), method="HEAD", url=url, final_url=url)


def check_one(item: queue.WorkItem, net, *, cfg, ctx: existence.Context, hooks=(),
              delay: float, now_fn, late=lambda: False) -> tuple[Outcome, ProbeResult]:
    """Один ключ: запит, класифікатор, правило повторного 404, гачки доказів.

    Спільне для смуги циклу (`run_lane`) і нічної смуги (realty/night/lane.py, E9):
    той самий підпис і та сама перевірка існування. `net` — фетчер смуги, що рахує
    запити; `late()` — чи минула стеля часу (тоді перевірок існування не починаємо).
    """
    spec = cfg.hosts[item.host]
    result = net.check(item.url, method=spec.method, delay=delay, max_bytes=spec.max_bytes)
    at = now_fn()
    verdict = classify(spec, cfg.ria_page, item.key, result)
    if verdict.kind == NOT_FOUND and not pol.is_row_key(item.key):
        streak = queue.counted_404s((*item.streak404, at), cfg.repeat_404.min_interval_hours)
        # Після стелі часу перевірок існування не починаємо (2–3 запити ще):
        # лишається «не знайдено» — безпечний бік (рецензія E8, D52).
        if len(streak) >= cfg.repeat_404.count and not late():
            try:
                verdict = existence.resolve(item, verdict, streak, ctx, net, delay)
            except Exception as e:                 # noqa: BLE001 — не перевірили = не знімаємо
                log.warning("%s: перевірка існування %s не вдалась: %s", item.host,
                            item.key, type(e).__name__)
    capture: dict = {}
    for hook in hooks:
        try:
            got = hook(item, result, verdict)
        except Exception as e:                     # noqa: BLE001 — докази не важливіші за перевірку
            log.warning("%s: гачок %s упав: %s", item.host, getattr(hook, "__name__", hook),
                        type(e).__name__)
            continue
        for field_name, value in (got or {}).items():
            if isinstance(value, dict) and isinstance(capture.get(field_name), dict):
                capture[field_name] = {**value, **capture[field_name]}
            else:
                capture.setdefault(field_name, value)
    if verdict.extra:
        # Розібраний стан сторінки (сотні КБ) — лише для гачків; далі не тримаємо.
        verdict = replace(verdict, extra={})
    return Outcome(item, verdict, at, capture), result


# Публічне ім'я для нічної смуги (realty/night/lane.py): той самий лічильник запитів.
CountingFetcher = _Counting


def run_lane(host: str, items: list[queue.WorkItem], *, fetcher, cfg, ctx: existence.Context,
             hooks=(), deadline: float | None = None, now_fn=None,
             mode: str = "cycle") -> tuple[list[Outcome], LaneStats]:
    """Обходить ключі одного хоста послідовно, з його власною паузою."""
    now_fn = now_fn or queue._now
    delay = pol.pace(cfg, host, mode)
    stats = LaneStats(host=host)
    net = _Counting(fetcher, stats)
    started = time.monotonic()
    out: list[Outcome] = []
    consecutive = 0

    def late() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    for n, item in enumerate(items):
        if stats.stopped_early or late():
            for rest in items[n:]:
                out.append(Outcome(rest, None, now_fn()))
            stats.skipped += len(items) - n
            break
        outcome, result = check_one(item, net, cfg=cfg, ctx=ctx, hooks=hooks, delay=delay,
                                    now_fn=now_fn, late=late)
        verdict = outcome.verdict
        out.append(outcome)
        stats.signatures[verdict.signature] = stats.signatures.get(verdict.signature, 0) + 1
        if result.code in BLOCKING_CODES:
            stats.blocked += 1
            consecutive += 1
            if consecutive >= cfg.run.max_consecutive_blocks:
                stats.stopped_early = True
                log.warning("%s відмовляє (%d поспіль) — зупиняємо смугу до наступного прогону",
                            host, consecutive)
        else:
            consecutive = 0
    stats.seconds = round(time.monotonic() - started, 1)
    return out, stats


def run_items(by_host: dict[str, list[queue.WorkItem]], *, fetcher, cfg, hooks=(),
              deadline: float | None = None, now_fn=None, mode: str = "cycle",
              snapshots: dict | None = None) -> tuple[list[Outcome], dict[str, LaneStats]]:
    """Усі смуги паралельно (по потоку на хост); у базу нічого не пишуть."""
    now_fn = now_fn or queue._now
    lanes = {h: items for h, items in by_host.items() if items}
    if not lanes:
        return [], {}
    if snapshots is None:
        snapshots = existence.load_snapshots(cfg, existence.snapshot_sources(cfg))
    ctx = existence.Context(cfg=cfg, now=now_fn(), snapshots=snapshots)
    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = {h: pool.submit(run_lane, h, items, fetcher=fetcher, cfg=cfg, ctx=ctx,
                                  hooks=hooks, deadline=deadline, now_fn=now_fn, mode=mode)
                   for h, items in lanes.items()}
        outcomes: list[Outcome] = []
        stats: dict[str, LaneStats] = {}
        for host, fut in futures.items():
            got, st = fut.result()
            outcomes += got
            stats[host] = st
    return outcomes, stats


def by_host(items) -> dict[str, list[queue.WorkItem]]:
    out: dict[str, list[queue.WorkItem]] = {}
    for item in items:
        out.setdefault(item.host, []).append(item)
    return out


def check_keys(keys, reason: str, hooks=(), *, fetcher=None, cfg=None, scope=None,
               now_fn=None, deadline: float | None = None) -> dict[str, Verdict]:
    """Вердикти для ключів «сайт:id» — без запису в базу (Блок 5, нічні смуги)."""
    from ..db import session_scope
    from ..fetcher import Fetcher

    now_fn = now_fn or queue._now
    cfg = cfg or pol.load()
    scope = scope or session_scope
    with scope() as s:
        items = queue.items_for_keys(s, cfg, keys, reason, now=now_fn())
    own = fetcher is None
    fetcher = fetcher or Fetcher(delay=1.0, use_cache=False, label="verify")
    try:
        outcomes, _ = run_items(by_host(items), fetcher=fetcher, cfg=cfg, hooks=hooks,
                                deadline=deadline, now_fn=now_fn)
    finally:
        if own:
            fetcher.close()
    return {o.item.key: o.verdict for o in outcomes if o.verdict is not None}
