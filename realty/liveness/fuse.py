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

Два тлумачення «20% перевірених» — `fuse.mode` (рішення власника 08.10 — tiered, D55):
  * literal — n = кожне перевірене оголошення (рядок) джерела в прогоні, усі яруси;
    «спрацював» — вердикт «знято» (і для вже знятих із вибірки знятих);
  * tiered — пул ВИПАДКОВИХ і контрольних (queue.RANDOM_POOL_TIERS: canary, random
    циклу, нічний onetime_blind — обидва рівномірно випадкові, D56): межа `share` за
    `share_test` (wilson95 — нижня межа 95% інтервалу Вілсона частки понад `share`,
    raw — сама частка) при n ≥ `min_checked`; підказані (opened, repeat404, absent,
    reseen, нічний onetime_hinted), сліпий обхід sweep і нічний догін overdue
    (навмисно від найімовірніше знятих — `queue._order`; 08.10 sweep DOM.RIA: 38%
    «знято» проти 13% у рівномірній вибірці, D56) і ще актуальні рядки ключів, що
    перевіряють зняте (вибірка знятих, нічний M2: onetime_reseen, legacy_404, нічний
    held_return): частка > `hinted_share` при n ≥ `hinted_min_checked`; уже зняті
    рядки цих ярусів — поза частками.
    Уночі «прогін» — пакет смуг за ~15 хв (realty/night, E9, D53).
В обох режимах:
  * ≥ `canary_trip_min` «знято» серед контрольних ключів хоста → тримати сайт і
    джерела цих рядків. Крім справжнього зняття (D56): сторінка назвала дату зняття
    (DOM.RIA deleted_at) ПІЗНІШУ за останню появу ключа у власній стрічці сайту —
    його зняли вже після того, як ми його бачили; таке записуємо (звіт прогону,
    `canary_genuine`), а не тримаємо. «Знято» без дати чи з давнішою датою — тримаємо;
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
  * tiered: пул випадкових і контрольних, що сам не набрав min_checked, — разом із
    такими перевірками за `fuse.random_window_hours`, І в кроці циклу (інші пули
    кроку циклу вікна не мають): rieltor (10 + 4), lun і flombu (2 + 2) набирають
    min_checked лише за кілька циклів (D56).

Стан — ops.liveness_fuse (сайт його читає й знімає; цикл і процес перевірки
читають перед застосуванням); історія спрацювань і знять — ops.liveness_fuse_log.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from .. import ops
from ..models import CheckEvent, Listing
from . import policy as pol
from .queue import (RANDOM_POOL_TIERS, REMOVED_TARGET_TIERS, SWEEP_POOL_TIERS, TIER_CANARY,
                    TIER_HELD, own_feed_seen)
from .signatures import REMOVED

EXAMPLES_MAX = 10
SCOPE_SOURCE, SCOPE_HOST = "source", "host"
# Пули частки: literal — усе; tiered — випадкові й контрольні / сліпий обхід і догін /
# підказані (D56).
POOL_ALL, POOL_RANDOM, POOL_SWEEP, POOL_HINTED = "all", "random", "sweep", "hinted"
# Допуск годинника для дати зняття DOM.RIA (сторінка може бути на хвилини попереду).
GENUINE_SKEW = timedelta(minutes=10)
# fuse.share_test = "wilson95": двобічний 95% інтервал — z = Φ⁻¹(0,975). Це означення
# самого тесту з назви в конфігу, а не поріг (поріг — fuse.share).
WILSON95_Z = 1.959963984540054


@dataclass
class Trip:
    source: str                 # джерело (listings.source) або сімейство сайту
    reason: str                 # share | hinted_share | canary (+ _window для малих прогонів)
    checked: int
    removed: int
    share: float | None
    examples: list[str] = field(default_factory=list)
    scope: str = SCOPE_SOURCE   # чим рахували: source — джерело рядків, host — сайт
    # Нижня межа 95% інтервалу Вілсона частки (лише tiered, пул випадкових, wilson95).
    lower: float | None = None


def wilson_lower(removed: int, n: int, z: float = WILSON95_Z) -> float:
    """Нижня межа інтервалу Вілсона для частки removed / n (0 при n = 0)."""
    if n <= 0:
        return 0.0
    p = removed / n
    zz = z * z
    centre = p + zz / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + zz / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + zz / n))


