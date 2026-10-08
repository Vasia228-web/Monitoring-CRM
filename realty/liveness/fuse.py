"""Запобіжник перевірки актуальності (правило ескалації промту; Блок 1, E8, D52).

Підпис «знято» спрацював на понад `fuse.share` (20%) перевірених оголошень одного
джерела за прогін → для джерела нічого не знімаємо (і не повертаємо), тривога в
Telegram (сторож, watchdog.check_liveness), і так — доки власник не зніме
запобіжник на /status або `cli.py liveness fuse clear --source …`. Оцінка —
ДО будь-якого запису (apply.py), по всьому прогону.

Частку рахуємо двічі — за джерелом рядка (`listings.source`) і за САЙТОМ, чий підпис
спрацював (сімейство хоста: domria, olx, rieltor, lun, flombu): копії LUN чужих
оголошень інакше розчинялись би в пулі «lun» серед здорових копій OLX, і зламаний
підпис DOM.RIA знімав би ключі, що мають лише рядки LUN; у rieltor власного
джерела немає зовсім (рецензія E8, D52). Спрацювання за сайтом тримає його
сімейство (те саме ім'я в ops.liveness_fuse): ключі цього сайту — без змін стану.

Два тлумачення «20% перевірених» — `fuse.mode` (питання власнику ще відкрите, D52):
  * literal — n = кожне перевірене оголошення (рядок) джерела в прогоні, усі яруси;
    «спрацював» — вердикт «знято» (і для вже знятих із вибірки знятих);
  * tiered — сліпі (sweep, нічні onetime_blind і overdue) + контрольні (canary):
    частка > `share` при n ≥ `min_checked`; підказані (opened, repeat404, absent,
    reseen, нічний onetime_hinted) і ще актуальні рядки ключів, що перевіряють зняте
    (вибірка знятих, нічний M2: onetime_reseen, legacy_404, нічний held_return):
    частка > `hinted_share` при n ≥ `hinted_min_checked`; уже зняті рядки цих ярусів
    — поза частками.
    Уночі «прогін» — пакет смуг за ~15 хв (realty/night, E9, D53).
В обох режимах:
  * ≥ `canary_trip_min` «знято» серед контрольних ключів хоста → тримати сайт і
    джерела цих рядків;
  * частку рахуємо лише з n ≥ min_checked (3 оголошення однієї квартири — не ознака
    зламаного підпису);
  * ярус held (перевірка «знято», не застосованого під запобіжником, ПІСЛЯ того як
    власник його зняв) — поза частками: зняття запобіжника і є схваленням цих
    вердиктів; інакше ярус, що за побудовою майже весь «знято», тримав би джерело
    знову щоразу (рецензія E8, D52). Лише held: нічна повторна перевірка
    незастосованого «живе» (ярус held_return) рахується — свіжого «знято» на такий
    ключ власник не бачив (рецензія E9, D53);
  * малий прогін (не крок циклу: перевірка при відкритті, точкова, ручна з --limit)
    рахується разом із перевірками новим підписом за останні `fuse.window_hours`
    (check_events): інакше прогони по одній квартирі ніколи не набирали б n ≥
    min_checked і знімали б без запобіжника. Для відпущеного джерела вікно — не
    раніше за момент, коли власник його відпустив (те, що він бачив, він схвалив).

Стан — ops.liveness_fuse (сайт його читає й знімає; цикл і процес перевірки
читають перед застосуванням); історія спрацювань і знять — ops.liveness_fuse_log.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from .. import ops
from ..models import CheckEvent, Listing
from . import policy as pol
from .queue import BLIND_TIERS, REMOVED_TARGET_TIERS, TIER_CANARY, TIER_HELD
from .signatures import REMOVED

EXAMPLES_MAX = 10
SCOPE_SOURCE, SCOPE_HOST = "source", "host"


@dataclass
class Trip:
    source: str                 # джерело (listings.source) або сімейство сайту
    reason: str                 # share | hinted_share | canary (+ _window для малих прогонів)
    checked: int
    removed: int
    share: float | None
    examples: list[str] = field(default_factory=list)
    scope: str = SCOPE_SOURCE   # чим рахували: source — джерело рядків, host — сайт


def _pool(mode: str, tier: str, active: bool = True) -> str | None:
    if tier == TIER_HELD:
        return None
    if mode == "literal":
        return "all"
    if tier in BLIND_TIERS:
        return "blind"
    if tier in REMOVED_TARGET_TIERS:
        # Вибірка знятих і нічний M2 перевіряють уже ЗНЯТІ рядки: знімати можна лише
        # ще актуальні рядки їхніх ключів (змішаний ключ) — їх і рахуємо.
        return "hinted" if active else None
    return "hinted"


def family_of_host(cfg, host: str | None) -> str | None:
    spec = cfg.hosts.get(host) if host else None
    return spec.family if spec is not None and spec.family else None


def held_for(cfg, item, held: set[str]) -> bool:
    """Ключ під запобіжником: тримається його сайт або джерело хоч одного рядка."""
    return family_of_host(cfg, item.host) in held or any(r.source in held for r in item.rows)


def _limits(fz, pool: str) -> tuple[float, int]:
    if pool == "hinted":
        return fz.hinted_share, fz.hinted_min_checked
    return fz.share, fz.min_checked


def window_counts(session, cfg, *, since: datetime,
                  cleared: dict[str, datetime] | None = None) -> dict:
    """Перевірки новим підписом від `since` — у тих самих пулах, що й `evaluate`.

    Для малих прогонів (не кроку циклу): {(scope, ім'я, пул): [n, «знято»]}. Ярус —
    check_events.reason; сайт — префікс site_key (рядок без ключа — лише джерело).
    `cleared` — {ім'я: коли власник відпустив}: для нього рахуємо лише пізніші.
    Чи був рядок ще актуальним на момент перевірки, тут невідомо — вибірка знятих
    у tiered не рахується (як зняті рядки).
    """
    mode = cfg.fuse.mode
    cleared = cleared or {}
    out: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    rows = session.execute(
        select(Listing.source, Listing.site_key, CheckEvent.reason, CheckEvent.alive,
               CheckEvent.checked_at)
        .join(Listing, Listing.id == CheckEvent.listing_id)
        .where(CheckEvent.signature.isnot(None), CheckEvent.checked_at >= since))
    for source, site_key, tier, alive, at in rows:
        pool = _pool(mode, tier or "", active=False)
        if pool is None:
            continue
        for scope, name in ((SCOPE_SOURCE, source), (SCOPE_HOST, pol.family_of_key(site_key))):
            if name and (name not in cleared or at >= cleared[name]):
                c = out[(scope, name, pool)]
                c[0] += 1
                c[1] += int(alive is False)
    return dict(out)


def cleared_at() -> dict[str, datetime]:
    """{джерело чи сайт: коли власник востаннє відпустив} — для вікна малих прогонів."""
    ops.init_ops()
    with ops.ops_session() as s:
        return {src: at for src, at in s.execute(
            select(ops.LivenessFuse.source, ops.LivenessFuse.cleared_at)
            .where(ops.LivenessFuse.state == "clear", ops.LivenessFuse.cleared_at.isnot(None)))}


def evaluate(outcomes, cfg, *, prior: dict | None = None) -> list[Trip]:
    """Спрацювання за результатами прогону (до запису).

    `prior` — перевірки за вікно (`window_counts`) для малих прогонів: пул, де сам
    прогін не набрав min_checked, рахується разом із ними; тримаємо лише там, де
    цей прогін сам щось «зняв». Пул, що набрав n сам, — лише за цим прогоном.
    """
    fz = cfg.fuse
    counts: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    examples: dict[str, list[str]] = defaultdict(list)
    canary_removed: dict[str, list] = defaultdict(list)
    for oc in outcomes:
        if oc.verdict is None:
            continue
        removed = oc.verdict.kind == REMOVED
        family = family_of_host(cfg, oc.item.host)
        url = pol.safe_url(oc.item.url) if removed else None
        for row in oc.item.rows:
            pool = _pool(fz.mode, oc.item.tier, row.is_active)
            for scope, name in ((SCOPE_SOURCE, row.source), (SCOPE_HOST, family)):
                if not name:
                    continue
                if pool is not None:
                    c = counts[(scope, name, pool)]
                    c[0] += 1
                    c[1] += int(removed)
                if url and len(examples[name]) < EXAMPLES_MAX and url not in examples[name]:
                    examples[name].append(url)
        if removed and oc.item.tier == TIER_CANARY:
            canary_removed[oc.item.host].append(oc.item)
    trips: dict[str, Trip] = {}
    for key in sorted(counts):
        scope, name, pool = key
        n, r = counts[key]
        if r == 0:
            continue
        limit, need = _limits(fz, pool)
        # Прогін, що сам набрав n ≥ min_checked, судимо як є (вікно могло б розбавити
        # його «знято» давнішими «живе»); вікно — лише для замалого прогону.
        pn, pr = (0, 0) if n >= need else (prior or {}).get(key, (0, 0))
        tn, tr = n + pn, r + pr
        if tn >= need and tr / tn > limit:
            reason = "hinted_share" if pool == "hinted" else "share"
            if pn:
                reason += "_window"
            trips.setdefault(name, Trip(name, reason, tn, tr, round(tr / tn, 4),
                                        examples[name], scope))
    for host, items in sorted(canary_removed.items()):
        if len(items) < fz.canary_trip_min:
            continue
        names = {family_of_host(cfg, host)} | {row.source for item in items for row in item.rows}
        for name in sorted(n for n in names if n):
            trips.setdefault(name, Trip(name, "canary", len(items), len(items), None,
                                        examples[name], SCOPE_HOST))
    return sorted(trips.values(), key=lambda t: t.source)


def held_sources() -> set[str]:
    ops.init_ops()
    with ops.ops_session() as s:
        return set(s.scalars(select(ops.LivenessFuse.source)
                             .where(ops.LivenessFuse.state == "held")))


def _log(s, source: str, action: str, *, by: str | None = None, run_id=None, mode=None,
         t: Trip | None = None) -> None:
    s.add(ops.LivenessFuseLog(
        source=source, action=action, at=ops._now(), by=by[:32] if by else None,
        run_id=run_id, mode=mode, reason=t.reason if t else None,
        checked=t.checked if t else None, removed=t.removed if t else None,
        share=t.share if t else None,
        examples=json.dumps(t.examples, ensure_ascii=False) if t else None))


def trip(trips: list[Trip], *, run_id: int | None, mode: str) -> list[str]:
    """Записати спрацювання (джерело вже під запобіжником — лишається, як було).

    Поточний стан — ops.liveness_fuse (рядок переписується); кожне нове
    спрацювання — ще й рядок ops.liveness_fuse_log (історія лишається)."""
    if not trips:
        return []
    ops.init_ops()
    newly = []
    with ops.ops_session() as s:
        for t in trips:
            row = s.get(ops.LivenessFuse, t.source)
            if row is not None and row.state == "held":
                continue
            if row is None:
                row = ops.LivenessFuse(source=t.source)
                s.add(row)
            row.state, row.mode, row.reason = "held", mode, t.reason[:24]
            row.tripped_at, row.run_id = ops._now(), run_id
            row.checked, row.removed, row.share = t.checked, t.removed, t.share
            row.examples = json.dumps(t.examples, ensure_ascii=False)
            row.cleared_at = row.cleared_by = None
            _log(s, t.source, "trip", run_id=run_id, mode=mode, t=t)
            newly.append(t.source)
    return newly


def clear(source: str, *, by: str) -> bool:
    """Зняти запобіжник (власник): True — джерело трималось і тепер відпущене."""
    ops.init_ops()
    with ops.ops_session() as s:
        row = s.get(ops.LivenessFuse, source)
        if row is None or row.state != "held":
            return False
        row.state = "clear"
        row.cleared_at = ops._now()
        row.cleared_by = by[:32]
        _log(s, source, "clear", by=by, run_id=row.run_id, mode=row.mode)
        return True


def state() -> list[dict]:
    """Усі джерела, що колись спрацьовували (для /status і сторожа)."""
    ops.init_ops()
    with ops.ops_session() as s:
        rows = s.scalars(select(ops.LivenessFuse).order_by(ops.LivenessFuse.source)).all()
        return [{"source": r.source, "state": r.state, "mode": r.mode, "reason": r.reason,
                 "tripped_at": ops.as_utc_iso(r.tripped_at), "run_id": r.run_id,
                 "checked": r.checked, "removed": r.removed, "share": r.share,
                 "examples": json.loads(r.examples or "[]"),
                 "cleared_at": ops.as_utc_iso(r.cleared_at), "cleared_by": r.cleared_by}
                for r in rows]


def history(limit: int = 20) -> list[dict]:
    """Останні спрацювання й зняття (ops.liveness_fuse_log), новіші першими."""
    ops.init_ops()
    with ops.ops_session() as s:
        rows = s.scalars(select(ops.LivenessFuseLog)
                         .order_by(ops.LivenessFuseLog.id.desc()).limit(limit)).all()
        return [{"source": r.source, "action": r.action, "at": ops.as_utc_iso(r.at),
                 "by": r.by, "run_id": r.run_id, "mode": r.mode, "reason": r.reason,
                 "checked": r.checked, "removed": r.removed, "share": r.share}
                for r in rows]


def window_since(cfg, now: datetime) -> datetime:
    return now - timedelta(hours=cfg.fuse.window_hours)
