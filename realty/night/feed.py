"""Спільний нічний прохід стрічки LUN і flombu — робота identity їхніх смуг (E11, D60).

Інтеграція, конфлікт 4: замість трьох проходів стрічки LUN (дозбір identity, фаза A
Блоку 4, N1 Блоку 3) і двох flombu — ОДИН за ніч, що захоплює одразу identity, докази
місця (place_raw: geoEntities LUN, населений пункт flombu) і докази продавця
(seller_evidence: isOwner, contactType, комісія LUN; ownerType flombu).

Це НЕ збір (до E11 смуга запускала `cli.py scrape --no-detail`, звичайний збір
стрічки): нових оголошень не додає, ціну, події ціни, last_seen і стан актуальності не
чіпає. Пише лише в рядки, які вже є (джерело + зовнішній id), і лише туди, де порожньо:
identity — якщо NULL, place_raw і seller_evidence — нові ключі, seller_profile — якщо
NULL (evidence.fill_row). Після КОЖНОЇ сторінки — короткий запис і стан (з якої сторінки
продовжити), тож дедлайн смуги нічого не губить, а наступне вікно продовжує з місця
зупинки.

Коли потрібен (config/night.toml [feed]): актуальних рядків джерела без доказів ≥
min_missing_active; після завершеного проходу — не раніше cooldown_hours, а якщо він
закрив менше за low_gain — через cooldown_low_gain_days (як дозбір identity, D42).
Запити — через ворота смуги (night.lane.IdentityGate): той самий темп старт-до-старту,
дедлайн і лічильники блокувань, що й у перевірок смуги.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy import func, or_, select

from ..models import Listing

log = logging.getLogger(__name__)

SOURCE_HOSTS = {"lun": "lun.ua", "flombu": "flombu.com"}


def state_name(source: str) -> str:
    return f"feed:{source}"


def _missing_cond(source: str, scfg):
    """Умова «рядку бракує доказів стрічки» для джерела."""
    conds = [Listing.identity.is_(None),
             func.json_extract(Listing.seller_evidence,
                               f"$.{getattr(scfg, source).checked_key}").is_(None)]
    if source == "lun":
        # Позначка проходу стрічки з geoEntities (Блок 4, E10): лише з нею порожній ЖК
        # означає «LUN ЖК не показав».
        conds.append(func.json_extract(Listing.place_raw, "$.lun_geo_checked_at").is_(None))
    return or_(*conds)


def need(session, source: str, scfg) -> int:
    """Скільки АКТУАЛЬНИХ рядків джерела ще без доказів стрічки."""
    return session.scalar(select(func.count()).select_from(Listing).where(
        Listing.source == source, Listing.is_active.is_(True),
        _missing_cond(source, scfg))) or 0


def _age_h(now: datetime, iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return None


def due(session, source: str, ncfg, scfg, *, now: datetime, state: dict | None = None) -> dict:
    """Чи потрібен прохід цього вікна: {"due", "why", "need", "resume_page", "pages_est"}.

    `now` — UTC без зони (як і часи в стані). Лише читання."""
    from . import evidence

    feed = ncfg.feed
    st = state if state is not None else evidence.state_get(state_name(source))
    n = need(session, source, scfg)
    max_pages = feed.max_pages[source]
    out = {"due": False, "why": "", "need": n, "resume_page": 1, "pages_est": 0,
           "max_pages": max_pages}
    started_h = _age_h(now, st.get("started_at"))
    if (st.get("started_at") and not st.get("finished_at") and started_h is not None
            and started_h <= feed.resume_max_age_hours and int(st.get("next_page") or 1) > 1):
        page = min(int(st["next_page"]), max_pages + 1)
        if page <= max_pages:
            out.update(due=True, why=f"продовжую незавершений прохід зі сторінки {page}",
                       resume_page=page,
                       pages_est=max(0, int(st.get("pages_total") or max_pages) - page + 1))
            return out
    done_h = _age_h(now, st.get("finished_at"))
    if done_h is not None:
        low = int(st.get("gain") or 0) < feed.low_gain
        cooldown_h = feed.cooldown_low_gain_days * 24 if low else feed.cooldown_hours
        if done_h < cooldown_h:
            out["why"] = (f"попередній прохід {done_h:.0f} год тому закрив {st.get('gain', 0)} "
                          f"(наступний — через {cooldown_h:.0f} год)")
            return out
    if n < feed.min_missing_active:
        out["why"] = f"не потрібно: актуальних без доказів {n} < {feed.min_missing_active}"
        return out
    out.update(due=True, why=f"актуальних без доказів {n}",
               pages_est=int(st.get("pages_total") or max_pages))
    return out


def _source(source: str, fetcher):
    if source == "lun":
        from ..sources.lun import LunSource

        return LunSource(fetcher=fetcher, mode="full")
    from ..sources.flombu import FlombuSource

    return FlombuSource(fetcher=fetcher, mode="full")


def _page(src, source: str, page: int) -> tuple[list[dict], int, int | None]:
    """(записи, об'єктів на сторінці, сторінок за сайтом) — один запит."""
    if source == "lun":
        from ..sources import lun

        recs, found = src.parse_page(src.fetcher.get(lun.page_url(page)))
        return recs, found, None
    from ..sources import flombu

    return src.parse_page(src.fetcher.get_json(flombu.API, src._params(page)))


def run(source: str, stop_at: float, gate, *, ncfg=None, scfg=None, scope=None,
        now_fn=None, fetcher=None) -> dict:
    """Прохід стрічки джерела в смузі хоста до `stop_at` (epoch). Звіт — словник.

    `gate` — ворота смуги (night.lane.IdentityGate): пауза, дедлайн, блокування;
    `fetcher` — для тестів (інакше фетчер воріт без кешу)."""
    from .. import configfiles
    from ..fetcher import FetchError
    from ..identity_backfill import Stop
    from . import evidence

    ncfg = ncfg or configfiles.load("night")
    scfg = scfg or configfiles.load("seller")
    if scope is None:
        from ..db import session_scope as scope
    now_fn = now_fn or evidence.utcnow
    name = state_name(source)
    st = evidence.state_get(name)
    with scope() as s:
        d = due(s, source, ncfg, scfg, now=now_fn(), state=st)
    report = {"source": source, "status": "skipped", "why": d["why"], "need_before": d["need"],
              "pages": 0, "objects": 0, "records": 0, "matched": 0, "updated": {}}
    if not d["due"]:
        return report
    if gate is not None and gate.stopped():
        report["why"] = "смугу зупинено (дедлайн чи блокування)"
        return report
    page = d["resume_page"]
    if page == 1:
        st = {"started_at": now_fn().isoformat(timespec="seconds"), "next_page": 1,
              "need_before": d["need"], "pages_total": st.get("pages_total")}
    report.update(status="running", first_page=page, why=d["why"])
    if fetcher is None:
        pace = gate.pace if gate is not None else 1.0
        fetcher = (gate.fetcher(pace, source, use_cache=False) if gate is not None
                   else _plain_fetcher(pace, source))
    src = _source(source, fetcher)
    max_pages = ncfg.feed.max_pages[source]
    finished = False
    try:
        while page <= max_pages:
            if gate is not None and gate.stopped():
                report["status"] = "stopped"
                break
            try:
                recs, found, total = _page(src, source, page)
            except Stop:
                report["status"] = "stopped"
                break
            except FetchError as e:
                report.update(status="fetch_error", error=f"{type(e).__name__}: {str(e)[:160]}")
                log.warning("прохід стрічки %s: сторінка %d — %s", source, page, e)
                break
            report["pages"] += 1
            if found < 0:
                report.update(status="fetch_error", error=f"порожній payload на сторінці {page}")
                break
            report["objects"] += found
            report["records"] += len(recs)
            got = evidence.write_feed(scope, source, recs, now_fn())
            report["matched"] += got.pop("matched", 0)
            for k, v in got.items():
                report["updated"][k] = report["updated"].get(k, 0) + v
            st["next_page"] = page + 1
            if total is not None:
                st["pages_total"] = total
            evidence.state_put(name, st)
            if found == 0 or (total is not None and page >= total):
                finished = True
                break
            page += 1
        else:
            finished = True                           # стеля сторінок
    finally:
        if fetcher is not None and hasattr(fetcher, "close"):
            try:
                fetcher.close()
            except Exception:                         # noqa: BLE001
                pass
    with scope() as s:
        left = need(s, source, scfg)
    report["need_after"] = left
    if finished:
        gain = int(st.get("need_before") or d["need"]) - left
        st.update(finished_at=now_fn().isoformat(timespec="seconds"), gain=gain, left=left,
                  pages_total=st.get("pages_total") or (page if page <= max_pages else max_pages))
        evidence.state_put(name, st)
        report.update(status="finished", gain=gain)
    log.info("прохід стрічки %s: %s, сторінок %d, оновлено %s, без доказів %d → %d", source,
             report["status"], report["pages"], report["updated"], d["need"], left)
    return report


def _plain_fetcher(pace: float, label: str):
    from ..fetcher import Fetcher

    return Fetcher(delay=pace, use_cache=False, label=label)


def lane_job(source: str, stop_at: float, gate=None) -> dict:
    """Робота identity смуги lun.ua / flombu.com — звіт у форматі дозбору identity."""
    started = time.monotonic()
    rep = run(source, stop_at, gate)
    return {"done": {source: f"{rep['status']}: {rep.get('why') or ''}".strip()},
            "feed": rep, "left": {source: rep.get("need_after", rep.get("need_before"))},
            "errors": int(rep["status"] == "fetch_error"),
            "seconds": round(time.monotonic() - started, 1)}