def _pool(mode: str, tier: str, active: bool = True) -> str | None:
    if tier == TIER_HELD:
        return None
    if mode == "literal":
        return POOL_ALL
    if tier in RANDOM_POOL_TIERS:
        return POOL_RANDOM
    if tier in SWEEP_POOL_TIERS:
        # Сліпий обхід і нічний догін навмисно йдуть від найімовірніше знятих
        # (`queue._order`) — окремий пул із межею хоста `hosts.*.sweep_share` за
        # виміряною часткою (рецензія D56: у пулі підказаних з 97% зламаний підпис
        # знімав би сотні живих обходом раніше, ніж спрацюють 20–30 випадкових).
        return POOL_SWEEP
    if tier in REMOVED_TARGET_TIERS:
        # Вибірка знятих і нічний M2 перевіряють уже ЗНЯТІ рядки: знімати можна лише
        # ще актуальні рядки їхніх ключів (змішаний ключ) — їх і рахуємо.
        return POOL_HINTED if active else None
    return POOL_HINTED


def family_of_host(cfg, host: str | None) -> str | None:
    spec = cfg.hosts.get(host) if host else None
    return spec.family if spec is not None and spec.family else None


def held_for(cfg, item, held: set[str]) -> bool:
    """Ключ під запобіжником: тримається його сайт або джерело хоч одного рядка."""
    return family_of_host(cfg, item.host) in held or any(r.source in held for r in item.rows)


def sweep_share_for(cfg, name: str) -> float:
    """Межа пулу обходу для джерела чи сайту `name`: хост цього сімейства; джерело без
    власного хоста (копії) — найсуворіша з меж хостів."""
    for spec in cfg.hosts.values():
        if spec.family == name and spec.checkable:
            return spec.sweep_share
    return min(s.sweep_share for s in cfg.hosts.values() if s.checkable)


def _limits(fz, pool: str, cfg=None, name: str | None = None) -> tuple[float, int]:
    if pool == POOL_HINTED:
        return fz.hinted_share, fz.hinted_min_checked
    if pool == POOL_SWEEP:
        return sweep_share_for(cfg, name), fz.sweep_min_checked
    return fz.share, fz.min_checked


def _over(fz, pool: str, n: int, r: int, cfg=None, name: str | None = None
          ) -> tuple[bool, float | None]:
    """(тримати?, нижня межа Вілсона чи None) для пулу з n ≥ потрібного."""
    limit, _need = _limits(fz, pool, cfg, name)
    if fz.mode == "tiered" and pool == POOL_RANDOM and fz.share_test == "wilson95":
        lower = wilson_lower(r, n)
        return lower > limit, round(lower, 4)
    return r / n > limit, None


def genuine_canary_removal(cfg, oc, now: datetime | None = None) -> bool:
    """«Знято» на контрольному — справжнє зняття, а не зламаний підпис (D56): сторінка
    назвала дату зняття на джерелі (DOM.RIA deleted_at → verdict.source_removed_at)
    ПІЗНІШУ за останню появу ключа у власній стрічці сайту (hosts.*.canary_sources) і
    не в майбутньому (`now` + GENUINE_SKEW) — його зняли вже після того, як ми його
    бачили. Без дати, з давнішою чи майбутньою — ні. Скільки таких на хост за прогін
    дозволено — fuse.canary_genuine_max (`evaluate`)."""
    at = getattr(oc.verdict, "source_removed_at", None)
    if at is None or oc.item.host not in cfg.hosts:
        return False
    seen = own_feed_seen(cfg, oc.item.host, oc.item.rows)
    limit = (now or getattr(oc, "at", None) or at) + GENUINE_SKEW
    return seen is not None and seen < at <= limit


