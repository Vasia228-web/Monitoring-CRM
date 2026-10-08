"""Перевірка існування перед зняттям за повторним 404 (рішення власника 1, D46; E8, D52).

Зняти за 404 можна лише тоді, коли 404 повторився `repeat_404.count` разів з
інтервалом І перевірка існування ПОКАЗАЛА, що оголошення справді немає. Тут — друга
половина: стратегії з `hosts.*.existence` по черзі:

  * feed               — лише «є»: рядок ключа бачили в стрічці (OLX/LUN) не
                         давніше за `repeat_404.feed_presence_hours` → існує;
                         адреса з цієї стрічки — кандидат на ремонт. Не бачили —
                         НЕ доказ відсутності (стрічка OLX обрізана 25 сторінками:
                         ~1 100 із 2 488; відсутність у стрічці не знімає НІКОЛИ);
  * snapshot:<джерело> — свіжий ПОВНИЙ перелік джерела (data/snapshots, не
                         старший за alerts.snapshot_stale_hours): DOM.RIA — за
                         id ключа, LUN і flombu — за external_id своїх рядків.
                         «Немає» — лише для ключа ТОГО САМОГО сайту (перелік
                         DOM.RIA для domria:…, LUN для lun:…, flombu для flombu:…);
                         агрегатор, що випустив копію чужого оголошення (LUN →
                         rieltor), — не доказ, що сайт-першоджерело його зняв;
  * ria_api            — картка DOM.RIA за id: «немає» — лише 200 без
                         переадресації, realty_id = id ключа і is_delete/deleted_at;
                         404/410 картки — «не відповіла» (такого підпису Етап 0 не
                         бачив: 100 зі 100 карток — 200); без ознак видалення → існує,
                         beautiful_url — кандидат на ремонт;
  * olx_id_url         — адреса OLX лише з ID (вимкнено, доки не перевірено наживо):
                         «немає» — лише 410; 404 OLX — загальна сторінка помилки.

«Немає» можуть сказати лише ABSENCE_CAPABLE-стратегії; будь-яка інша «немає» —
«не застосовна» (захист від помилки в коді чи конфігу).

Підсумок: існує й кандидат живий → «живе» з ремонтом (listings.probe_url, подія
url_repaired) — НЕ знімаємо; існує без робочого кандидата → лишається «не
знайдено»; хоч одна стратегія не відповіла (мережа, застарілий перелік) →
«не знайдено»; жодна не застосовна → «не знайдено»; усі застосовні сказали «немає»
→ знято з підписом repeat_404. OLX без olx_id_url і rieltor (existence = [])
404-м не знімаються ніколи — лише 410 (питання власнику, D52).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import policy as pol
from .signatures import ALIVE, NOT_FOUND, REMOVED, Verdict, classify

EXISTS, ABSENT, INCONCLUSIVE, NOT_APPLICABLE = "exists", "absent", "inconclusive", "n/a"
# Стратегії, яким дозволено сказати «немає» (snapshot:<джерело> — ще й лише для ключа
# того самого сайту, див. _snapshot). `feed` — лише «є».
ABSENCE_CAPABLE = frozenset({"ria_api", "olx_id_url"})


@dataclass
class Finding:
    strategy: str
    state: str
    candidates: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Context:
    """Те, що стратегіям потрібно поза мережею: переліки джерел, «зараз», конфіг."""

    cfg: object
    now: datetime
    snapshots: dict = field(default_factory=dict)   # джерело → Snapshot | None


def load_snapshots(cfg, sources) -> dict:
    """Повні переліки для стратегій snapshot:<джерело> (лише позначені complete)."""
    from .. import snapshot

    out = {}
    for name in sources:
        snap = snapshot.load(name)
        out[name] = snap if (snap is not None and snap.complete) else None
    return out


def snapshot_sources(cfg) -> set[str]:
    return {name.split(":", 1)[1] for spec in cfg.hosts.values() for name in spec.existence
            if name.startswith("snapshot:")}


def _feed(item, ctx: Context) -> Finding:
    after = ctx.now - timedelta(hours=ctx.cfg.repeat_404.feed_presence_hours)
    fresh = [r for r in item.rows if r.last_seen and r.last_seen >= after]
    if not fresh:
        # Стрічка OLX обрізана 25 сторінками (snapshot.NOT_ENUMERABLE_REASON):
        # «не бачили» — не доказ відсутності (рецензія E8, D52).
        return Finding("feed", NOT_APPLICABLE, note="у стрічці не видно — не доказ відсутності")
    return Finding("feed", EXISTS, [r.original_url for r in
                                    sorted(fresh, key=lambda r: r.last_seen, reverse=True)])


def _snapshot(name: str, item, ctx: Context) -> Finding:
    source = name.split(":", 1)[1]
    snap = ctx.snapshots.get(source)
    stale_h = ctx.cfg.alerts.snapshot_stale_hours.get(source)
    if snap is None or stale_h is None:
        return Finding(name, INCONCLUSIVE, note="немає повного переліку")
    if ctx.now - snap.taken_at > timedelta(hours=stale_h):
        return Finding(name, INCONCLUSIVE, note="перелік застарів")
    family = pol.family_of_key(item.key)
    if source == "domria" and family == "domria":
        present = pol.id_of_key(item.key) in snap.ids
    else:
        own = [r for r in item.rows if r.source == source]
        if not own:
            return Finding(name, NOT_APPLICABLE)
        present = any(r.external_id in snap.ids for r in own)
    if present:
        return Finding(name, EXISTS)
    if family != source:
        # Перелік агрегатора (LUN) без копії чужого оголошення (rieltor, OLX) — не
        # доказ, що сайт-першоджерело його зняв (рецензія E8, D52).
        return Finding(name, NOT_APPLICABLE, note=f"перелік {source} — не доказ для {family}")
    return Finding(name, ABSENT)


def _ria_api(item, ctx: Context, fetcher, delay: float) -> Finding:
    if pol.family_of_key(item.key) != "domria":
        return Finding("ria_api", NOT_APPLICABLE)
    rules = ctx.cfg.ria_page
    url = rules.api_card_url.format(id=pol.id_of_key(item.key))
    res = fetcher.check(url, method="GET", delay=delay, max_bytes=2_000_000)
    if res.code in (404, 410):
        # Етап 0: 100 зі 100 карток — 200, зняті — 200 з is_delete/deleted_at. 404
        # картки — не перевірений підпис, а він тут вирішував би зняття (рецензія E8).
        return Finding("ria_api", INCONCLUSIVE, note=f"картка {res.code} — не перевірений підпис")
    if res.code != 200 or not res.body:
        return Finding("ria_api", INCONCLUSIVE, note=f"картка {res.code} {res.error or ''}".strip())
    try:
        card = json.loads(res.body)
    except ValueError:
        return Finding("ria_api", INCONCLUSIVE, note="картка не JSON")
    if not isinstance(card, dict):
        return Finding("ria_api", INCONCLUSIVE, note="картка не об'єкт")
    if str(card.get("realty_id")) != pol.id_of_key(item.key):
        return Finding("ria_api", INCONCLUSIVE, note="картка іншого оголошення")
    if any(card.get(k) for k in rules.api_deleted_keys):
        if res.chain:
            return Finding("ria_api", INCONCLUSIVE,
                           note="картка: видалено, але через переадресацію")
        return Finding("ria_api", ABSENT, note="картка: видалено")
    beautiful = card.get("beautiful_url")
    cands = [rules.api_repair_url.format(beautiful_url=beautiful)] if beautiful else []
    return Finding("ria_api", EXISTS, cands)


def _olx_id_url(item, ctx: Context, fetcher, delay: float, spec) -> Finding:
    if not ctx.cfg.olx.id_url_enabled:
        return Finding("olx_id_url", NOT_APPLICABLE, note="вимкнено")
    if pol.family_of_key(item.key) != "olx":
        return Finding("olx_id_url", NOT_APPLICABLE)
    url = ctx.cfg.olx.id_url.format(id=pol.id_of_key(item.key))
    res = fetcher.check(url, method=spec.method or "HEAD", delay=delay, max_bytes=0)
    verdict = classify(spec, ctx.cfg.ria_page, item.key, res)
    if verdict.kind == ALIVE:
        return Finding("olx_id_url", EXISTS, [res.final_url])
    if verdict.kind == REMOVED:
        return Finding("olx_id_url", ABSENT, note=f"код {res.code}")
    # 404 OLX — загальна сторінка «Ой, щось пішло не так» і для живих (22359, Етап 0).
    return Finding("olx_id_url", INCONCLUSIVE,
                   note=f"код {res.code}" if verdict.kind == NOT_FOUND else verdict.signature)


def _guard(f: Finding) -> Finding:
    """«Немає» — лише від стратегій, яким це дозволено (ABSENCE_CAPABLE, snapshot:*)."""
    if f.state != ABSENT or f.strategy in ABSENCE_CAPABLE or f.strategy.startswith("snapshot:"):
        return f
    return Finding(f.strategy, NOT_APPLICABLE, f.candidates,
                   note="ця стратегія не доводить відсутності")


def resolve(item, probe_verdict: Verdict, streak: tuple[datetime, ...], ctx: Context,
            fetcher, delay: float) -> Verdict:
    """Вердикт ключа, чий 404 набрав серію `repeat_404.count` з інтервалом."""
    spec = ctx.cfg.hosts[item.host]
    findings: list[Finding] = []
    for name in spec.existence:
        if name == "feed":
            findings.append(_feed(item, ctx))
        elif name.startswith("snapshot:"):
            findings.append(_snapshot(name, item, ctx))
        elif name == "ria_api":
            findings.append(_ria_api(item, ctx, fetcher, delay))
        elif name == "olx_id_url":
            findings.append(_olx_id_url(item, ctx, fetcher, delay, spec))
    findings = [_guard(f) for f in findings]
    summary = {
        "streak": [t.isoformat(timespec="seconds") for t in streak],
        "existence": [{"strategy": f.strategy, "state": f.state,
                       **({"note": f.note} if f.note else {})} for f in findings],
    }
    exists = [f for f in findings if f.state == EXISTS]
    if exists:
        tried = set()
        probed = pol.probe_url(ctx.cfg, item.host, item.url)
        for f in exists:
            for cand in f.candidates:
                url = pol.probe_url(ctx.cfg, item.host, cand)
                if url in tried or url == probed:
                    continue
                tried.add(url)
                res = fetcher.check(url, method=spec.method, delay=delay,
                                    max_bytes=spec.max_bytes)
                verdict = classify(spec, ctx.cfg.ria_page, item.key, res)
                if verdict.kind == ALIVE:
                    return Verdict(ALIVE, "alive", res.code,
                                   {**verdict.evidence, **summary,
                                    "repair": {"old_url": pol.safe_url(item.url),
                                               "new_url": pol.safe_url(url),
                                               "strategy": f.strategy}},
                                   repaired_url=url, repair_strategy=f.strategy)
        return Verdict(NOT_FOUND, "not_found", probe_verdict.code,
                       {**probe_verdict.evidence, **summary, "result": "exists_no_repair"})
    if any(f.state == INCONCLUSIVE for f in findings):
        return Verdict(NOT_FOUND, "not_found", probe_verdict.code,
                       {**probe_verdict.evidence, **summary, "result": "inconclusive"})
    if any(f.state == ABSENT for f in findings):
        return Verdict(REMOVED, "repeat_404", probe_verdict.code,
                       {**probe_verdict.evidence, **summary, "result": "absent"})
    return Verdict(NOT_FOUND, "not_found", probe_verdict.code,
                   {**probe_verdict.evidence, **summary, "result": "no_strategy"})

