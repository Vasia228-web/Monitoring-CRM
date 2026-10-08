"""План ночі по хостах: роботи реєстру в порядку пріоритету (E9, D53).

Смуга хоста бере ключі «сайт:id» у порядку config/night.toml `jobs.order`; ключ —
лише один раз за план (і за ніч: ключ, який уже пробували після старту першого вікна
цієї ночі, не береться — «повторних запитів до того самого ключа за ніч немає»,
інтеграція, D47). Тут лише читання бази; мережа — lane.py, запис — conductor.py
через liveness.apply.

Роботи перевірки актуальності (яруси — realty/liveness/queue.py):
  canary          — контрольні «відомо живі»: порція хоста `canaries_per_run` на
                    початку смуги і по night jobs.canaries_per_batch у кожному
                    наступному пакеті (spread_canaries, D56);
  held            — вердикт, не застосований через запобіжник, — щойно джерело й сайт
                    відпущено: «знято» актуальному рядку — ярус held (запобіжник його
                    не рахує: власник, відпускаючи, бачив саме ці вердикти), «живе»
                    знятому рядку змішаного ключа — ярус held_return (рахується, як
                    будь-яка перевірка: нову відповідь «знято» власник не бачив;
                    рецензія E9, D53);
  legacy_404      — M2: рядки, які старий код зняв за ОДНИМ 404 (check_events без
                    підпису, код 404, alive = 0 — до рішення власника 1);
  onetime_reseen  — M2: зняті старим кодом, last_seen > delisted_at (усі «знову
                    бачені», разом з артефактом D43 — повертаємо лише тих, кого жива
                    перевірка покаже живими, D47);
                    M2 «відповів», коли після зняття є перевірка новим підписом зі
                    зрозумілою відповіддю або 404: «знято»/404 — лишається знятим
                    (готово); «живе», а рядок досі знятий (запобіжник тримав) —
                    перевіряємо знову;
  onetime_hinted  — M3: актуальні ключі без ЖОДНОЇ відповіді новим підписом, зниклі з
                    повного переліку (absent_since), — давніші першими;
  onetime_blind   — M3: решта таких ключів, рівномірно випадково (D56): зерно —
                    onetime_seed і ніч (обидва вікна ночі — той самий порядок, до кого
                    черга не дійшла в першому, ті в другому першими; наступна ніч —
                    новий порядок). Прохід одноразовий і однаково дійде до всіх ключів:
                    порядок впливає лише на зміщення оцінки частки «знято», а M3 у
                    tiered — у пулі 20% запобіжника разом із контрольними;
  rm_sample       — вибірка знятих за removed_sample.window_days (порція — night.toml);
  overdue         — догін: лише якщо прострочених ключів хоста (немає зрозумілої
                    перевірки новим підписом за 2 × recheck_days) понад
                    alerts.coverage_overdue_share, — ключі, яким настав строк.
Ключ під запобіжником (сайт ключа чи джерело хоч одного рядка — як apply.held_for)
уночі не питаємо зовсім: результат однаково не застосувався б.
identity — дозбір ознак у смузі хоста свого джерела (jobs.identity_sources), останнім.

M2 і M3 одноразові за визначенням стану, а не за прапорцем: коли всі ключі мають
відповідь новим підписом, їхні яруси порожні — вікно лише контрольні, вибірка знятих
і (за потреби) догін, і виходить за хвилини.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from ..liveness import policy as pol, queue
from ..models import CheckEvent, Listing, ListingEvent

# Уся історія перевірок новим підписом (M2/M3 — «хоч одна відповідь будь-коли»).
EPOCH = datetime(2000, 1, 1)


@dataclass
class HostPlan:
    host: str
    pace: float
    items: list = field(default_factory=list)             # WorkItem у порядку смуги
    tiers: dict = field(default_factory=dict)             # ярус → ключів
    identity: str | None = None                           # джерело дозбору identity
    skipped: str | None = None                            # чому смуги немає
    overdue_share: float | None = None
    held_keys: int = 0                                    # ключів під запобіжником — не питаємо
    attempted_tonight: int = 0                            # уже пробували цієї ночі

    @property
    def requests(self) -> int:
        return len(self.items)

    @property
    def seconds(self) -> float:
        """Нижня межа тривалості: кожен ключ — щонайменше один запит у темпі хоста."""
        return self.requests * self.pace

    def as_dict(self) -> dict:
        return {"pace": self.pace, "requests": self.requests,
                "seconds": round(self.seconds, 1), "tiers": dict(self.tiers),
                "identity": self.identity, "skipped": self.skipped,
                "overdue_share": self.overdue_share, "held_keys": self.held_keys,
                "attempted_tonight": self.attempted_tonight}


@dataclass
class NightPlan:
    hosts: dict[str, HostPlan]
    held: set[str]
    now: datetime

    @property
    def lanes(self) -> dict[str, HostPlan]:
        """Хости, яким є що робити (ключі чи дозбір) і яких не зупинено."""
        return {h: p for h, p in self.hosts.items()
                if p.skipped is None and (p.items or p.identity)}

    def as_dict(self) -> dict:
        return {h: p.as_dict() for h, p in sorted(self.hosts.items())}


def legacy_removals(session) -> tuple[set[int], set[int]]:
    """(зняті старим кодом, з них — за одним 404): id рядків.

    «Старим кодом» — без події removed у мить зняття (новий код пише подію з тим самим
    часом, що й delisted_at, apply.py). Ручні позначки (manual_active) — не наші.
    """
    new_code = set(session.scalars(
        select(ListingEvent.listing_id).join(Listing, Listing.id == ListingEvent.listing_id)
        .where(ListingEvent.kind == "removed", ListingEvent.at == Listing.delisted_at)))
    legacy = {lid for lid in session.scalars(
        select(Listing.id).where(Listing.is_active.is_(False), Listing.manual_active.is_(None),
                                 Listing.delisted_at.isnot(None)))
        if lid not in new_code}
    by_404 = set(session.scalars(
        select(CheckEvent.listing_id).where(CheckEvent.signature.is_(None),
                                            CheckEvent.code == 404,
                                            CheckEvent.alive.is_(False)).distinct()))
    return legacy, legacy & by_404


def _answered(h, since: datetime | None = None):
    """Відповіді новим підписом (зрозумілі або 404) від `since` — у часовому порядку."""
    if h is None:
        return []
    return [e for e in h.events if (since is None or e[0] >= since)
            and (e[2] is not None or e[1] == "not_found")]


def overdue_share(u: queue.Universe, cfg, host: str) -> float | None:
    """Частка актуальних ключів хоста без зрозумілої перевірки новим підписом за
    2 × recheck_days (те саме, що «прострочено» у зведенні /status)."""
    cut = u.now - timedelta(days=2 * cfg.hosts[host].recheck_days)
    keys = [k for k in u.active_keys if u.key_host[k] == host]
    if not keys:
        return None
    late = sum(1 for k in keys if (t := queue.last_understandable(u, k)) is None or t < cut)
    return round(late / len(keys), 4)


def night_key(now: datetime, attempted_since: datetime | None = None,
              night: str | None = None) -> str:
    """Ключ ночі для зерна M3: місцева дата ночі (windows.Window.night_date), інакше —
    старт першого вікна ночі (той самий для обох вікон), інакше — дата `now`."""
    if night:
        return night
    if attempted_since is not None:
        return attempted_since.isoformat(timespec="minutes")
    return now.date().isoformat()


def night_batches(ncfg) -> int:
    """Скільки пакетів (lanes.batch_minutes) у найдовшому вікні ночі."""
    from ..configfiles import hhmm_minutes

    longest = max(hhmm_minutes(w.stop_requests) - hhmm_minutes(w.start) for w in ncfg.windows)
    return max(1, math.ceil(longest / ncfg.lanes.batch_minutes))


def spread_canaries(p: HostPlan, first: int, per_batch: int, batch_minutes: float,
                    seconds: float | None = None) -> None:
    """Контрольні — у кожен пакет смуги (D56): перші `first` — на початку, далі по
    `per_batch` через кожні ~batch_minutes × 60 / pace запитів. Ті, що випали б за
    кінець черги, не питаємо (запитів не більшає понад план)."""
    canaries = [i for i in p.items if i.tier == queue.TIER_CANARY]
    if len(canaries) <= first or per_batch <= 0:
        return
    rest = [i for i in p.items if i.tier != queue.TIER_CANARY]
    # Удвічі частіше, ніж уміщує пакет за темпом: справжній крок довший за плановий
    # (DOM.RIA 1,4 с проти 1,0; перевірки існування — тим самим темпом), і за плановим
    # кроком частина пакетів лишалась би без контрольного (перевірка виправлень D56).
    step = max(1, int(batch_minutes * 60 / max(p.pace, seconds or 0, 0.1) / 2))
    extra = canaries[first:]
    out = list(canaries[:first])
    pos = 0
    for b in range(1, len(extra) // per_batch + 1):
        nxt = min(len(rest), b * step)
        if b * step > len(rest):
            break
        out += rest[pos:nxt] + extra[(b - 1) * per_batch: b * per_batch]
        pos = nxt
    out += rest[pos:]
    p.items = out
    p.tiers[queue.TIER_CANARY] = sum(1 for i in out if i.tier == queue.TIER_CANARY)


def build(session, lcfg, ncfg, *, now: datetime, held=frozenset(),
          attempted_since: datetime | None = None, skip_hosts: dict | None = None,
          hosts=None, night: str | None = None) -> NightPlan:
    """План ночі. `now` — UTC без зони (як у базі); `held` — джерела й сайти під
    запобіжником; `attempted_since` — старт першого вікна цієї ночі (UTC): ключі, які
    відтоді вже пробували, не беруться; `skip_hosts` — {хост: чому без смуги};
    `night` — місцева дата ночі (зерно порядку M3, `night_key`)."""
    held = set(held)
    nkey = night_key(now, attempted_since, night)
    skip_hosts = dict(skip_hosts or {})
    order = [j for j in ncfg.jobs.order if j != "identity"]
    u = queue.universe(session, lcfg, now=now, hosts=hosts, history_since=EPOCH)
    plans: dict[str, HostPlan] = {}
    for host in sorted(u.wanted):
        plans[host] = HostPlan(host=host, pace=pol.pace(lcfg, host, "night"),
                               skipped=skip_hosts.get(host))
    if "identity" in ncfg.jobs.order:
        for host, source in ncfg.jobs.identity_sources.items():
            if host in plans:
                plans[host].identity = source
    taken: set[str] = set()

    def is_held(key: str) -> bool:
        return bool(held) and (queue.family_held(u, lcfg, key, held)
                               or any(r.source in held for r in u.groups[key]))

    def tried_tonight(key: str) -> bool:
        return attempted_since is not None and any(
            r.last_attempt is not None and r.last_attempt >= attempted_since
            for r in u.groups[key])

    def take(key: str, tier: str) -> bool:
        host = u.key_host.get(key)
        if host is None or key in taken or plans[host].skipped:
            return False
        p = plans[host]
        if is_held(key):
            p.held_keys += 1
            taken.add(key)
            return False
        if tried_tonight(key):
            p.attempted_tonight += 1
            taken.add(key)
            return False
        item = queue.make_item(lcfg, key, u.groups[key], tier, history=u.hist(key))
        if item is None:
            return False
        taken.add(key)
        p.items.append(item)
        p.tiers[tier] = p.tiers.get(tier, 0) + 1
        return True

    def fill(tier: str, keys, cap_of=None) -> None:
        left: dict[str, int] = {}
        for key in keys:
            host = u.key_host.get(key)
            if host is None:
                continue
            if cap_of is not None:
                left.setdefault(host, cap_of(host))
                if left[host] <= 0:
                    continue
            if take(key, tier) and cap_of is not None:
                left[host] -= 1

    legacy = by_404 = None

    def m2(tier: str) -> list[str]:
        nonlocal legacy, by_404
        if legacy is None:
            legacy, by_404 = legacy_removals(session)
        return _m2_keys(u, lcfg, tier, legacy, by_404)

    batches = night_batches(ncfg)
    for job in order:
        if job == queue.TIER_CANARY:
            fill(job, queue.canary_keys(u, lcfg),
                 lambda h: lcfg.hosts[h].canaries_per_run + 2 * ncfg.jobs.canaries_per_batch
                 * max(0, batches - 1))
        elif job == queue.TIER_HELD:
            fill(queue.TIER_HELD, queue.unapplied_removal_keys(u, lcfg, held))
            # Незастосоване «живе» ключа M2 повертає сам M2 (причина події — його ярус).
            m2_keys = set(m2(queue.TIER_M2_LEGACY404)) | set(m2(queue.TIER_M2_RESEEN))
            fill(queue.TIER_HELD_RETURN, [k for k in queue.unapplied_return_keys(u, lcfg, held)
                                          if k not in m2_keys])
        elif job in (queue.TIER_M2_LEGACY404, queue.TIER_M2_RESEEN):
            fill(job, m2(job))
        elif job in (queue.TIER_M3_HINTED, queue.TIER_M3_BLIND):
            fill(job, _m3_keys(u, lcfg, job, ncfg.jobs.onetime_seed, nkey))
        elif job == queue.TIER_SAMPLE:
            rng = random.Random(f"{ncfg.jobs.onetime_seed}:{now.date().isoformat()}")
            sample = queue.removed_sample_keys(u, lcfg, rng, exclude=taken)
            fill(job, [k for h in sorted(sample) for k in sample[h]],
                 lambda h: ncfg.jobs.rm_sample_per_host.get(h, 0))
        elif job == queue.TIER_OVERDUE:
            late = {}
            for host in plans:
                plans[host].overdue_share = share = overdue_share(u, lcfg, host)
                if share is not None and share > lcfg.alerts.coverage_overdue_share:
                    late[host] = True
            if late:
                fill(job, [k for k in queue.due_keys(u, lcfg, session, held=held, exclude=taken)
                           if u.key_host[k] in late])
    for host, p in plans.items():
        spread_canaries(p, lcfg.hosts[host].canaries_per_run, ncfg.jobs.canaries_per_batch,
                        ncfg.lanes.batch_minutes,
                        ncfg.report.typical_request_seconds.get(host))
    return NightPlan(hosts=plans, held=held, now=now)


def _m2_keys(u: queue.Universe, cfg, tier: str, legacy: set[int], by_404: set[int]) -> list[str]:
    """Ключі M2 ярусу `tier`: legacy_404 — за id, onetime_reseen — свіжіше бачені першими."""
    out = []
    for key, rows in u.groups.items():
        if key not in u.key_host:
            continue
        cand = [r for r in rows if r.id in legacy and not r.is_active
                and r.manual_active is None and r.delisted_at is not None]
        if not cand:
            continue
        is404 = any(r.id in by_404 for r in cand)
        if tier == queue.TIER_M2_LEGACY404 and not is404:
            continue
        if tier == queue.TIER_M2_RESEEN:
            if is404:
                continue                               # уже в legacy_404
            cand = [r for r in cand if r.last_seen and r.last_seen > r.delisted_at]
            if not cand:
                continue
        answers = _answered(u.hist(key), min(r.delisted_at for r in cand))
        if answers and answers[-1][2] is not True:
            continue                                   # знято / 404 новим підписом — готово
        # Без відповіді, або «живе», а рядок досі знятий (запобіжник тримав) — питаємо.
        if not queue._backoff_ok(cfg, rows, u.now):
            continue
        if tier == queue.TIER_M2_LEGACY404:
            out.append((0.0, key))
        else:
            seen = max(r.last_seen for r in cand)
            out.append((-seen.timestamp(), key))
    out.sort()
    return [k for _, k in out]


def _m3_keys(u: queue.Universe, cfg, tier: str, seed: int, night: str) -> list[str]:
    """Ключі M3: актуальні без жодної відповіді новим підписом; hinted — зниклі з
    переліку (давніші першими), blind — решта, рівномірно випадкова перестановка (по
    хостах) із зерном onetime_seed + ніч + хост: від `_order` чи порядку рядків у базі
    не залежить (кандидати спершу впорядковано за ключем), щоночі — нова (D56)."""
    hinted, blind = [], defaultdict(list)
    for key in u.active_keys:
        if _answered(u.hist(key)):
            continue
        rows = u.groups[key]
        if not queue._backoff_ok(cfg, rows, u.now):
            continue
        absent = [r.absent_since for r in rows if r.is_active and r.absent_since]
        if absent:
            hinted.append((min(absent), key))
        elif not all(r.is_active for r in rows):
            # Змішаний ключ (рядок уже знято, копія ще актуальна) — не сліпий: «знято» на
            # ньому здебільшого справжнє і здувало б частку пулу випадкових (D56).
            hinted.append((datetime.min, key))
        else:
            blind[u.key_host[key]].append(key)
    if tier == queue.TIER_M3_HINTED:
        return [k for _, k in sorted(hinted)]
    out = []
    for host in sorted(blind):
        keys = sorted(blind[host])
        random.Random(f"{seed}:{night}:{host}").shuffle(keys)
        out += keys
    return out