def window_counts(session, cfg, *, since: datetime,
                  cleared: dict[str, datetime] | None = None,
                  random_since: datetime | None = None, pools=None) -> dict:
    """Перевірки новим підписом від `since` — у тих самих пулах, що й `evaluate`.

    {(scope, ім'я, пул): [(коли, «знято»), …]} — новіші першими: `evaluate` добирає з
    них лише стільки найсвіжіших, скільки бракує прогону до min_checked (рецензія D56:
    усе вікно розбавляло б свіжу поломку давніми «живе»). У tiered одна перевірка
    КЛЮЧА — одна одиниця (рядки ключа — та сама відповідь сайту, не незалежні спроби);
    у literal — рядок, як і досі. Ярус — check_events.reason; сайт — префікс site_key
    (рядок без ключа — лише джерело). `cleared` — {ім'я: коли власник відпустив}: для
    нього лише пізніші. `random_since` — у tiered окремий початок вікна пулу випадкових
    і контрольних (fuse.random_window_hours); `pools` — лише ці пули (крок циклу — лише
    пул випадкових). Чи був рядок ще актуальним на момент перевірки, тут невідомо —
    вибірка знятих у tiered не рахується (як зняті рядки).
    """
    mode = cfg.fuse.mode
    cleared = cleared or {}
    rsince = random_since if random_since is not None and mode == "tiered" else since
    long_pools = (POOL_RANDOM, POOL_SWEEP)
    only_long = mode == "tiered" and pools is not None and set(pools) <= set(long_pools)
    stmt = (select(Listing.id, Listing.source, Listing.site_key, CheckEvent.reason,
                   CheckEvent.alive, CheckEvent.checked_at)
            .join(Listing, Listing.id == CheckEvent.listing_id)
            .where(CheckEvent.signature.isnot(None),
                   CheckEvent.checked_at >= (rsince if only_long else min(since, rsince))))
    if only_long:
        tiers = (RANDOM_POOL_TIERS if POOL_RANDOM in pools else ()) + \
            (SWEEP_POOL_TIERS if POOL_SWEEP in pools else ())
        stmt = stmt.where(CheckEvent.reason.in_(tiers))
    seen: set = set()
    out: dict[tuple[str, str, str], list] = defaultdict(list)
    for lid, source, site_key, tier, alive, at in session.execute(stmt):
        pool = _pool(mode, tier or "", active=False)
        if pool is None or (pools is not None and pool not in pools):
            continue
        if at < (rsince if pool in long_pools else since):
            continue
        unit = (site_key or f"id:{lid}", tier, at) if mode == "tiered" else (lid, tier, at)
        for scope, name in ((SCOPE_SOURCE, source), (SCOPE_HOST, pol.family_of_key(site_key))):
            if not name or (name in cleared and at < cleared[name]):
                continue
            mark = (scope, name, pool, unit)
            if mark in seen:
                continue
            seen.add(mark)
            out[(scope, name, pool)].append((at, int(alive is False)))
    for checks in out.values():
        checks.sort(key=lambda c: c[0], reverse=True)
    return dict(out)


def _top_up(prior_value, missing: int) -> tuple[int, int]:
    """Скільки (n, «знято») додати з вікна, щоб прогону вистачило до min_checked:
    найсвіжіші перевірки, не більше `missing`. Старий вигляд (n, r) — як є (тести)."""
    if not prior_value:
        return 0, 0
    if isinstance(prior_value, tuple) and len(prior_value) == 2 \
            and all(isinstance(x, int) for x in prior_value):
        return prior_value
    take = list(prior_value)[:max(0, missing)]
    return len(take), sum(r for _at, r in take)


def cleared_at() -> dict[str, datetime]:
    """{джерело чи сайт: коли власник востаннє відпустив} — для вікна малих прогонів."""
    ops.init_ops()
    with ops.ops_session() as s:
        return {src: at for src, at in s.execute(
            select(ops.LivenessFuse.source, ops.LivenessFuse.cleared_at)
            .where(ops.LivenessFuse.state == "clear", ops.LivenessFuse.cleared_at.isnot(None)))}


def prior_counts(session, cfg, now: datetime, *, cycle: bool) -> dict | None:
    """Перевірки за вікна для пулів, що самі не набрали потрібного n (`evaluate`).

    Крок циклу — у tiered лише пули випадкових і обходу (fuse.random_window_hours;
    підказані кроку циклу вікна не мають, як і досі); малий прогін і нічний пакет —
    усі пули: випадкові й обхід — за random_window_hours, решта — за window_hours.
    Добирається лише бракуюче до min_checked, найсвіжіше першим (`evaluate`).
    Для відпущеного джерела — не раніше, ніж власник його відпустив."""
    fz = cfg.fuse
    if cycle and fz.mode != "tiered":
        return None
    return window_counts(session, cfg, since=window_since(cfg, now),
                         random_since=random_window_since(cfg, now), cleared=cleared_at(),
                         pools=(POOL_RANDOM, POOL_SWEEP) if cycle else None)


