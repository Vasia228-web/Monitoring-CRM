"""Черга перевірок Блоку 1: план прогону ярусами, по ключах «сайт:id» (E8, D52).

Одиниця — ключ `listings.site_key`: один запит на ключ, вердикт — усім його рядкам
(копіям LUN теж). Яруси одного прогону, по порядку, кожен зі стелею з
config/liveness.toml (`hosts.*`):

  1. canary    — контрольні «відомо живі» (свіжі у ВЛАСНІЙ стрічці сайту —
                 hosts.*.canary_sources, не зниклі з переліку, остання перевірка не
                 «знято»): на них стоїть запобіжник;
     random    — `random_per_run` актуальних ключів, рівномірно випадково (із зерном)
                 з усіх, крім узятих контрольними, утриманих запобіжником і тих, що
                 чекають ярусу held: на них (і контрольних) стоїть 20% запобіжника
                 (D56); порція — з `sweep_per_run`;
  2. підказані (спільна стеля `hinted_cap_per_run`):
     opened    — квартири, відкриті під час циклу (відкладені завдання черги
                 ops.lookup_checks) і відкриті нещодавно без перевірки після того;
     repeat404 — серія 404 ключа, якій настав наступний строк (правило D46): до
                 `repeat_404.count` — через min_interval_hours, далі (оголошення
                 існує чи перевірка не відповіла) — repeat_404.after_count_hours;
                 не більше половини спільної стелі (решта — absent і далі);
     absent    — зниклі з повного переліку (absent_since) за графіком
                 run.absent_backoff_hours — «кандидат лише раз» більше не буває;
     reseen    — зняті, які знову з'явились у стрічці (без артефакту D43);
     held      — «знято», не застосоване через запобіжник, після його зняття (у
                 частці запобіжника не рахується — зняття і є схваленням);
  3. rm_sample — випадкова вибірка знятих за removed_sample.window_days (повернення
                 живих і частка повернень на /status);
  4. sweep     — сліпий обхід ключів, яким настав строк (востаннє перевірені новим
                 підписом давніше за recheck_days хоста або ніколи), у порядку
                 `_order()` (навмисно — найімовірніше зняті першими, тому в tiered він
                 у пулі підказаних, D56), по `sweep_per_run` мінус узяті випадкові;
                 після незрозумілих відповідей — графік run.unknown_backoff_hours
                 замість вічного кінця черги.

Тут лише читання бази; мережа — engine.py, запис — apply.py.
"""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, select

from ..models import CheckEvent, DataReport, Listing, PriceEvent
from . import policy as pol

TIER_CANARY, TIER_OPENED, TIER_REPEAT404 = "canary", "opened", "repeat404"
TIER_ABSENT, TIER_RESEEN, TIER_HELD = "absent", "reseen", "held"
TIER_SAMPLE, TIER_SWEEP = "rm_sample", "sweep"
TIER_RANDOM = "random"
HINTED_TIERS = (TIER_OPENED, TIER_REPEAT404, TIER_ABSENT, TIER_RESEEN, TIER_HELD)
PLAN_ORDER = (TIER_CANARY, TIER_RANDOM, *HINTED_TIERS, TIER_SAMPLE, TIER_SWEEP)
# Нічні яруси (диригент `cli.py night`, E9, D53; realty/night/plan.py). Назви — у
# check_events.reason (≤16) і в причині події returned (план Блоку 1: «onetime_reseen
# або legacy_404»):
#   onetime_reseen — M2: зняті старим кодом, але знову бачені в стрічці;
#   legacy_404     — M2: зняті старим кодом за ОДНИМ 404 (до рішення власника 1);
#   onetime_hinted — M3: актуальні ключі без жодної відповіді новим підписом, зниклі з
#                    повного переліку (absent_since);
#   onetime_blind  — M3: решта таких ключів, рівномірно випадково (зерно — ніч, D56);
#   overdue        — догін, коли прострочених ключів хоста понад alerts.coverage_overdue_share.
TIER_M2_RESEEN, TIER_M2_LEGACY404 = "onetime_reseen", "legacy_404"
TIER_M3_HINTED, TIER_M3_BLIND = "onetime_hinted", "onetime_blind"
TIER_OVERDUE = "overdue"
# Нічна робота «held» має ДВА яруси (рецензія E9, D53):
#   held        — незастосоване «знято» (власник, відпустивши запобіжник, бачив саме
#                 ці вердикти — тому запобіжник їх не рахує, як і в кроці циклу, E8);
#   held_return — незастосоване «живе» знятого рядка змішаного ключа: свіжої відповіді
#                 «знято» на такий ключ власник НЕ бачив — вона рахується в запобіжнику,
#                 як і будь-яка перевірка (у tiered — як вибірка знятих: ще актуальні
#                 рядки ключа — у пулі підказаних).
TIER_HELD_RETURN = "held_return"
NIGHT_TIERS = (TIER_M2_RESEEN, TIER_M2_LEGACY404, TIER_M3_HINTED, TIER_M3_BLIND, TIER_OVERDUE)
# Яруси для запобіжника (fuse._pool): у tiered межа `share` — лише для РІВНОМІРНО
# випадкових (random циклу, нічний M3 onetime_blind) і контрольних (рішення власника,
# D55); сліпий обхід sweep і догін overdue навмисно йдуть від найімовірніше знятих
# (`_order`, 08.10: 38% «знято» в sweep DOM.RIA проти 13% у рівномірній вибірці) — вони
# в пулі підказаних (D56). Ті, що перевіряють уже ЗНЯТІ рядки (живі повертаються), — у
# tiered зняті рядки поза частками, ще актуальні рядки їхніх ключів — у пулі підказаних.
RANDOM_POOL_TIERS = (TIER_CANARY, TIER_RANDOM, TIER_M3_BLIND)
REMOVED_TARGET_TIERS = (TIER_SAMPLE, TIER_M2_RESEEN, TIER_M2_LEGACY404, TIER_HELD_RETURN)

