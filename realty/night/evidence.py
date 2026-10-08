"""Нічні роботи доказів Блоків 3 (тип продавця) і 4 (район/ЖК) у диригенті (E11, D60).

Усе, що чекає ночей, — у тих самих смугах хостів, ПІСЛЯ всіх ярусів Блоку 1
(інтеграція, конфлікт 3), у тому самому темпі й з тими самими блокуваннями:

  * olx.ua — смуга рендерів Chromium (night/render.py): спершу вкладки пошуку
    «Приватні» (щоночі) і «Бізнес» (зрізи кімнати × ціна, раз на тиждень) — членство
    оголошень у ops.olx_tab_seen; потім ОДНА черга сторінок деталей для Блоків 3 (чип
    «Приватна особа»/«Бізнес», «Тип угоди», непрозорий id профілю) і 4 («Назва ЖК») —
    один рендер на ключ за ніч, актуальні без доказів, приватні першими;
  * lun.ua і flombu.com — спільний прохід стрічки (night/feed.py) замість дозбору identity;
  * rieltor.ua — GET перевірки Блоку 1 замість HEAD (liveness.capture.body_hosts);
  * dom.ria.com — гачок стану сторінки GET перевірки (E8), як і досі.

Запис — ЛИШЕ туди, де порожньо (`fill_row`): identity — якщо NULL, place_raw і
seller_evidence — нові ключі, seller_profile — якщо NULL. Статус, ціна, last_seen і
решта полів не змінюються ніколи. Мітка вкладки (seller_evidence.olx_tab) — лише після
звірки членства з чипом сторінок деталей (`apply_tab_labels`): зламаний параметр
вкладок інакше записав би хибну мітку назавжди.

Тут також покриття доказами для /api/status/night і щоденного зведення.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from .. import ops
from ..models import Listing
from ..seller import evidence as sev

log = logging.getLogger(__name__)

OLX_TABS_STATE = "olx:tabs"
OLX_FAILED_STATE = "olx:detail_failed"
OLX_RENDERED_STATE = "olx:rendered_tonight"
# Стільки помилок рендера поспіль (код 0: мережа, браузер) — рендери стоять до кінця
# вікна (не блокування сайту: тривоги немає, у звіті — «render_errors»).
MAX_RENDER_ERRORS = 5


def utcnow() -> datetime:
    from ..liveness.queue import _now

    return _now()


# --- Стан (ops.night_state) -------------------------------------------------------------------


def state_get(name: str) -> dict:
    ops.init_ops()
    with ops.ops_session() as s:
        row = s.get(ops.NightState, name)
        if row is None or not row.value:
            return {}
        try:
            value = json.loads(row.value)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


def state_put(name: str, value: dict) -> None:
    ops.init_ops()
    with ops.ops_session() as s:
        row = s.get(ops.NightState, name)
        if row is None:
            row = ops.NightState(name=name)
            s.add(row)
        row.value = json.dumps(value, ensure_ascii=False, default=str)
        row.updated_at = ops._now()


# --- Запис «лише туди, де порожньо» -----------------------------------------------------------


def fill_row(row: Listing, got: dict, now: datetime) -> list[str]:
    """Доказ у рядок: identity — якщо NULL; place_raw, seller_evidence — нові ключі;
    seller_profile — якщо NULL. Повертає змінені поля (решту рядка не чіпаємо)."""
    changed = []
    ident = got.get("identity")
    if isinstance(ident, dict) and ident and row.identity is None:
        row.identity = ident
        changed.append("identity")
    for name in ("place_raw", "seller_evidence"):
        new = got.get(name)
        if not isinstance(new, dict) or not new:
            continue
        old = getattr(row, name) or {}
        merged = sev.merge_new(old, new) or {}
        if len(merged) > len(old):
            setattr(row, name, merged)
            changed.append(name)
            if name == "seller_evidence":
                row.seller_evidence_at = now
    prof = got.get("seller_profile")
    if prof and row.seller_profile is None:
        row.seller_profile = str(prof)[:96]
        changed.append("seller_profile")
    return changed


def _with_retry(fn):
    """«database is locked» (інший процес довше за busy_timeout тримав запис) — повтор,
    як у пакетах застосування ночі (liveness.apply, рецензія E9, D53)."""
    from ..liveness.apply import LOCKED_RETRIES, LOCKED_RETRY_WAIT_S

    attempt = 0
    while True:
        try:
            return fn()
        except OperationalError as e:
            if "locked" in str(e) and attempt < LOCKED_RETRIES:
                attempt += 1
                time.sleep(LOCKED_RETRY_WAIT_S)
                continue
            raise


def write_feed(scope, source: str, recs: list[dict], now: datetime) -> dict:
    """Записи сторінки стрічки → наявні рядки (джерело + зовнішній id); нових рядків не
    створює. Одна коротка транзакція на сторінку."""
    by_ext = {str(r["external_id"]): r for r in recs if r.get("external_id")}
    if not by_ext:
        return {"matched": 0}

    def go() -> dict:
        counts = Counter()
        with scope() as s:
            rows = s.scalars(select(Listing).where(Listing.source == source,
                                                   Listing.external_id.in_(list(by_ext))))
            for row in rows:
                counts["matched"] += 1
                for name in fill_row(row, by_ext[str(row.external_id)], now):
                    counts[name] += 1
        return dict(counts)

    return _with_retry(go)


def write_ids(scope, ids: list[int], got: dict, now: datetime) -> dict:
    """Доказ одного ключа → усі його рядки (копії LUN теж), лише туди, де порожньо."""
    def go() -> dict:
        counts = Counter()
        with scope() as s:
            for row in s.scalars(select(Listing).where(Listing.id.in_(list(ids)))):
                counts["rows"] += 1
                for name in fill_row(row, got, now):
                    counts[name] += 1
        return dict(counts)

    return _with_retry(go)


# --- OLX: план --------------------------------------------------------------------------------


def _iso_dt(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def remember_rendered(night_start: str | None, keys) -> None:
    """Ключі, чию сторінку деталей рендерили цієї ночі, — у ops.night_state (рецензія E11,
    08.10): рендер не ставить last_attempt рядкам (це поле Блоку 1), тож без цього вікно 2
    могло б питати той самий ключ удруге (HEAD контрольних/прострочених чи рендер). Стан
    однієї ночі: інша `night_start` — список починається заново."""
    if not night_start or not keys:
        return
    st = state_get(OLX_RENDERED_STATE)
    old = set(st.get("keys") or ()) if st.get("night_start") == night_start else set()
    state_put(OLX_RENDERED_STATE, {"night_start": night_start,
                                   "keys": sorted(old | set(keys))})


def rendered_tonight(night_start: datetime | None) -> set[str]:
    """Ключі, сторінку яких рендерили від `night_start` (старт першого вікна цієї ночі)."""
    if night_start is None:
        return set()
    try:
        st = state_get(OLX_RENDERED_STATE)
    except Exception:                                  # noqa: BLE001 — лише уточнення плану
        log.warning("ніч: стан рендерів OLX не читається", exc_info=True)
        return set()
    if st.get("night_start") != night_start.isoformat(timespec="seconds"):
        return set()
    return set(st.get("keys") or ())


def failed_keys(rcfg, now: datetime) -> dict[str, str]:
    """Ключі, рендер яких не вдався не давніше за failed_retry_hours (ключ → коли)."""
    cut = now - timedelta(hours=rcfg.failed_retry_hours)
    return {k: v for k, v in state_get(OLX_FAILED_STATE).items()
            if (t := _iso_dt(v)) is not None and t >= cut}


def private_members(scfg, now: datetime) -> set[str]:
    cut = now - timedelta(days=scfg.olx_tabs.member_max_age_days)
    ops.init_ops()
    with ops.ops_session() as s:
        return set(s.scalars(select(ops.OlxTabSeen.site_key).where(
            ops.OlxTabSeen.tab == "private", ops.OlxTabSeen.last_seen_at >= cut)))


def olx_detail_queue(session, lcfg, ncfg, scfg, *, now: datetime, night_start: datetime | None,
                     exclude=frozenset(), host: str = "olx.ua") -> tuple[list[dict], dict]:
    """Черга сторінок деталей OLX: ключі «olx:…», чий хоч один АКТУАЛЬНИЙ рядок без
    seller_evidence.<olx.checked_key>. Не беремо: ключі цього вікна в Блоці 1 (`exclude`),
    ключі, які вже пробували цієї ночі (last_attempt ≥ night_start), і невдалі за
    failed_retry_hours. Порядок: члени вкладки «Приватні», власні OLX (не копії LUN),
    свіжіше бачені. Повертає (черга ≤ detail_per_window, лічильники)."""
    from ..liveness import policy as pol

    ck = scfg.olx.checked_key
    rows = session.execute(select(
        Listing.id, Listing.site_key, Listing.source, Listing.original_url, Listing.probe_url,
        Listing.is_active, Listing.last_seen, Listing.last_attempt,
        func.json_extract(Listing.seller_evidence, f"$.{ck}")).where(
        Listing.site_key.like("olx:%"))).all()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r[1]].append(r)
    failed = failed_keys(ncfg.olx_render, now)
    private = private_members(scfg, now)
    rendered = rendered_tonight(night_start)
    counts = Counter()
    cand = []
    for key, rs in groups.items():
        if not any(r[5] and r[8] is None for r in rs):
            continue
        counts["missing"] += 1
        if key in exclude:
            counts["in_block1"] += 1
            continue
        if night_start is not None and (key in rendered or any(
                r[7] is not None and r[7] >= night_start for r in rs)):
            counts["tried_tonight"] += 1
            continue
        if key in failed:
            counts["failed_recently"] += 1
            continue
        fresh = max(rs, key=lambda r: (r[4] is not None, r[6] or datetime.min, r[0]))
        url = pol.probe_url(lcfg, host, fresh[4] or fresh[3])
        seen = max((r[6] for r in rs if r[6] is not None), default=datetime.min)
        cand.append(((0 if key in private else 1, 0 if any(r[2] == "olx" for r in rs) else 1,
                      -seen.timestamp() if seen != datetime.min else 0, key),
                     {"key": key, "url": url, "ids": sorted(r[0] for r in rs),
                      "private": key in private}))
    cand.sort(key=lambda x: x[0])
    counts["queued"] = min(len(cand), ncfg.olx_render.detail_per_window)
    counts["eligible"] = len(cand)
    counts["private"] = sum(1 for _, e in cand if e["private"])
    return [e for _, e in cand[:ncfg.olx_render.detail_per_window]], dict(counts)


def partitions(scfg) -> list[list]:
    t = scfg.olx_tabs
    return [[room, int(band[0]), int(band[1])] for room in t.rooms for band in t.price_bands_usd]


def _part_id(part) -> str:
    return f"{part[0]}:{part[1]}-{part[2]}"


def olx_tabs_plan(ncfg, scfg, *, now: datetime, night_start: datetime | None,
                  active_keys: int = 0) -> dict:
    """Що з вкладок робити цього вікна: «Приватні» — раз за ніч; «Бізнес» — нове коло
    раз на business_tab_every_days, незавершене коло — продовжити з невиконаних зрізів."""
    rcfg = ncfg.olx_render
    st = state_get(OLX_TABS_STATE)
    private_at = _iso_dt(st.get("private_at"))
    private_due = rcfg.private_tab_max_pages > 0 and not (
        private_at is not None and night_start is not None and private_at >= night_start)
    every = timedelta(days=rcfg.business_tab_every_days)
    sweep = st.get("business") or {}
    done_at, started_at = _iso_dt(sweep.get("finished_at")), _iso_dt(sweep.get("started_at"))
    parts = partitions(scfg)
    ignored_at = _iso_dt(sweep.get("filter_ignored_at"))
    if done_at is not None and now - done_at < every:
        remaining: list = []
    elif ignored_at is not None and now - ignored_at < every:
        # Фільтр зрізів не діяв (той самий перший екран) — нове коло лише через тиждень,
        # а не щовікна по два рендери-повтори.
        remaining = []
    elif started_at is not None and done_at is None and now - started_at < every:
        done = set(sweep.get("done") or ())
        remaining = [p for p in parts if _part_id(p) not in done]
    else:
        remaining = parts
    est_private = rcfg.private_tab_max_pages if private_due else 0
    est_business = 0
    if remaining:
        est_business = min(len(remaining) * rcfg.business_tab_max_pages,
                           math.ceil(active_keys * len(remaining) / max(1, len(parts))
                                     / rcfg.cards_per_page) + len(remaining))
    return {"private": {"due": private_due, "max_pages": rcfg.private_tab_max_pages},
            "business": {"due": bool(remaining), "partitions": remaining,
                         "max_pages": rcfg.business_tab_max_pages,
                         "new_sweep": remaining == parts},
            "est_renders": est_private + est_business}


def tab_url(scfg, tab: str, page: int, part=None) -> str:
    from ..sources.olx import IN_USD, LIST_URL

    t = scfg.olx_tabs
    params: list = [(t.param, t.values[tab])]
    if part is not None:
        room, lo, hi = part
        params.append((t.rooms_param, room))
        if lo:
            params.append((t.price_from_param, lo))
        if hi:
            params.append((t.price_to_param, hi))
    if page > 1:
        params.append(("page", page))
    return f"{LIST_URL}?{urlencode(params)}&{IN_USD}"


def plan_olx(session, lcfg, ncfg, scfg, *, now: datetime, night_start: datetime | None,
             exclude=frozenset()) -> dict:
    """Робота смуги olx.ua після Блоку 1 (для плану ночі й `--dry-run`)."""
    order = ncfg.jobs.order
    out: dict = {}
    active = session.scalar(select(func.count(func.distinct(Listing.site_key))).where(
        Listing.site_key.like("olx:%"), Listing.is_active.is_(True))) or 0
    if "olx_tabs" in order:
        out["olx_tabs"] = olx_tabs_plan(ncfg, scfg, now=now, night_start=night_start,
                                        active_keys=active)
    if "olx_detail" in order:
        queue, counts = olx_detail_queue(session, lcfg, ncfg, scfg, now=now,
                                         night_start=night_start, exclude=exclude)
        out["olx_detail"] = queue
        out["detail_counts"] = counts
    tabs = (out.get("olx_tabs") or {}).get("est_renders", 0)
    out["est_renders"] = min(ncfg.olx_render.max_per_window,
                             tabs + len(out.get("olx_detail") or ()))
    return out


# --- OLX: смуга (процес `cli.py night lane`) ---------------------------------------------------


def is_captcha(html: str | None, rcfg) -> bool:
    """Сторінка 200 без змісту (ні карток видачі, ні параметрів оголошення), але з ознакою
    капчі/відмови — рендер не вдався, а не «оголошення без чипа» (лічиться окремо від
    блокувань: captcha_stop_after поспіль — рендери стоять до кінця вікна)."""
    if not html:
        return False
    if ('data-cy="l-card"' in html or "ad-parameters-container" in html
            or 'data-testid="offer_title"' in html):
        return False
    return any(m in html for m in rcfg.captcha_markers)


def record_tab(keys: list[str], tab: str, now: datetime) -> dict:
    """Членство у вкладці → ops.olx_tab_seen (робочий запис; доказ — після звірки)."""
    if not keys:
        return {"new": 0, "conflicts": 0}
    out = Counter()
    ops.init_ops()
    with ops.ops_session() as s:
        for key in keys:
            row = s.get(ops.OlxTabSeen, key)
            if row is None:
                s.add(ops.OlxTabSeen(site_key=key, tab=tab, first_seen_at=now,
                                     last_seen_at=now, conflicts=0))
                out["new"] += 1
                continue
            if row.tab != tab:
                row.conflicts = (row.conflicts or 0) + 1
                row.tab = tab
                out["conflicts"] += 1
            row.last_seen_at = now
    return {"new": out["new"], "conflicts": out["conflicts"]}


class _Stopped(Exception):
    """Рендери цього вікна скінчились (стеля, дедлайн, пам'ять, блокування, капча,
    помилки)."""


class OlxJobs:
    """Вкладки й сторінки деталей OLX у смузі olx.ua — після перевірок Блоку 1.

    `gate` — ворота смуги (night.lane.EvidenceGate): пауза старт-до-старту з усіма
    запитами смуги, дедлайн із запасом на рендер (і на перезапуск браузера), 401/403/429
    — у ті самі «5 поспіль» і частку блокувань (смуга стоїть до кінця ночі → тривога
    сторожа night-blocked). Капча — окремо: captcha_stop_after поспіль — рендери стоять
    до кінця вікна (попередження night-captcha), Блок 1 і наступні вікна — як були."""

    def __init__(self, spec: dict, gate, *, renderer, scope, scfg, ncfg, now_fn=None,
                 mem_fn=None) -> None:
        from .render import render_headroom_mb

        self.spec = spec
        self.gate = gate
        self.renderer = renderer
        self.scope = scope
        self.scfg = scfg
        self.rcfg = ncfg.olx_render
        self.now_fn = now_fn or utcnow
        self.mem_fn = mem_fn or render_headroom_mb
        self.renders = 0
        self.errors_in_row = 0
        self.captcha_in_row = 0
        self.private_now: set[str] = set()
        # Ключі, сторінку яких цього вікна справді рендерили (запит пішов) — у стан ночі:
        # вікно 2 не питає їх ні рендером, ні HEAD Блоку 1 (один запит на ключ за ніч).
        self.rendered_keys: set[str] = set()
        self.report: dict = {"renders": 0, "blocked": 0, "stopped": None,
                             "tabs": {}, "detail": {}}

    # --- один рендер ---------------------------------------------------------------

    def _margin(self) -> float:
        """Запас до stop_requests, за який рендер уже не починаємо: deadline_margin плюс
        (пере)запуск браузера, якщо він буде перед цим рендером (рецензія E11, 08.10:
        запуск під сторожем render_timeout_seconds не входив у запас, і рендер із
        перезапуском міг перейти stop_requests + kill_grace)."""
        allowance = getattr(self.renderer, "launch_allowance", None)
        extra = float(allowance()) if callable(allowance) else 0.0
        return self.rcfg.deadline_margin_seconds + extra

    def _render(self, url: str, key: str | None = None):
        from ..fetcher import BLOCKING_CODES
        from ..identity_backfill import Stop

        if self.renders >= self.rcfg.max_per_window:
            raise _Stopped("cap")
        margin = self._margin()
        if self.gate.stopped(margin=margin):
            raise _Stopped(self.gate.why())
        mem = self.mem_fn()
        if mem is not None and mem < self.rcfg.min_mem_available_mb:
            self.report["mem_available_mb"] = mem
            raise _Stopped("memory")
        try:
            self.gate.wait(margin=margin)
        except Stop:
            raise _Stopped(self.gate.why()) from None
        res = self.renderer.render(url)
        self.renders += 1
        self.report["renders"] += 1
        if key is not None:
            self.rendered_keys.add(key)
        captcha = res.code == 200 and is_captcha(res.html, self.rcfg)
        if res.code in BLOCKING_CODES:
            self.report["blocked"] += 1
        # Капча — окремо від блокувань (рецензія E11, 08.10): у «5 поспіль» і частку смуги
        # не йде (інакше друга така ніч поставила б на утримання й перевірки Блоку 1
        # olx.ua), а зупиняє лише рендери цього вікна — captcha_stop_after поспіль.
        self.gate.observe(res.code)
        if self.gate.blocked():
            raise _Stopped("blocks")
        if captcha:
            self.report["captcha"] = self.report.get("captcha", 0) + 1
            self.captcha_in_row += 1
            if self.captcha_in_row >= self.rcfg.captcha_stop_after:
                raise _Stopped("captcha")
        else:
            self.captcha_in_row = 0
        self.errors_in_row = self.errors_in_row + 1 if res.code == 0 else 0
        if self.errors_in_row >= MAX_RENDER_ERRORS:
            raise _Stopped("render_errors")
        return res, captcha

    # --- вкладки -------------------------------------------------------------------

    def _tab_pages(self, tab: str, max_pages: int, part=None) -> dict:
        from bs4 import BeautifulSoup

        out = {"pages": 0, "keys": 0, "first": []}
        for page in range(1, max_pages + 1):
            res, captcha = self._render(tab_url(self.scfg, tab, page, part))
            out["pages"] += 1
            if res.code != 200 or captcha:
                out["error"] = f"код {res.code}" + (" (капча)" if captcha else "")
                break
            info = sev.olx_tab_page(BeautifulSoup(res.html, "lxml"), tab, self.scfg)
            if not info["active"]:
                out["param_failed"] = info["active_labels"][:3]
                break
            if page == 1:
                out["first"] = info["keys"]
            got = record_tab(info["keys"], tab, self.now_fn())
            out["keys"] += len(info["keys"])
            out["conflicts"] = out.get("conflicts", 0) + got["conflicts"]
            if tab == "private":
                self.private_now.update(info["keys"])
            if not info["next"] or not info["keys"]:
                break
        else:
            # Стеля сторінок вкладки, а наступна сторінка ще є — членство неповне; не тихо,
            # а в щоденне зведення (власник 09.10).
            out["cap_hit"] = True
            from .. import ops
            ops.record_list_cap("olx", "olx_tab", max_pages,
                                f"вкладка «{tab}»{' ' + _part_id(part) if part else ''}: "
                                f"наступна сторінка ще є")
        return out

    def _tabs(self, plan: dict) -> None:
        rep = self.report["tabs"]
        st = state_get(OLX_TABS_STATE)
        if (plan.get("private") or {}).get("due"):
            got = self._tab_pages("private", plan["private"]["max_pages"])
            rep["private"] = {k: v for k, v in got.items() if k != "first"}
            if got.get("param_failed"):
                st["param_failed_at"] = self.now_fn().isoformat(timespec="seconds")
                state_put(OLX_TABS_STATE, st)
                rep["param_failed"] = True
                return
            if "error" not in got:
                st["private_at"] = self.now_fn().isoformat(timespec="seconds")
                st["private_keys"] = got["keys"]
                state_put(OLX_TABS_STATE, st)
        bplan = plan.get("business") or {}
        if not bplan.get("due"):
            return
        sweep = st.get("business") or {}
        if bplan.get("new_sweep") or not sweep.get("started_at") or sweep.get("finished_at"):
            sweep = {"started_at": self.now_fn().isoformat(timespec="seconds"), "done": [],
                     "keys": 0}
        brep = rep.setdefault("business", {"partitions": 0, "pages": 0, "keys": 0})
        prev_first: list = []
        for part in bplan.get("partitions") or ():
            got = self._tab_pages("business", bplan["max_pages"], part)
            brep["pages"] += got["pages"]
            brep["keys"] += got["keys"]
            if got.get("param_failed"):
                rep["param_failed"] = True
                st["param_failed_at"] = self.now_fn().isoformat(timespec="seconds")
                break
            if got.get("error"):
                brep["error"] = got["error"]
                break
            if got["first"] and got["first"] == prev_first:
                # Зріз дав ті самі картки, що й попередній: фільтр кімнат/ціни не діє —
                # коло «Бізнес» зупиняємо (не палимо рендери на повтори).
                brep["filter_ignored"] = _part_id(part)
                sweep["filter_ignored_at"] = self.now_fn().isoformat(timespec="seconds")
                break
            prev_first = got["first"]
            brep["partitions"] += 1
            sweep["done"] = sorted({*sweep.get("done", []), _part_id(part)})
            sweep["keys"] = int(sweep.get("keys") or 0) + got["keys"]
            st["business"] = sweep
            state_put(OLX_TABS_STATE, st)
        else:
            sweep["finished_at"] = self.now_fn().isoformat(timespec="seconds")
        st["business"] = sweep
        state_put(OLX_TABS_STATE, st)

    # --- сторінки деталей --------------------------------------------------------------

    def _detail(self, entries: list[dict]) -> None:
        from .. import links
        from ..sources import olx

        rep = self.report["detail"]
        for k in ("rendered", "written", "no_content", "failed", "id_mismatch"):
            rep.setdefault(k, 0)
        fields = Counter()
        codes = Counter()
        failed = state_get(OLX_FAILED_STATE)
        # Приватних, знайдених у вкладці цієї ночі, — першими (черга будувалась до вкладок).
        entries = sorted(entries, key=lambda e: 0 if e["key"] in self.private_now else 1)
        try:
            for entry in entries:
                res, captcha = self._render(entry["url"], entry["key"])
                rep["rendered"] += 1
                ok = False
                if res.code == 200 and not captcha:
                    final = None
                    if res.final_url:
                        try:
                            final = links.site_key(res.final_url)
                        except Exception:              # noqa: BLE001
                            final = None
                    if final not in (None, entry["key"]):
                        rep["id_mismatch"] += 1
                    else:
                        got = olx.page_evidence(res.html or "")
                        if got:
                            fields.update(write_ids(self.scope, entry["ids"], got, self.now_fn()))
                            rep["written"] += 1
                            ok = True
                        else:
                            rep["no_content"] += 1
                else:
                    codes[str(res.code)] += 1
                    rep["failed"] += 1
                if not ok:
                    failed[entry["key"]] = self.now_fn().isoformat(timespec="seconds")
        finally:
            cut = self.now_fn() - timedelta(hours=self.rcfg.failed_retry_hours)
            state_put(OLX_FAILED_STATE, {k: v for k, v in failed.items()
                                         if (t := _iso_dt(v)) is not None and t >= cut})
            remember_rendered(self.spec.get("night_start"), self.rendered_keys)
            rep["fields"] = dict(fields)
            if codes:
                rep["codes"] = dict(codes)
            rep["left_in_queue"] = len(entries) - rep["rendered"]

    def run(self) -> dict:
        try:
            if self.spec.get("olx_tabs"):
                self._tabs(self.spec["olx_tabs"])
            if self.spec.get("olx_detail"):
                self._detail(self.spec["olx_detail"])
        except _Stopped as e:
            self.report["stopped"] = str(e)
        finally:
            try:
                self.renderer.close()
            except Exception:                          # noqa: BLE001
                log.exception("нічний браузер не закрився")
        log.info("рендери OLX: %d (блокувань %d), вкладки %s, деталі %s%s", self.renders,
                 self.report["blocked"], self.report["tabs"], self.report["detail"],
                 f"; зупинка: {self.report['stopped']}" if self.report["stopped"] else "")
        return self.report


def run_olx_jobs(spec: dict, gate, *, renderer_factory, scope=None, scfg=None, ncfg=None,
                 now_fn=None, mem_fn=None) -> dict:
    """Точка входу смуги olx.ua (night.lane): справжній браузер — renderer_factory()."""
    from .. import configfiles

    ncfg = ncfg or configfiles.load("night")
    scfg = scfg or configfiles.load("seller")
    if scope is None:
        from ..db import session_scope as scope
    jobs = OlxJobs(spec, gate, renderer=renderer_factory(ncfg.olx_render), scope=scope,
                   scfg=scfg, ncfg=ncfg, now_fn=now_fn, mem_fn=mem_fn)
    return jobs.run()


# --- Мітки вкладок (диригент, на старті вікна) --------------------------------------------------


def tab_labels(session, scfg, *, now: datetime) -> dict:
    """Звірка членства у вкладках із чипом сторінок деталей (лише читання).

    Ключі: свіже членство (member_max_age_days), без конфліктів вкладок. Збіг — серед
    ключів, де відомі обидва. Мітку писати можна, лише якщо n ≥ chip_min_n і частка ≥
    chip_min_agreement; `pending` — рядки членів без мітки."""
    t = scfg.olx_tabs
    cut = now - timedelta(days=t.member_max_age_days)
    ops.init_ops()
    with ops.ops_session() as s:
        members = {k: tab for k, tab in s.execute(select(
            ops.OlxTabSeen.site_key, ops.OlxTabSeen.tab).where(
            ops.OlxTabSeen.last_seen_at >= cut, ops.OlxTabSeen.conflicts == 0))}
    out = {"members": len(members), "n": 0, "agree": 0, "pending_rows": 0,
           "status": "no_members", "rows": {}}
    if not members:
        return out
    chip, mark = scfg.olx.chip_key, t.tab_key
    keys = sorted(members)
    chips: dict[str, set] = defaultdict(set)
    pending: dict[str, list[int]] = defaultdict(list)
    for i in range(0, len(keys), 500):
        for lid, key, c, m in session.execute(select(
                Listing.id, Listing.site_key,
                func.json_extract(Listing.seller_evidence, f"$.{chip}"),
                func.json_extract(Listing.seller_evidence, f"$.{mark}")).where(
                Listing.site_key.in_(keys[i:i + 500]))):
            if c is not None:
                chips[key].add(c)
            if m is None:
                pending[key].append(lid)
    known = {k: next(iter(v)) for k, v in chips.items() if len(v) == 1}
    out["n"] = len(known)
    out["agree"] = sum(1 for k, c in known.items() if members[k] == c)
    out["pending_rows"] = sum(len(v) for v in pending.values())
    share = out["agree"] / out["n"] if out["n"] else 0.0
    out["share"] = round(share, 4)
    if out["n"] < t.chip_min_n:
        out["status"] = "not_enough"
    elif share < t.chip_min_agreement:
        out["status"] = "disagree"
    else:
        out["status"] = "calibrated"
    out["rows"] = {k: v for k, v in pending.items()}
    out["members_tab"] = members
    return out


def apply_tab_labels(scope, scfg, *, now: datetime) -> dict:
    """Мітки вкладок у seller_evidence (лише туди, де порожньо) — якщо звірка пройшла."""
    with scope() as s:
        plan = tab_labels(s, scfg, now=now)
    rows, members = plan.pop("rows"), plan.pop("members_tab", {})
    plan["applied"] = 0
    if plan["status"] != "calibrated" or not rows:
        return plan
    t = scfg.olx_tabs
    day = now.date().isoformat()
    batch: list[tuple[list[int], dict]] = []
    for key, ids in rows.items():
        batch.append((ids, {"seller_evidence": {t.tab_key: members[key], t.tab_at_key: day}}))
    n = 0
    for i in range(0, len(batch), 200):
        def go(part=batch[i:i + 200]) -> int:
            done = 0
            with scope() as s:
                for ids, got in part:
                    for row in s.scalars(select(Listing).where(Listing.id.in_(ids))):
                        if fill_row(row, got, now):
                            done += 1
            return done
        n += _with_retry(go)
    plan["applied"] = n
    return plan


# --- Покриття доказами ---------------------------------------------------------------------------


def coverage(session, scfg) -> dict:
    """Актуальні рядки за групами «джерело>сімейство»: усього, з доказами (будь-який ключ
    групи) і за кожним ключем; Блок 4 — позначки «джерело показувало поле ЖК»."""
    groups = scfg.coverage.groups
    keys = sorted({k for ks in groups.values() for k in ks})
    place_keys = ("olx_checked_at", "rieltor_checked_at", "lun_geo_checked_at")
    cols = [func.json_extract(Listing.seller_evidence, f"$.{k}") for k in keys]
    pcols = [func.json_extract(Listing.place_raw, f"$.{k}") for k in place_keys]
    out: dict = {}
    for source, site_key, *vals in session.execute(
            select(Listing.source, Listing.site_key, *cols, *pcols)
            .where(Listing.is_active.is_(True))):
        fam = (site_key or "").split(":", 1)[0] if site_key and ":" in site_key else "?"
        group = f"{source}>{fam}"
        if group not in groups:
            continue
        g = out.setdefault(group, {"label": scfg.coverage.labels[group], "total": 0,
                                   "with": 0, "keys": {k: 0 for k in groups[group]},
                                   "place": {}})
        g["total"] += 1
        have = {k for k, v in zip(keys, vals[:len(keys)]) if v is not None}
        if have & set(groups[group]):
            g["with"] += 1
        for k in groups[group]:
            if k in have:
                g["keys"][k] += 1
        for k, v in zip(place_keys, vals[len(keys):]):
            if v is not None:
                g["place"][k] = g["place"].get(k, 0) + 1
    return {g: out[g] for g in groups if g in out}


# --- План ночі: що дозбирати в кожній смузі (диригент і `--dry-run`) -----------------------------


def attach(session, lcfg, ncfg, scfg, plan, *, now: datetime,
           night_start: datetime | None) -> dict:
    """Дописати в план ночі (HostPlan.evidence) роботу доказів Блоків 3/4 ПІСЛЯ Блоку 1:
    рендери OLX (spec для смуги), прохід стрічки LUN/flombu (лише числа — рішення
    приймає сама смуга за тим самим `feed.due`), GET rieltor (уже в плані, body_gets).
    Повертає зведення: чи писатиме вікно докази (правило бекапу на старті), мітки вкладок."""
    from ..configfiles import OLX_RENDER_HOST
    from . import feed

    order = ncfg.jobs.order
    out = {"writes": 0, "olx_renders": 0, "olx_detail": 0, "feed": {}, "body_gets": 0,
           "tab_labels": None}
    hp = plan.hosts.get(OLX_RENDER_HOST)
    if hp is not None and hp.skipped is None and {"olx_tabs", "olx_detail"} & set(order):
        info = plan_olx(session, lcfg, ncfg, scfg, now=now, night_start=night_start,
                        exclude={i.key for i in hp.items})
        tabs = info.get("olx_tabs") or {}
        due_tabs = {k: v for k, v in tabs.items() if isinstance(v, dict) and v.get("due")}
        spec = {}
        if due_tabs:
            spec["olx_tabs"] = due_tabs
        if info.get("olx_detail"):
            spec["olx_detail"] = info["olx_detail"]
        if spec and night_start is not None:
            # Смуга пише відрендерені ключі в стан ЦІЄЇ ночі (remember_rendered).
            spec["night_start"] = night_start.isoformat(timespec="seconds")
        hp.evidence = {"est_renders": info["est_renders"] if spec else 0,
                       "detail": info.get("detail_counts") or {},
                       "tabs": {k: {"due": v.get("due"),
                                    "partitions": len(v.get("partitions") or ()),
                                    "max_pages": v.get("max_pages")}
                                for k, v in tabs.items() if isinstance(v, dict)},
                       "tabs_est_renders": tabs.get("est_renders", 0)}
        if spec:
            hp.evidence["spec"] = spec
        out["olx_renders"] = hp.evidence["est_renders"]
        out["olx_detail"] = len(spec.get("olx_detail") or ())
    if "identity" in order:
        for host, source in ncfg.jobs.identity_sources.items():
            p = plan.hosts.get(host)
            if source not in feed.SOURCE_HOSTS or p is None or p.skipped is not None:
                continue
            d = feed.due(session, source, ncfg, scfg, now=now)
            p.evidence = {"feed": d}
            out["feed"][source] = d
    out["body_gets"] = sum(p.body_gets for p in plan.hosts.values() if p.skipped is None)
    if "olx_tabs" in order:
        try:
            labels = tab_labels(session, scfg, now=now)
            labels.pop("rows", None)
            labels.pop("members_tab", None)
            out["tab_labels"] = labels
        except Exception as e:                       # noqa: BLE001 — лише прогноз
            out["tab_labels"] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:200]}
    labels = out["tab_labels"] or {}
    pending = labels.get("status") == "calibrated" and labels.get("pending_rows", 0) > 0
    # Що допише в realty.db (вкладки пишуть лише ops.olx_tab_seen — не рахуються).
    out["writes"] = (out["olx_detail"] + out["body_gets"]
                     + sum(1 for d in out["feed"].values() if d.get("due")) + int(pending))
    return out
