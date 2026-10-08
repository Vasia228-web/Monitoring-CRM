"""Звіти ночі: `cli.py liveness report` (підсумки вікна) і `cli.py night --dry-run` (план).

Підсумки — з ops.night_runs (пише диригент): по хостах — запити, знято, повернуто,
полагоджено, без висновку; звірка актуальних: після = до + повернуто − знято + нові
оголошення дозбору identity (прохід стрічки LUN/flombu — звичайний збір; скільки
рядків і подій ціни він дописав, міряє сам дозбір), залишок — «НЕЗВІРЕНО» (рецензія
E9, D53: не «інші записи», що сходились би за побудовою); строк продажу до/після
(рішення власника 2, D46: «показати, як зміниться оцінка»); перетини з циклом за
мітками ops (має бути 0 — замок). Часи — місцеві (як вікна в config/night.toml),
у дужках — UTC (так зберігаються).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from .. import ops

STATUS_UA = {
    "ok": "гаразд", "partial": "частково (смугу зупинено)", "running": "триває",
    "backup_failed": "БЕКАП НЕ ВДАВСЯ — у цьому вікні нічого не писали", "lock_timeout":
    "цикл не звільнив замок — вікно пропущено", "outside_window": "поза вікном",
    "disabled": "збір вимкнено (COLLECTOR_OFF)", "failed": "АВАРІЯ"}
STOP_UA = {None: "", "deadline": "дедлайн", "blocks": "БЛОКУВАННЯ поспіль",
           "block_share": "БЛОКУВАННЯ (частка)", "sigterm": "зупинено", "killed":
           "зупинено примусово", "no_summary": "без підсумку"}


def _j(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def as_dict(row: ops.NightRun) -> dict:
    d = {c: getattr(row, c) for c in (
        "id", "night_date", "window", "status", "started_at", "finished_at",
        "stop_requests_at", "release_lock_at", "lock_acquired_at", "lock_released_at",
        "lock_waited_s", "config_hash", "liveness_hash", "fuse_mode", "liveness_run_id",
        "active_before", "active_after", "message")}
    for name in ("backup", "plan", "lanes", "batches", "per_host", "per_tier", "fuse",
                 "identity", "totals", "liquidity_before", "liquidity_after", "evidence"):
        d[name] = _j(getattr(row, name))
    return d


def runs(limit: int = 10, run_id: int | None = None) -> list[dict]:
    ops.init_ops()
    with ops.ops_session() as s:
        stmt = select(ops.NightRun).order_by(ops.NightRun.id.desc())
        if run_id is not None:
            stmt = stmt.where(ops.NightRun.id == run_id)
        return [as_dict(r) for r in s.scalars(stmt.limit(limit))]


def cycle_overlaps(d: dict) -> int:
    """Скільки циклів (ops.cycles) перетнулися з часом, коли ніч тримала замок."""
    start, end = d.get("lock_acquired_at"), d.get("lock_released_at")
    if start is None:
        return 0
    end = end or datetime.max
    with ops.ops_session() as s:
        n = 0
        for c in s.scalars(select(ops.CycleRecord).where(ops.CycleRecord.started_at < end)):
            c_end = c.finished_at or datetime.max
            if c.started_at < end and c_end > start:
                n += 1
        return n


def local(dt: datetime | None) -> datetime | None:
    """Наївний UTC (як в ops.db) → місцевий час машини (Fedora: Europe/Kyiv)."""
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None) if dt else None


def hm(dt: datetime | None) -> str:
    """«02:47 (UTC 23:47)» — місцевий час, як у вікнах конфігу, і UTC у дужках."""
    return f"{local(dt):%H:%M} (UTC {dt:%H:%M})" if dt else "—"


def _t(dt) -> str:
    return f"{local(dt):%Y-%m-%d %H:%M:%S} (UTC {dt:%H:%M})" if dt else "—"


def _liq(row: dict | None) -> str:
    if not row:
        return "—"
    a = row.get("all") or {}
    return (f"медіана {a.get('median_days')} дн., подій {a.get('events')}, цензурованих "
            f"{a.get('censored')}, S(30) {a.get('S30')}, S(90) {a.get('S90')}")


def render_run(d: dict) -> str:
    out = ["", "=" * 96,
           f"НІЧ №{d['id']} — {d.get('night_date') or '—'}, вікно {d.get('window') or '—'}: "
           f"{STATUS_UA.get(d['status'], d['status'])}", "=" * 96]
    out.append(f"  почато {_t(d['started_at'])}, закінчено {_t(d['finished_at'])}; "
               f"запити до {hm(d['stop_requests_at'])}, замок до {hm(d['release_lock_at'])} "
               f"(місцевий час)")
    if d.get("lock_acquired_at"):
        released = d.get("lock_released_at")
        in_time = (released is not None and d.get("release_lock_at") is not None
                   and released <= d["release_lock_at"])
        out.append(f"  замок: чекали {(d.get('lock_waited_s') or 0) / 60:.0f} хв, узято "
                   f"{hm(d['lock_acquired_at'])}, звільнено {hm(released)} "
                   f"({'вчасно' if in_time else 'ПІЗНО'}); перетинів із циклом: "
                   f"{cycle_overlaps(d)}")
    b = d.get("backup") or {}
    if b:
        if b.get("due"):
            rule = {"onetime_max_age_hours": " (вікно з M2/M3)"}.get(b.get("rule"), "")
            out.append(f"  бекап на старті{rule}: {b.get('status')} {b.get('file') or ''}"
                       + (f" — {b['message'][:120]}" if b.get("message") else ""))
        else:
            out.append(f"  бекап на старті не потрібен: останній успішний {b.get('last_ok')}")
    if d.get("message"):
        out.append(f"  {d['message']}")
    fz = d.get("fuse") or {}
    # Спрацювання — лише нові (джерело, що вже тримається, наступні пакети лише бачать).
    for t in (t for t in fz.get("trips") or [] if t.get("new", True)):
        if t.get("batch"):
            # Пакет ~15 хв — «прогін» запобіжника: раніше в цьому вікні для джерела вже
            # могли щось зняти й повернути (рецензія E9, D53).
            at = datetime.fromisoformat(t["at"]) if t.get("at") else None
            before = t.get("before") or {}
            whom = (f"з пакета {t['batch']} ({hm(at)}) нічого не знімаємо й не повертаємо; "
                    f"до того в цьому вікні: знято {int(before.get('delisted') or 0)}, "
                    f"повернуто {int(before.get('restored') or 0)}")
        else:
            whom = "нічого не знято й не повернуто"
        out.append(f"  ЗАПОБІЖНИК ({fz.get('mode')}): {t['source']} — «знято» {t['removed']} "
                   f"із {t['checked']} ({t['reason']}); {whom}; чекає рішення на /status")
    if fz.get("held"):
        out.append(f"  під запобіжником: {', '.join(fz['held'])}")
    lanes, per_host = d.get("lanes") or {}, d.get("per_host") or {}
    hosts = sorted(set(lanes) | set(per_host))
    if hosts:
        out.append("")
        out.append(f"  {'сайт':<20}{'запитів':>8}{'ключів':>8}{'знято':>7}{'поверн.':>8}"
                   f"{'полаг.':>7}{'без висн.':>10}{'(404)':>7}{'запоб.':>8}{'не дійшли':>10}"
                   f"  зупинка")
        tot = dict.fromkeys(("requests", "keys", "delisted", "restored", "repaired", "unknown",
                             "not_found", "held", "not_reached"), 0)
        for host in hosts:
            ln, ph = lanes.get(host) or {}, per_host.get(host) or {}
            row = {"requests": int(ln.get("requests") or 0), "keys": int(ph.get("keys") or 0),
                   "delisted": int(ph.get("delisted") or 0),
                   "restored": int(ph.get("restored") or 0),
                   "repaired": int(ph.get("repaired") or 0),
                   "unknown": int(ph.get("unknown") or 0),
                   "not_found": int(ph.get("not_found") or 0),
                   "held": int(ln.get("skipped_held") or 0) + int(ph.get("held_keys") or 0),
                   "not_reached": int(ln.get("not_reached") or 0)}
            for k, v in row.items():
                tot[k] += v
            stop = STOP_UA.get(ln.get("stopped"), ln.get("stopped") or "")
            if ln.get("killed") and ln.get("stopped") != "killed":
                stop = (stop + ", " if stop else "") + "зупинено примусово"
            out.append(f"  {host:<20}{row['requests']:>8}{row['keys']:>8}{row['delisted']:>7}"
                       f"{row['restored']:>8}{row['repaired']:>7}{row['unknown']:>10}"
                       f"{row['not_found']:>7}{row['held']:>8}{row['not_reached']:>10}  {stop}")
        out.append(f"  {'разом':<20}{tot['requests']:>8}{tot['keys']:>8}{tot['delisted']:>7}"
                   f"{tot['restored']:>8}{tot['repaired']:>7}{tot['unknown']:>10}"
                   f"{tot['not_found']:>7}{tot['held']:>8}{tot['not_reached']:>10}")
        out.append("  (знято/повернуто/полагоджено/без висновку — оголошення, тобто рядки всіх "
                   "джерел ключа; «запоб.» — ключі під запобіжником: не питали або відповідь "
                   "не застосовано)")
    plan = (d.get("plan") or {}).get("hosts") or {}
    if plan:
        out.append("")
        out.append("  план вікна (ключів за роботами):")
        for host, p in sorted(plan.items()):
            if p.get("skipped"):
                out.append(f"    {host:<20} без смуги: {p['skipped']}")
                continue
            tiers = ", ".join(f"{k} {v}" for k, v in (p.get("tiers") or {}).items())
            out.append(f"    {host:<20} {p['requests']:>6} × {p['pace']:.1f} с ≈ "
                       f"{p['seconds'] / 60:>5.0f} хв  {tiers or '—'}"
                       + (f"; дозбір {p['identity']}" if p.get("identity") else ""))
    batches = d.get("batches") or []
    if batches:
        longest = max((b.get("longest_txn_s") or 0) for b in batches)
        out.append(f"  пакетів застосування: {len(batches)}, найдовша транзакція "
                   f"{longest:.3f} с")
    for host, rec in sorted((d.get("identity") or {}).items()):
        rep = rec.get("report") or {}
        out.append(f"  дозбір identity ({host}, {rec.get('source')}): "
                   f"{json.dumps(rep.get('done', rep), ensure_ascii=False)[:160]}")
    out += reconcile_lines(d)
    lb, la = d.get("liquidity_before"), d.get("liquidity_after")
    if lb or la:
        new = identity_written(d.get("identity"))["listings"]
        out.append("  строк продажу (Каплан—Меєр):" + (
            f" «після» — разом із {new} новими оголошеннями дозбору identity (збір стрічки)"
            if new else ""))
        out.append(f"    до:    {_liq(lb)}")
        out.append(f"    після: {_liq(la)}")
        for band in ("rooms_1", "rooms_2", "rooms_3"):
            b0, b1 = (lb or {}).get(band) or {}, (la or {}).get(band) or {}
            if b0 or b1:
                out.append(f"    {band}: медіана {b0.get('median_days')} → {b1.get('median_days')}"
                           f" дн. (подій {b0.get('events')} → {b1.get('events')})")
    out.append("=" * 96)
    return "\n".join(out)


def identity_written(identity: dict | None) -> dict:
    """Скільки рядків і подій ціни дописав за вікно дозбір identity (прохід стрічки
    LUN/flombu — звичайний збір, pipeline._upsert): для звірки «актуальних після»."""
    out = {"listings": 0, "price_events": 0}
    for rec in (identity or {}).values():
        for d in ((rec or {}).get("report") or {}).get("written", {}).values():
            for k in out:
                out[k] += int((d or {}).get(k) or 0)
    return out


def reconcile(d: dict) -> dict | None:
    """Звірка вікна: актуальних після = до + повернуто − знято + нові оголошення
    дозбору identity; подій ціни — лише від дозбору. Залишок — «незвірено»."""
    before, after = d.get("active_before"), d.get("active_after")
    if before is None or after is None:
        return None
    per_host = d.get("per_host") or {}
    returned = sum(int((ph or {}).get("restored") or 0) for ph in per_host.values())
    removed = sum(int((ph or {}).get("delisted") or 0) for ph in per_host.values())
    written = identity_written(d.get("identity"))
    out = {"before": before, "after": after, "returned": returned, "removed": removed,
           "identity_new": written["listings"], "identity_prices": written["price_events"]}
    out["active_residual"] = after - (before + returned - removed + written["listings"])
    tot = d.get("totals") or {}
    b, a = tot.get("before") or {}, tot.get("after") or {}
    if b.get("listings") is not None and a.get("listings") is not None:
        out["listings_residual"] = a["listings"] - b["listings"] - written["listings"]
    if b.get("price_events") is not None and a.get("price_events") is not None:
        out["price_events_delta"] = a["price_events"] - b["price_events"]
        out["price_events_residual"] = out["price_events_delta"] - written["price_events"]
    return out


def reconcile_lines(d: dict) -> list[str]:
    r = reconcile(d)
    if r is None:
        return []
    out = ["", f"  актуальних оголошень: до {r['before']} → після {r['after']} "
               f"(повернуто {r['returned']}, знято {r['removed']}"
               + (f", нових від дозбору identity {r['identity_new']}" if r["identity_new"] else "")
               + ")"]
    bad = []
    if r["active_residual"]:
        bad.append(f"актуальних {r['active_residual']:+d}")
    if r.get("listings_residual"):
        bad.append(f"рядків {r['listings_residual']:+d}")
    if r.get("price_events_residual"):
        bad.append(f"подій ціни {r['price_events_residual']:+d}")
    if "price_events_delta" in r:
        out.append(f"  подій ціни: {r['price_events_delta']:+d}"
                   + (f" (дозбір identity — збір стрічки LUN/flombu: {r['identity_prices']:+d})"
                      if r["identity_prices"] else ""))
    out.append(f"  НЕЗВІРЕНО: {', '.join(bad)} — зміни поза ніччю й дозбором (власник? інший "
               f"процес?)" if bad else "  звірка: збіглось")
    return out


def render_plan(d: dict) -> str:
    w = d["window"]
    out = ["", "=" * 100,
           f"НІЧ — ПЛАН ({'поточне' if w['current'] else 'найближче'} вікно {w['label']}, ніч "
           f"{w['night_date']}): без мережі й без запису", "=" * 100,
           f"  старт {w['start']}, нові запити до {w['stop_requests']}, замок до "
           f"{w['release_lock']} (місцевий час)"]
    b = d["backup"]
    out.append(f"  бекап на старті: {'ПОТРІБЕН' if b['due'] else 'не потрібен'} (останній "
               f"успішний {b['last_ok'] or 'ніколи'}, поріг {b['max_age_hours']:g} год, "
               f"з M2/M3 — {b.get('onetime_max_age_hours', 0):g} год; ключів M2/M3 "
               f"{b.get('onetime_keys', 0)})"
               + (f"; ≈ {b['seconds'] / 60:.0f} хв із першого вікна" if b.get("seconds") else ""))
    out.append(f"  запобіжник: {d['fuse_mode']}; тримаються: {', '.join(d['held']) or 'ніхто'}")
    if d.get("disabled"):
        out.append("  УВАГА: data/COLLECTOR_OFF — нічний диригент нічого не робитиме")
    out.append(f"  порядок робіт: {', '.join(d['order'])}")
    tiers = [t for t in d["order"] if t != "identity"]
    short = {"canary": "контр", "held": "утрим", "legacy_404": "M2·404",
             "onetime_reseen": "M2·reseen", "onetime_hinted": "M3·підк", "onetime_blind":
             "M3·сліпі", "rm_sample": "вибірка", "overdue": "догін"}
    out.append("")
    out.append(f"  {'сайт':<15}{'темп':>6}{'крок':>6}"
               + "".join(f"{short.get(t, t):>10}" for t in tiers)
               + f"{'запитів':>9}{'× крок =':>14}{'у вікно':>9}{'вікон':>7}"
               + f"  {'лишиться після вікон 1–4':>28}")

    def count(h, t):
        tiers_ = h["tiers"] or {}
        # Робота «held» — два яруси: held (незастосоване «знято») і held_return.
        return tiers_.get(t, 0) + (tiers_.get("held_return", 0) if t == "held" else 0)

    for host, h in d["hosts"].items():
        if h.get("skipped"):
            out.append(f"  {host:<15} без смуги: {h['skipped']}")
            continue
        dur = h.get("seconds_real", h["seconds"])
        hours, mins = int(dur // 3600), int(dur % 3600 // 60)
        took = f"{hours} год {mins:02d} хв" if dur >= 60 else f"{dur:.0f} с"
        left = " / ".join(str(x) for x in h.get("left_after_windows") or [])
        out.append(f"  {host:<15}{h['pace']:>5.1f}с{h.get('rate', h['pace']):>5.1f}с" + "".join(
            f"{count(h, t):>10}" for t in tiers)
            + f"{h['requests']:>9}{took:>14}"
            f"{h['window_capacity']:>9}{h['windows_needed'] or 0:>7}  {left:>28}")
        notes = []
        if h.get("identity"):
            notes.append(f"дозбір identity: {h['identity']} (після перевірок смуги)")
        if h.get("held_keys"):
            notes.append(f"під запобіжником {h['held_keys']} ключів — не питаємо")
        if h.get("attempted_tonight"):
            notes.append(f"уже пробували цієї ночі {h['attempted_tonight']}")
        if h.get("overdue_share") is not None:
            notes.append(f"прострочених {100 * h['overdue_share']:.0f}%")
        if notes:
            out.append(f"  {'':<15}  ↳ " + "; ".join(notes))
    out.append("-" * 100)
    out.append("  «крок» — max(темп, замір минулої ночі чи типовий час запиту): темп "
               "(policy.pace night = max(delay, full_delay)) — старт-до-старту;")
    out.append("  «у вікно» — запитів від старту до stop_requests; «лишиться» — перше вікно "
               "без бекапу, якщо він потрібен; без очікування замка циклу; хости паралельно.")
    for note in d.get("notes") or []:
        out.append(f"  УВАГА: {note}")
    out.append("=" * 100)
    return "\n".join(out)