# Скільки днів на ринку вважаємо «давно». Свіже оголошення майже напевно ще
# живе, і перевірка його майже не дає інформації — черга має йти не рівномірно.
OLD_LISTING_DAYS = 45
# Наскільки недавнє зниження ціни вважаємо сигналом. Падіння ціни — сильний
# натяк на близьке завершення: продавець поспішає.
PRICE_DROP_WINDOW_DAYS = 21
# Скільки днів відкриття картки тримає об'єкт у пріоритеті.
VIEW_WINDOW_DAYS = 14
# Які джерела має під наглядом різниця списків (snapshot.ENUMERABLE): для них
# сліпий обхід — підстраховка. Дублюється тут, щоб модулі не імпортували один
# одного по колу; збіг перевіряє тест.
SNAPSHOT_COVERED = {"domria", "lun", "flombu"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _order(now: datetime | None = None):
    """Порядок сліпого обходу — навмисно нерівномірний (перенесено з verify.py).

    Вперед ідуть ті, у кого ймовірність зникнення вища: кого не пробували жодного
    разу; на кого скаржились чи кого відкривали; у кого нещодавно впала ціна; хто
    давно на ринку; кого давно не пробували. Перед усім — кількість поспіль
    незрозумілих відповідей. Нерівномірність зміщує криву виживання, якщо не
    знати фактичного графіка перевірок, — тому кожна перевірка пишеться в
    `check_events`.
    """
    now = now or _now()
    old_before = now - timedelta(days=OLD_LISTING_DAYS)
    drop_after = now - timedelta(days=PRICE_DROP_WINDOW_DAYS)
    reported = (select(DataReport.listing_id)
                .where(DataReport.created_at >= now - timedelta(days=14))
                .scalar_subquery())
    viewed_after = now - timedelta(days=VIEW_WINDOW_DAYS)
    recent_drop = (select(PriceEvent.listing_id)
                   .where(PriceEvent.observed_at >= drop_after)
                   .group_by(PriceEvent.listing_id)
                   .having(func.count(PriceEvent.id) > 1)
                   .scalar_subquery())
    return (
        Listing.check_failures.asc(),
        case((Listing.last_attempt.is_(None), 0), else_=1),
        case((Listing.source.in_(SNAPSHOT_COVERED), 1), else_=0),
        case((Listing.id.in_(reported), 0), else_=1),
        case((and_(Listing.views > 0, Listing.viewed_at >= viewed_after), 0), else_=1),
        case((Listing.id.in_(recent_drop), 0), else_=1),
        case((Listing.published_at < old_before, 0), else_=1),
        Listing.last_attempt.asc(),
        Listing.id.asc(),
    )


# --- Рядки й ключі ------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    id: int
    site_key: str | None
    source: str
    external_id: str
    original_url: str
    probe_url: str | None
    is_active: bool
    manual_active: bool | None
    delisted_at: datetime | None
    last_seen: datetime | None
    last_attempt: datetime | None
    last_checked: datetime | None
    check_failures: int
    absent_since: datetime | None
    viewed_at: datetime | None
    property_id: int | None


ROW_COLUMNS = (Listing.id, Listing.site_key, Listing.source, Listing.external_id,
               Listing.original_url, Listing.probe_url, Listing.is_active,
               Listing.manual_active, Listing.delisted_at, Listing.last_seen,
               Listing.last_attempt, Listing.last_checked, Listing.check_failures,
               Listing.absent_since, Listing.viewed_at, Listing.property_id)


def _row(r) -> Row:
    return Row(id=r[0], site_key=r[1], source=r[2], external_id=str(r[3]),
               original_url=r[4] or "", probe_url=r[5], is_active=bool(r[6]),
               manual_active=r[7], delisted_at=r[8], last_seen=r[9], last_attempt=r[10],
               last_checked=r[11], check_failures=int(r[12] or 0), absent_since=r[13],
               viewed_at=r[14], property_id=r[15])


def key_of(row: Row) -> str:
    return row.site_key or pol.row_key(row.id)


@dataclass
class WorkItem:
    """Один ключ у плані: що питати, де, яким ярусом і кому застосувати вердикт."""

    key: str
    host: str
    url: str
    tier: str
    rows: tuple[Row, ...]
    # Зараховані 404 поточної серії (від старого до нового), для правила D46.
    streak404: tuple[datetime, ...] = ()
    # Завдання черги перевірки при відкритті, які цей ключ закриває.
    jobs: tuple[int, ...] = ()

    @property
    def listing_ids(self) -> list[int]:
        return [r.id for r in self.rows]

    @property
    def sources(self) -> set[str]:
        return {r.source for r in self.rows}


@dataclass
class KeyHistory:
    """Перевірки ключа НОВИМ підписом (check_events.signature не NULL)."""

    events: list[tuple[datetime, str, bool | None]] = field(default_factory=list)

    @property
    def latest(self):
        return self.events[-1] if self.events else None

    def last_understandable(self) -> datetime | None:
        for at, _sig, alive in reversed(self.events):
            if alive is not None:
                return at
        return None

    def last_any(self) -> datetime | None:
        return self.events[-1][0] if self.events else None

    def answers_since(self, since: datetime) -> int:
        """Скільки перевірок із відповіддю (живе, знято, 404) — від `since`."""
        return len({at for at, sig, alive in self.events
                    if at >= since and (alive is not None or sig == "not_found")})

    def streak404(self, min_interval_h: float) -> tuple[datetime, ...]:
        """Зараховані 404 поточної серії: поспіль від кінця, будь-яка інша
        відповідь обриває; сусідні зараховані — не ближче за інтервал."""
        times: list[datetime] = []
        for at, sig, _alive in reversed(self.events):
            if sig != "not_found":
                break
            times.append(at)
        return counted_404s(times, min_interval_h)


def counted_404s(times, min_interval_h: float) -> tuple[datetime, ...]:
    out: list[datetime] = []
    gap = timedelta(hours=min_interval_h)
    for at in sorted(times):
        if not out or at - out[-1] >= gap:
            out.append(at)
    return tuple(out)


def load_history(session, *, since: datetime, keys=None) -> dict[str, KeyHistory]:
    """Перевірки новим підписом від `since`, згруповані за ключем (одна на прогін)."""
    stmt = (select(Listing.id, Listing.site_key, CheckEvent.checked_at, CheckEvent.signature,
                   CheckEvent.alive)
            .join(Listing, Listing.id == CheckEvent.listing_id)
            .where(CheckEvent.signature.isnot(None), CheckEvent.checked_at >= since))
    rows = []
    if keys is None:
        rows = session.execute(stmt).all()
    else:
        site_keys = [k for k in keys if not pol.is_row_key(k)]
        row_ids = [int(k[len(pol.ROW_KEY_PREFIX):]) for k in keys if pol.is_row_key(k)]
        for chunk in _chunks(site_keys, 500):
            rows += session.execute(stmt.where(Listing.site_key.in_(chunk))).all()
        for chunk in _chunks(row_ids, 500):
            rows += session.execute(stmt.where(Listing.id.in_(chunk),
                                               Listing.site_key.is_(None))).all()
    seen: dict[str, dict[datetime, tuple]] = defaultdict(dict)
    for lid, site_key, at, sig, alive in rows:
        key = site_key or pol.row_key(lid)
        seen[key].setdefault(at, (at, sig, alive))
    return {k: KeyHistory(sorted(v.values(), key=lambda e: e[0])) for k, v in seen.items()}


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def history_window(cfg) -> timedelta:
    """Скільки історії потрібно плану: найдовший строк хоста, вікно вибірки знятих
    і серія 404 (count × інтервал) — з днем запасу."""
    days = max([s.recheck_days for s in cfg.hosts.values()]
               + [cfg.removed_sample.window_days,
                  cfg.repeat_404.count * cfg.repeat_404.min_interval_hours / 24 * 4])
    return timedelta(days=days + 1)


def _group(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        out[key_of(r)].append(r)
    return out


def _probe_for(cfg, host: str, rows: list[Row]) -> str:
    """Адреса запиту: полагоджена (probe_url) першою, інакше — адреса рядка з
    найсвіжішим «бачили», нормалізована під хост (links)."""
    def fresh(r: Row):
        return (r.last_seen or datetime.min, r.id)

    repaired = [r for r in rows if r.probe_url]
    pick = max(repaired, key=fresh) if repaired else max(rows, key=fresh)
    return pol.probe_url(cfg, host, pick.probe_url or pick.original_url)


def make_item(cfg, key: str, rows: list[Row], tier: str, *, history: KeyHistory | None,
              jobs=()) -> WorkItem | None:
    host = pol.host_for(cfg, key, rows[0].original_url)
    if host is None or not cfg.hosts[host].checkable:
        return None
    streak = history.streak404(cfg.repeat_404.min_interval_hours) if history else ()
    return WorkItem(key=key, host=host, url=_probe_for(cfg, host, rows), tier=tier,
                    rows=tuple(sorted(rows, key=lambda r: r.id)), streak404=streak,
                    jobs=tuple(jobs))


def load_rows(session, *, where=None) -> list[Row]:
    stmt = select(*ROW_COLUMNS)
    if where is not None:
        stmt = stmt.where(where)
    return [_row(r) for r in session.execute(stmt)]


def items_for_keys(session, cfg, keys, tier: str, *, now: datetime | None = None
                   ) -> list[WorkItem]:
    """Точкові ключі (check_keys, Блок 5): усі рядки кожного ключа."""
    now = now or _now()
    keys = list(dict.fromkeys(keys))
    rows: list[Row] = []
    site_keys = [k for k in keys if not pol.is_row_key(k)]
    row_ids = [int(k[len(pol.ROW_KEY_PREFIX):]) for k in keys if pol.is_row_key(k)]
    for chunk in _chunks(site_keys, 500):
        rows += load_rows(session, where=Listing.site_key.in_(chunk))
    for chunk in _chunks(row_ids, 500):
        rows += load_rows(session, where=and_(Listing.id.in_(chunk), Listing.site_key.is_(None)))
    groups = _group(rows)
    history = load_history(session, since=now - history_window(cfg), keys=list(groups))
    out = []
    for key in keys:
        if key in groups:
            item = make_item(cfg, key, groups[key], tier, history=history.get(key))
            if item is not None:
                out.append(item)
    return out


def items_for_rows(session, cfg, ids, tier: str, *, now: datetime | None = None
                   ) -> list[WorkItem]:
    """Точкові оголошення (перевірка при відкритті, `verify_batch(ids=…)`): ключі цих
    рядків — з УСІМА рядками ключа, щоб вердикт дійшов і до копій."""
    keys = []
    for chunk in _chunks(ids, 500):
        for r in load_rows(session, where=Listing.id.in_(chunk)):
            keys.append(key_of(r))
    return items_for_keys(session, cfg, keys, tier, now=now)


# --- План прогону ---------------------------------------------------------------------------


@dataclass
class Plan:
    by_host: dict[str, list[WorkItem]] = field(default_factory=dict)
    tiers: dict[str, dict[str, int]] = field(default_factory=dict)   # хост → ярус → ключів
    jobs: dict[int, int] = field(default_factory=dict)               # завдання → квартира
    # Завдання → ключі, які воно мало перевірити (закривається, лише якщо ВСІ взято й
    # перевірено; решту бере процес перевірки після циклу).
    job_keys: dict[int, set[str]] = field(default_factory=dict)
    held_sources: set[str] = field(default_factory=set)

    @property
    def items(self) -> list[WorkItem]:
        return [i for items in self.by_host.values() for i in items]


def _backoff_ok(cfg, rows: list[Row], now: datetime) -> bool:
    n = max((r.check_failures for r in rows), default=0)
    if n <= 0:
        return True
    last = max((r.last_attempt for r in rows if r.last_attempt), default=None)
    if last is None:
        return True
    sched = cfg.run.unknown_backoff_hours
    return now - last >= timedelta(hours=sched[min(n, len(sched)) - 1])


# --- Спільне для плану циклу й нічного плану (E9, D53) ------------------------------------


@dataclass
class Universe:
    """Один прочит бази для плану: рядки за ключами, історія новим підписом, хост ключа.

    Те саме бачать і `plan_run` (крок циклу), і нічний план (realty/night/plan.py):
    яруси обох рахуються однаковими вибірками (`canary_keys`, `unapplied_removal_keys`,
    `removed_sample_keys`, `due_keys`).
    """

    now: datetime
    groups: dict[str, list[Row]]
    history: dict[str, KeyHistory]
    key_host: dict[str, str]
    wanted: set[str]
    active_keys: list[str] = field(default_factory=list)

    def hist(self, key: str) -> KeyHistory | None:
        return self.history.get(key)


def universe(session, cfg, *, now: datetime, hosts=None,
             history_since: datetime | None = None) -> Universe:
    """Рядки всієї таблиці, згруповані за ключем, з хостами, які перевіряються
    (`hosts` — лише ці), й історією перевірок новим підписом від `history_since`
    (типово — вікно, потрібне плану циклу, `history_window`)."""
    wanted = {h for h, s in cfg.hosts.items() if s.checkable and (not hosts or h in hosts)}
    groups = _group(load_rows(session))
    history = load_history(session, since=history_since if history_since is not None
                           else now - history_window(cfg))
    key_host: dict[str, str] = {}
    for key, rs in groups.items():
        host = pol.host_for(cfg, key, rs[0].original_url)
        if host in wanted:
            key_host[key] = host
    active = [k for k in key_host if any(r.is_active for r in groups[k])]
    return Universe(now=now, groups=groups, history=history, key_host=key_host,
                    wanted=wanted, active_keys=active)


def family_held(u: Universe, cfg, key: str, held) -> bool:
    return cfg.hosts[u.key_host[key]].family in held


def key_held(u: Universe, cfg, key: str, held) -> bool:
    """Сліпа перевірка ключа не витрачає запитів: сайт ключа під запобіжником або
    ВСІ його рядки — з утриманих джерел (як у сліпому обході циклу)."""
    return bool(held) and (family_held(u, cfg, key, held)
                           or all(r.source in held for r in u.groups[key]))


def own_feed_seen(cfg, host: str, rows) -> datetime | None:
    """Остання поява ключа у ВЛАСНІЙ стрічці сайту (рядки джерел
    hosts.*.canary_sources): копія в чужій стрічці «живе» не доводить — 08.10 рядок
    LUN, що вказував на OLX, зробив контрольним уже зняте оголошення (D56)."""
    own = cfg.hosts[host].canary_sources
    return max((r.last_seen for r in rows if r.source in own and r.last_seen), default=None)


def canary_keys(u: Universe, cfg) -> list[str]:
    """Контрольні: усі рядки актуальні, свіжі у власній стрічці сайту (рядок джерела з
    hosts.*.canary_sources — не давніше run.canary_fresh_hours), не зниклі, останнє —
    не «знято».

    Порядок — найдавніша спроба першою (контроль обходить різні ключі)."""
    fresh_after = u.now - timedelta(hours=cfg.run.canary_fresh_hours)
    out = []
    for k in u.active_keys:
        rs = u.groups[k]
        if not all(r.is_active and r.manual_active is not False for r in rs):
            continue
        if any(r.absent_since for r in rs):
            continue
        seen = own_feed_seen(cfg, u.key_host[k], rs)
        if seen is None or seen < fresh_after:
            continue
        h = u.hist(k)
        latest = h.latest if h else None
        if latest is not None and (latest[2] is False or latest[1] == "not_found"):
            continue
        out.append(k)
    out.sort(key=lambda k: (max((r.last_attempt or datetime.min) for r in u.groups[k]), k))
    return out


def random_keys(u: Universe, cfg, rng: random.Random, *, held=(), exclude=()
                ) -> dict[str, list[str]]:
    """Ярус random (D56): по хостах — `random_per_run` актуальних ключів, рівномірно
    випадково без повторів (`rng`; кандидати впорядковані, тож вибір залежить лише від
    зерна, а не від `_order` чи порядку рядків у базі). Не беруться: `exclude` (уже
    взяті — контрольні), ключі під запобіжником (`key_held`, як у сліпому обході) і ті,
    що чекають ярусу held (незастосоване «знято» після зняття запобіжника: власник,
    відпускаючи, схвалив саме ці вердикти, і в частці вони не рахуються, E8).

    Випадкові — СЛІПІ: без підказки, що ключ знято, і без обходу графіків повторів.
    Тому не беруться й ключі, які веде підказаний ярус чи графік: зниклі з переліку
    (absent_since — ярус absent; інакше в «випадкових» опинилась би купа справді знятих
    і запобіжник тримав би за справжні зняття), з останнім вердиктом «не знайдено»
    (серія 404 — ярус repeat404 зі своїм інтервалом) і на паузі після незрозумілих
    відповідей (run.unknown_backoff_hours)."""
    pending = set(unapplied_removal_keys(u, cfg, held))
    pool: dict[str, list[str]] = defaultdict(list)
    for k in u.active_keys:
        if k in exclude or k in pending or (held and key_held(u, cfg, k, held)):
            continue
        rs = u.groups[k]
        if any(r.absent_since for r in rs):
            continue
        h = u.hist(k)
        if h and h.latest and h.latest[1] == "not_found":
            continue
        if not _backoff_ok(cfg, rs, u.now):
            continue
        pool[u.key_host[k]].append(k)
    out: dict[str, list[str]] = {}
    for host in sorted(pool):
        n = cfg.hosts[host].random_per_run
        keys = sorted(pool[host])
        out[host] = rng.sample(keys, min(n, len(keys))) if n > 0 else []
    return out


def unapplied_removal_keys(u: Universe, cfg, held) -> list[str]:
    """«Знято», не застосоване через запобіжник, — джерело й сайт уже відпущені."""
    out = []
    for k in u.active_keys:
        h = u.hist(k)
        if h and h.latest[2] is False and not family_held(u, cfg, k, held) and \
                not any(r.source in held for r in u.groups[k]):
            out.append(k)
    return out


def unapplied_return_keys(u: Universe, cfg, held) -> list[str]:
    """«Живе», не застосоване через запобіжник: остання відповідь ключа новим підписом —
    живе, а рядок без ручної позначки досі знятий (змішаний ключ). Нічний ярус held
    (E9, D53) перевіряє їх знову, щойно джерело й сайт відпущено; крок циклу — ні
    (там ярус held лише для незастосованих знять, E8)."""
    out = []
    for k, rs in u.groups.items():
        if k not in u.key_host:
            continue
        h = u.hist(k)
        if not h or h.latest[2] is not True:
            continue
        if not any(not r.is_active and r.manual_active is None and r.delisted_at
                   and r.delisted_at <= h.latest[0] for r in rs):
            continue
        if family_held(u, cfg, k, held) or any(r.source in held for r in rs):
            continue
        out.append(k)
    return sorted(out)


def removed_sample_keys(u: Universe, cfg, rng: random.Random, *,
                        exclude=()) -> dict[str, list[str]]:
    """Вибірка знятих: ключі зі знятими за window_days, не перевірені новим підписом
    min_gap_days; по хостах, перемішані `rng` (хости — за абеткою)."""
    window_after = u.now - timedelta(days=cfg.removed_sample.window_days)
    gap_after = u.now - timedelta(days=cfg.removed_sample.min_gap_days)
    removed: dict[str, list[str]] = defaultdict(list)
    for k, rs in u.groups.items():
        host = u.key_host.get(k)
        if host is None or k in exclude:
            continue
        if not any(not r.is_active and r.manual_active is None and r.delisted_at
                   and r.delisted_at >= window_after for r in rs):
            continue
        h = u.hist(k)
        last = h.last_any() if h else None
        if last is not None and last >= gap_after:
            continue
        removed[host].append(k)
    for host in sorted(removed):
        keys = sorted(removed[host])
        rng.shuffle(keys)
        removed[host] = keys
    return removed


def last_understandable(u: Universe, key: str) -> datetime | None:
    h = u.hist(key)
    return h.last_understandable() if h else None


def due_keys(u: Universe, cfg, session, *, held=(), exclude=()) -> list[str]:
    """Сліпий обхід: ключі, яким настав строк (востаннє зрозуміло перевірені новим
    підписом давніше за recheck_days хоста або ніколи), у порядку `_order()`."""
    rank = {lid: i for i, lid in enumerate(session.scalars(
        select(Listing.id).where(Listing.is_active.is_(True)).order_by(*_order(u.now))))}
    due = []
    for k in u.active_keys:
        if k in exclude:
            continue
        rs = u.groups[k]
        if held and key_held(u, cfg, k, held):
            continue
        h = u.hist(k)
        if h and h.latest and h.latest[1] == "not_found":
            continue                                   # серією 404 опікується repeat404
        spec = cfg.hosts[u.key_host[k]]
        last_ok = h.last_understandable() if h else None
        if last_ok is not None and u.now - last_ok < timedelta(days=spec.recheck_days):
            continue
        if not _backoff_ok(cfg, rs, u.now):
            continue
        best = min((rank.get(r.id, 10**9) for r in rs if r.is_active), default=10**9)
        due.append((best, k))
    due.sort()
    return [k for _, k in due]


def plan_run(session, cfg, *, now: datetime | None = None, hosts=None,
             limit_per_host: int | None = None, jobs: dict[int, int] | None = None,
             held_sources: set[str] | None = None, rng: random.Random | None = None,
             tiers: tuple[str, ...] | None = None) -> Plan:
    """План прогону циклу: яруси для кожного хоста в межах його порцій.

    `jobs` — {завдання: квартира} відкладених перевірок при відкритті (ярус opened);
    `held_sources` — джерела й сайти під запобіжником: сліпий обхід їхніх ключів
    (усі рядки з утриманих джерел або сайт ключа утримано) не витрачає запитів
    (решта ярусів — так: їх результат видно власнику, але не застосовується);
    `limit_per_host` — ручна стеля на хост (cli --limit; вона ж —
    порція сліпого обходу замість `sweep_per_run`); `tiers` — лише ці яруси
    (verify.collect бере тільки sweep).
    """
    def want(tier: str) -> bool:
        return tiers is None or tier in tiers

    now = now or _now()
    rng = rng or random.Random(int(now.timestamp()))
    held = set(held_sources or ())
    plan = Plan(jobs=dict(jobs or {}), held_sources=held)
    u = universe(session, cfg, now=now, hosts=hosts)
    groups, history, key_host = u.groups, u.history, u.key_host
    taken: set[str] = set()
    picked: dict[str, list[WorkItem]] = {h: [] for h in sorted(u.wanted)}
    counts: dict[str, dict[str, int]] = {h: defaultdict(int) for h in picked}

    def room(host: str) -> int:
        return (limit_per_host - len(picked[host])) if limit_per_host is not None else 10**9

    def take(key: str, tier: str, jobs_=()) -> bool:
        host = key_host.get(key)
        if host is None or key in taken or room(host) <= 0:
            return False
        item = make_item(cfg, key, groups[key], tier, history=history.get(key), jobs=jobs_)
        if item is None:
            return False
        taken.add(key)
        picked[host].append(item)
        counts[host][tier] += 1
        return True

    def fill(tier: str, keys_by_host: dict[str, list[str]], cap_of, jobs_of=None) -> None:
        for host, keys in keys_by_host.items():
            if host not in picked:
                continue
            cap = cap_of(host)
            for key in keys:
                if cap <= 0:
                    break
                if take(key, tier, (jobs_of or {}).get(key, ())):
                    cap -= 1

    def by_host(keys) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for k in keys:
            if k in key_host:
                out[key_host[k]].append(k)
        return out

    hist = u.hist
    active_keys = u.active_keys

    # 1. Контрольні: усі рядки актуальні, свіжі у власній стрічці, не зниклі, останнє —
    # не «знято».
    if want(TIER_CANARY):
        fill(TIER_CANARY, by_host(canary_keys(u, cfg)), lambda h: cfg.hosts[h].canaries_per_run)

    # 1b. Випадкові — одразу після контрольних (з усіх актуальних, а не з того, що
    # лишили підказані): на них і контрольних стоїть 20% запобіжника (D56).
    # Ручна стеля (`--limit`) — випадкових стільки ж ЧАСТКОЮ, як у звичайному прогоні
    # (random_per_run / sweep_per_run від стелі): інакше випадкові з'їдали б усю стелю.
    def random_cap(h: str) -> int:
        spec = cfg.hosts[h]
        if limit_per_host is None:
            return spec.random_per_run
        return limit_per_host * spec.random_per_run // max(spec.sweep_per_run, 1)

    if want(TIER_RANDOM):
        fill(TIER_RANDOM, random_keys(u, cfg, rng, held=held, exclude=taken), random_cap)

    hinted_left = {h: cfg.hosts[h].hinted_cap_per_run for h in picked}

    def fill_hinted(tier, keys, jobs_of=None, cap_of=None):
        if not want(tier):
            return
        for host, ks in by_host(keys).items():
            if host not in picked:
                continue
            own = cap_of(host) if cap_of is not None else 10**9
            for key in ks:
                if hinted_left[host] <= 0 or own <= 0:
                    break
                if take(key, tier, (jobs_of or {}).get(key, ())):
                    hinted_left[host] -= 1
                    own -= 1

    recheck_cut = now - timedelta(minutes=cfg.run.opened_recheck_minutes)
    # 2a. Відкладені перевірки при відкритті (квартири з черги ops.lookup_checks).
    job_keys: dict[str, list[int]] = defaultdict(list)
    if plan.jobs:
        by_prop: dict[int, list[int]] = defaultdict(list)
        for job_id, prop in plan.jobs.items():
            by_prop[prop].append(job_id)
        for k in active_keys:
            for r in groups[k]:
                if r.is_active and r.property_id in by_prop and (
                        r.last_attempt is None or r.last_attempt < recheck_cut):
                    job_keys[k].extend(by_prop[r.property_id])
    # 2b. Відкриті нещодавно без зрозумілої перевірки після відкриття.
    viewed_after = now - timedelta(hours=cfg.run.opened_priority_hours)
    opened = list(job_keys)
    for k in active_keys:
        if k in job_keys:
            continue
        for r in groups[k]:
            if (r.is_active and r.viewed_at and r.viewed_at >= viewed_after
                    and (r.last_checked is None or r.last_checked < r.viewed_at)
                    and (r.last_attempt is None or r.last_attempt < recheck_cut)):
                opened.append(k)
                break
    fill_hinted(TIER_OPENED, opened, {k: tuple(dict.fromkeys(v)) for k, v in job_keys.items()})
    for key, job_ids in job_keys.items():
        for job_id in job_ids:
            plan.job_keys.setdefault(job_id, set()).add(key)

    # 2c. Серія 404, якій настав наступний строк. Серія вже набрала count, а ключ
    # досі актуальний (існує без робочої адреси чи перевірка не відповіла) — далі
    # рідше, за repeat_404.after_count_hours; ярус бере не більше половини спільної
    # стелі, щоб вічні 404 не витісняли зниклих із переліку (рецензія E8, D52).
    gap = timedelta(hours=cfg.repeat_404.min_interval_hours)
    after = cfg.repeat_404.after_count_hours
    repeat = []
    for k in active_keys:
        h = hist(k)
        streak = h.streak404(cfg.repeat_404.min_interval_hours) if h else ()
        if not streak:
            continue
        wait = gap
        if len(streak) >= cfg.repeat_404.count:
            wait = max(gap, timedelta(hours=after[min(len(streak) - cfg.repeat_404.count,
                                                      len(after) - 1)]))
        if h.latest[0] <= now - wait:
            repeat.append((streak[0], k))
    fill_hinted(TIER_REPEAT404, [k for _, k in sorted(repeat)],
                cap_of=lambda h: -(-cfg.hosts[h].hinted_cap_per_run // 2))

    # 2d. Зниклі з переліку — за графіком run.absent_backoff_hours.
    sched = cfg.run.absent_backoff_hours
    absent = []
    for k in active_keys:
        rs = [r for r in groups[k] if r.is_active and r.absent_since]
        if not rs:
            continue
        since = min(r.absent_since for r in rs)
        n = hist(k).answers_since(since) if hist(k) else 0
        if n >= len(sched):
            continue                                   # далі — звичайний строк хоста
        if now >= since + timedelta(hours=sched[n]) and _backoff_ok(cfg, groups[k], now):
            absent.append((since, k))
    fill_hinted(TIER_ABSENT, [k for _, k in sorted(absent)])

    # 2e. Зняті, які знову з'явились у стрічці (без last_seen із вікна D43).
    ignore_before = datetime.fromisoformat(cfg.run.reseen_ignore_before)
    grace = timedelta(hours=cfg.run.reseen_grace_hours)
    reseen = []
    for k, rs in groups.items():
        if k not in key_host:
            continue
        last = hist(k).last_any() if hist(k) else None
        for r in rs:
            if (not r.is_active and r.manual_active is None and r.delisted_at
                    and r.last_seen and r.last_seen >= ignore_before
                    and r.last_seen > r.delisted_at + grace
                    and (last is None or last < r.last_seen)):
                reseen.append((r.last_seen, k))
                break
    fill_hinted(TIER_RESEEN, [k for _, k in sorted(reseen, reverse=True)
                              if _backoff_ok(cfg, groups[k], now)])

    # 2f. «Знято», не застосоване через запобіжник (джерело вже не тримається).
    fill_hinted(TIER_HELD, unapplied_removal_keys(u, cfg, held))

    # 3. Вибірка знятих: випадкові ключі, не перевірені новим підписом min_gap_days.
    removed = removed_sample_keys(u, cfg, rng, exclude=taken)
    if want(TIER_SAMPLE):
        fill(TIER_SAMPLE, removed, lambda h: cfg.hosts[h].removed_sample_per_run)

    # 4. Сліпий обхід: ключі, яким настав строк, у порядку _order(); порція —
    # sweep_per_run мінус узяті випадкові (D56: запитів на прогін не більшає).
    if want(TIER_SWEEP):
        fill(TIER_SWEEP, by_host(due_keys(u, cfg, session, held=held, exclude=taken)),
             lambda h: limit_per_host if limit_per_host is not None
             else max(0, cfg.hosts[h].sweep_per_run - counts[h].get(TIER_RANDOM, 0)))

    for host, items in picked.items():
        items.sort(key=lambda i: PLAN_ORDER.index(i.tier))
        plan.by_host[host] = items
        plan.tiers[host] = dict(counts[host])
    return plan
