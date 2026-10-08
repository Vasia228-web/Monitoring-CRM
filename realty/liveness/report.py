"""Зведення Блоку 1 для /status і журналу (E8, D52).

`status_block` рахує крок циклу наприкінці прогону й кладе в ops.liveness_runs.report —
сайт лише читає готовий рядок (/api/status/liveness, інтеграція, конфлікт 9):

  * покриття — частка актуальних ключів, перевірених НОВИМ підписом за
    `report.coverage_days`; скільки ключів прострочено (довше за 2 × recheck_days);
  * зняття — за вікнами `report.windows_days`, по джерелах і причинах (410, банер
    DOM.RIA, повторний 404);
  * повернення за `report.return_window_days` — по джерелах і причині того
    зняття, яке повернення скасувало (подія returned несе prev_removal_reason і
    prev_removal_event_id; для старіших подій — останнє зняття не пізніше за
    повернення): після банера й повторного 404 — показник помилки правила
    (рішення власника 1, D46); після 410 — повторна активація OLX/rieltor, окремо
    (D47 п. 6); після зняття старим кодом — «до E8». Частка — когортою: знаменник —
    зняття за вікно, чисельник — ті з НИХ, що повернулись (≤ 1; рецензія E8, D52);
  * вибірка знятих — скільки з перевірених виявились живими;
  * беклог зниклих із переліку; «не перевіряється» (Благо).

`liquidity` — строк продажу (медіана, події, цензуровані, S(30), S(90)) — для
звіту власнику «до/після» (рішення власника 2: показати, як зміниться оцінка).
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select

from ..models import CheckEvent, Listing, ListingEvent
from . import policy as pol
from .signatures import REMOVAL_SIGNATURES

RULE_ERROR_REASONS = ("ria_archive", "repeat_404")


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def status_block(session, cfg, *, now: datetime) -> dict:
    rows = session.execute(select(Listing.id, Listing.site_key, Listing.source,
                                  Listing.original_url, Listing.is_active,
                                  Listing.absent_since)).all()
    hosts_of: dict[str, str | None] = {}
    key_rows: dict[str, list] = defaultdict(list)
    unconfirmed: dict[str, int] = defaultdict(int)
    absent: dict[str, int] = defaultdict(int)
    for lid, site_key, source, url, active, absent_since in rows:
        key = site_key or pol.row_key(lid)
        if key not in hosts_of:
            hosts_of[key] = pol.host_for(cfg, key, url)
        host = hosts_of[key]
        if active and host is not None and not cfg.hosts[host].checkable:
            unconfirmed[source] += 1
        if active and absent_since is not None:
            absent[source] += 1
        key_rows[key].append((source, bool(active)))

    longest = max([s.recheck_days for s in cfg.hosts.values()] + [cfg.report.coverage_days])
    since = now - timedelta(days=2 * longest + 1)
    last_ok: dict[str, datetime] = {}
    for lid, site_key, at in session.execute(
            select(Listing.id, Listing.site_key, CheckEvent.checked_at)
            .join(Listing, Listing.id == CheckEvent.listing_id)
            .where(CheckEvent.signature.isnot(None), CheckEvent.alive.isnot(None),
                   CheckEvent.checked_at >= since)):
        key = site_key or pol.row_key(lid)
        if key not in last_ok or at > last_ok[key]:
            last_ok[key] = at

    cover_after = now - timedelta(days=cfg.report.coverage_days)
    coverage: dict[str, dict] = {}
    by_host: dict[str, dict] = {}
    for key, members in key_rows.items():
        host = hosts_of.get(key)
        if host is None or not cfg.hosts[host].checkable or not any(a for _, a in members):
            continue
        checked = last_ok.get(key)
        overdue_after = now - timedelta(days=2 * cfg.hosts[host].recheck_days)
        h = by_host.setdefault(host, {"keys": 0, "covered": 0, "never": 0, "overdue": 0})
        h["keys"] += 1
        h["covered"] += int(checked is not None and checked >= cover_after)
        h["never"] += int(checked is None)
        h["overdue"] += int(checked is None or checked < overdue_after)
        for source in {s for s, a in members if a}:
            c = coverage.setdefault(source, {"keys": 0, "covered": 0, "never": 0})
            c["keys"] += 1
            c["covered"] += int(checked is not None and checked >= cover_after)
            c["never"] += int(checked is None)
    for d in (*coverage.values(), *by_host.values()):
        d["share"] = round(d["covered"] / d["keys"], 4) if d["keys"] else None
    for d in by_host.values():
        d["overdue_share"] = round(d["overdue"] / d["keys"], 4) if d["keys"] else None

    windows = sorted(set(cfg.report.windows_days))
    oldest = now - timedelta(days=max(windows + [cfg.report.return_window_days]))
    removals: dict[str, dict] = {}
    for source, reason, at in session.execute(
            select(ListingEvent.source, ListingEvent.reason, ListingEvent.at)
            .where(ListingEvent.kind == "removed", ListingEvent.at >= oldest)):
        for w in windows:
            if at >= now - timedelta(days=w):
                d = removals.setdefault(source or "?", {}).setdefault(f"{w}d", {})
                d[reason or "?"] = d.get(reason or "?", 0) + 1

    ret_after = now - timedelta(days=cfg.report.return_window_days)
    returned = session.execute(
        select(ListingEvent.listing_id, ListingEvent.source, ListingEvent.at,
               ListingEvent.evidence)
        .where(ListingEvent.kind == "returned", ListingEvent.at >= ret_after)).all()
    # Старі події returned (без prev_removal_event_id): останнє зняття рядка не
    # пізніше за повернення — двійковим пошуком у його відсортованих зняттях.
    legacy_ids = list({lid for lid, _s, _a, ev in returned
                       if not (isinstance(ev, dict) and "prev_removal_event_id" in ev)})
    removals_of: dict[int, list[tuple]] = defaultdict(list)
    for chunk_start in range(0, len(legacy_ids), 500):
        chunk = legacy_ids[chunk_start:chunk_start + 500]
        for lid, at, eid, reason in session.execute(
                select(ListingEvent.listing_id, ListingEvent.at, ListingEvent.id,
                       ListingEvent.reason)
                .where(ListingEvent.kind == "removed", ListingEvent.listing_id.in_(chunk))
                .order_by(ListingEvent.at, ListingEvent.id)):
            removals_of[lid].append((at, eid, reason))
    removed_window: dict[str, dict] = {}
    window_removals: dict[int, tuple[str, str]] = {}
    for eid, source, reason in session.execute(
            select(ListingEvent.id, ListingEvent.source, ListingEvent.reason)
            .where(ListingEvent.kind == "removed", ListingEvent.at >= ret_after)):
        d = removed_window.setdefault(source or "?", {})
        d[reason or "?"] = d.get(reason or "?", 0) + 1
        window_removals[eid] = (source or "?", reason or "?")
    returns: dict[str, dict] = {}
    came_back: set[int] = set()                    # зняття за вікно, що повернулись
    for lid, source, at, ev in returned:
        ev = ev if isinstance(ev, dict) else {}
        if "prev_removal_event_id" in ev:
            prev_id, why = ev.get("prev_removal_event_id"), ev.get("prev_removal_reason")
        else:
            got = removals_of.get(lid) or []
            i = bisect_right(got, (at, float("inf"))) - 1
            prev_id, why = (got[i][1], got[i][2]) if i >= 0 else (None, None)
        d = returns.setdefault(source or "?", {})
        d[why or "legacy"] = d.get(why or "legacy", 0) + 1
        if prev_id in window_removals:
            came_back.add(prev_id)

    def share(sources, reasons):
        back = sum(1 for eid in came_back if window_removals[eid][0] in sources
                   and window_removals[eid][1] in reasons)
        gone = sum(removed_window.get(s, {}).get(r, 0) for s in sources for r in reasons)
        return {"returned": back, "removed": gone,
                "share": round(back / gone, 4) if gone else None}

    all_sources = sorted(set(removed_window) | set(returns))
    rule_error = {s: share([s], RULE_ERROR_REASONS) for s in all_sources}
    rule_error["_all"] = share(all_sources, RULE_ERROR_REASONS)
    reactivation = {s: share([s], ("status_410",)) for s in all_sources}
    reactivation["_all"] = share(all_sources, ("status_410",))
    repeat404 = share(all_sources, ("repeat_404",))

    sample: dict[str, dict] = {}
    for source, alive in session.execute(
            select(Listing.source, CheckEvent.alive)
            .join(Listing, Listing.id == CheckEvent.listing_id)
            .where(CheckEvent.reason == "rm_sample", CheckEvent.signature.isnot(None),
                   CheckEvent.checked_at >= ret_after)):
        d = sample.setdefault(source, {"checked": 0, "alive": 0, "removed": 0, "unclear": 0})
        d["checked"] += 1
        d["alive" if alive is True else "removed" if alive is False else "unclear"] += 1
    for d in sample.values():
        known = d["alive"] + d["removed"]
        d["alive_share"] = round(d["alive"] / known, 4) if known else None

    return {
        "computed_at": _iso(now),
        "coverage_days": cfg.report.coverage_days,
        "coverage": coverage, "coverage_by_host": by_host,
        "removals": removals, "windows_days": windows,
        "return_window_days": cfg.report.return_window_days,
        "returns": returns, "removed_in_return_window": removed_window,
        "rule_error": rule_error, "reactivation_410": reactivation,
        "repeat404_returns": repeat404,
        "removed_sample": sample,
        "absent_backlog": dict(absent),
        "unconfirmed": dict(unconfirmed),
        "reasons": list(REMOVAL_SIGNATURES),
    }


def liquidity(session, *, bands=(1, 2, 3)) -> dict:
    """Строк продажу (Каплан—Меєр) — усі квартири й за кімнатністю."""
    from ..analytics import segments
    from ..analytics.settings import load as load_settings
    from ..analytics.survival import Observation, estimate

    cfg = load_settings()
    universe = segments.build_universe(session)

    def est(items):
        obs = [Observation(days=o[0], event=o[1], entry=o[2])
               for it in items if (o := it.observation) is not None]
        r = estimate(obs, cfg)
        checkpoints = {c.get("day"): c.get("survival") for c in (r.get("checkpoints") or [])
                       if isinstance(c, dict)}
        return {k: r.get(k) for k in ("n", "events", "censored", "median_days", "q25_days")} | {
            "S30": checkpoints.get(30), "S90": checkpoints.get(90)}

    out = {"all": est(universe.items)}
    for band in bands:
        out[f"rooms_{band}"] = est([i for i in universe.items if i.band == band])
    return out