def evaluate(outcomes, cfg, *, prior: dict | None = None,
             report: dict | None = None) -> list[Trip]:
    """Спрацювання за результатами прогону (до запису).

    `prior` — перевірки за вікно (`window_counts`/`prior_counts`): пулу, що в прогоні
    не набрав min_checked, додаємо НАЙСВІЖІШІ з них — рівно скільки бракує; тримаємо
    лише там, де цей прогін сам щось «зняв». Пул, що набрав n сам, — лише за прогоном.
    У tiered одиниця — перевірений КЛЮЧ (рядки ключа — одна відповідь сайту; рецензія
    D56), у literal — рядок, як і досі.
    `report` (необов'язково) — сюди: `pools` — кожен пул, де цей прогін щось «зняв»
    (n, вікно, чи оцінено, частка, нижня межа, чи тримає), і `canary_genuine` —
    справжні зняття контрольних (не тримають, якщо їх на хост не більше за
    fuse.canary_genuine_max; D56).
    """
    fz = cfg.fuse
    counts: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    examples: dict[str, list[str]] = defaultdict(list)
    canary_removed: dict[str, list] = defaultdict(list)
    genuine_by_host: dict[str, list] = defaultdict(list)
    for oc in outcomes:
        if oc.verdict is None:
            continue
        removed = oc.verdict.kind == REMOVED
        family = family_of_host(cfg, oc.item.host)
        url = pol.safe_url(oc.item.url) if removed else None
        units: set[tuple[str, str, str]] = set()
        for row in oc.item.rows:
            pool = _pool(fz.mode, oc.item.tier, row.is_active)
            for scope, name in ((SCOPE_SOURCE, row.source), (SCOPE_HOST, family)):
                if not name:
                    continue
                if pool is not None:
                    if fz.mode == "tiered":
                        units.add((scope, name, pool))
                    else:
                        c = counts[(scope, name, pool)]
                        c[0] += 1
                        c[1] += int(removed)
                if url and len(examples[name]) < EXAMPLES_MAX and url not in examples[name]:
                    examples[name].append(url)
        for unit in units:
            c = counts[unit]
            c[0] += 1
            c[1] += int(removed)
        if removed and oc.item.tier == TIER_CANARY:
            if genuine_canary_removal(cfg, oc):
                genuine_by_host[oc.item.host].append(oc)
            else:
                canary_removed[oc.item.host].append(oc.item)
    # Справжні зняття контрольних — не більше fuse.canary_genuine_max на хост за прогін:
    # «зламаний стан» сторінки показав би дату зняття на багатьох живих одразу — тоді
    # усі вони рахуються як «знято» контрольних і тримають (рецензія D56).
    genuine: list[dict] = []
    for host, ocs in genuine_by_host.items():
        if len(ocs) > fz.canary_genuine_max:
            canary_removed[host].extend(oc.item for oc in ocs)
            continue
        for oc in ocs:
            seen = own_feed_seen(cfg, oc.item.host, oc.item.rows)
            genuine.append({"key": oc.item.key, "host": oc.item.host,
                            "url": pol.safe_url(oc.item.url),
                            "source_removed_at": oc.verdict.source_removed_at.isoformat(),
                            "seen": seen.isoformat() if seen else None})
    trips: dict[str, Trip] = {}
    pools_report: list[dict] = []
    for key in sorted(counts):
        scope, name, pool = key
        n, r = counts[key]
        if r == 0:
            continue
        _limit, need = _limits(fz, pool, cfg, name)
        # Прогін, що сам набрав n ≥ min_checked, судимо як є (вікно могло б розбавити
        # його «знято» давнішими «живе»); вікно — лише для замалого прогону.
        pn, pr = (0, 0) if n >= need else _top_up((prior or {}).get(key), need - n)
        tn, tr = n + pn, r + pr
        evaluated = tn >= need
        over, lower = _over(fz, pool, tn, tr, cfg, name) if evaluated else (False, None)
        pools_report.append({"scope": scope, "name": name, "pool": pool, "checked": n,
                             "removed": r, "window_checked": pn, "window_removed": pr,
                             "evaluated": evaluated, "share": round(tr / tn, 4) if tn else None,
                             "lower": lower, "tripped": over})
        if over:
            reason = {POOL_HINTED: "hinted_share", POOL_SWEEP: "sweep_share"}.get(pool, "share")
            if pn:
                reason += "_window"
            trips.setdefault(name, Trip(name, reason, tn, tr, round(tr / tn, 4),
                                        examples[name], scope, lower))
    for host, items in sorted(canary_removed.items()):
        if len(items) < fz.canary_trip_min:
            continue
        names = {family_of_host(cfg, host)} | {row.source for item in items for row in item.rows}
        for name in sorted(n for n in names if n):
            trips.setdefault(name, Trip(name, "canary", len(items), len(items), None,
                                        examples[name], SCOPE_HOST))
    if report is not None:
        report["pools"] = pools_report
        report["canary_genuine"] = genuine
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


def random_window_since(cfg, now: datetime) -> datetime:
    """Початок вікна пулу випадкових і контрольних (tiered, D56)."""
    return now - timedelta(hours=cfg.fuse.random_window_hours)
