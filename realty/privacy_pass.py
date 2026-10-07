"""`cli.py privacy scan|apply|impact` — телефони в НАЯВНИХ рядках (кроки E6–E7, D51).

Нові записи чистять слухачі ORM самі (`realty/models.py`). Тут — разовий прохід
по тому, що вже лежить у базі (рішення власника 5, D46: «зі свіжим бекапом»):

  * `scan [--db] [--limit N] [--since ДАТА]` — ЛИШЕ ЧИТАННЯ: скільки рядків і
    фрагментів БУЛО Б замінено — за джерелом (рядки LUN — ще й за сайтом, куди
    веде посилання) і полем, серед усіх і серед актуальних; форми замін без цифр
    номера («+38 (0XX) XXX-XX-XX»), відкинуті «майже номери» (форма й причина,
    зокрема групи через «/», «⏎», «+» — reason loose). Ні текстів, ні номерів, ні
    імен не друкує. --since — дата чи дата з часом у будь-якому ISO-написанні
    («2026-10-06», «2026-10-06T00:00», «… 00:00Z»), UTC, як first_seen у базі.
  * `impact [--db]` — ЛИШЕ ЧИТАННЯ: чи не змінить заміна похідних величин —
    classify_condition, classify_market (і в тій формі, як їх кличе
    housekeeping.reclassify: назва ЖК третім текстом, стан — з ринком),
    street_from_text, вердикт контролю якості й пробне зведення квартир
    (розбиття на групи) — до й після. Очікувано 0. Працює й на базі без
    міграції E6 (колонки, яких ще немає, не читаються).
  * `apply --yes [--db]` — разова заміна: без --yes відмовляє; бекап зі status ok
    не старший за `apply.backup_max_age_h` (інакше відмова; --no-backup-check —
    лише для копій); замок циклу; більше `apply.max_rows` рядків — стоп; пакети
    ≤ `apply.batch_rows` рядків на транзакцію BEGIN IMMEDIATE; у кожній — відбиток
    усіх колонок, КРІМ description і title, до й після (розбіжність — ROLLBACK і
    стоп); друкує кількості до/після й залишок за повторним скануванням (має
    бути 0). Змінені id — у файл `--ids-out` (лише номери рядків).
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import text

from . import links, privacy
from .links_index import db_path, listing_columns, open_readonly, rows_digest, write_engine


def _group(source: str, url: str | None) -> str:
    """Джерело; для LUN — ще й сайт, на який веде посилання (lun>olx, lun>rieltor…)."""
    if source != "lun":
        return source
    try:
        fam = links.family_of(url)
    except Exception:                                # noqa: BLE001 — лише підпис групи
        fam = None
    return f"lun>{fam or '?'}"


def parse_since(value: str) -> str:
    """--since → 'YYYY-MM-DD HH:MM:SS' (UTC) — у тому вигляді, в якому first_seen
    лежить у базі ('2026-10-06 05:00:00.123456'), бо порівняння — текстове.

    «2026-10-06T00:00» без цього відкидав би всі рядки того самого дня («T» > « »).
    """
    from datetime import datetime, timezone

    raw = (value or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        raise ValueError(f"--since: «{value}» — не дата (2026-10-06 чи 2026-10-06 05:00, UTC)") \
            from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _rows(conn, fields: tuple[str, ...], limit: int | None, since: str | None = None):
    cols = ", ".join(fields)
    sql = (f"SELECT id, source, original_url, coalesce(manual_active, is_active), {cols} "
           f"FROM listings")
    params: tuple = ()
    if since:
        # Нові оголошення (контрольна точка E6: після циклу номерів у нових — 0).
        sql += " WHERE first_seen >= ?"
        params = (parse_since(since),)
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params)


# --- scan -----------------------------------------------------------------------------------

_NEAR_REASON = {"code": "код", "shape": "форма", "loose": "інший роздільник"}


def scan(*, db: str | None = None, limit: int | None = None, since: str | None = None,
         cfg=None) -> dict:
    """Звіт «що було б замінено» — лише читання (див. докстрінг модуля)."""
    cfg = cfg or privacy.config()
    fields = tuple(cfg.phone.fields)
    since = parse_since(since) if since else None
    path = db_path(db)
    conn = open_readonly(path)
    per = defaultdict(Counter)
    kinds, patterns, shapes = Counter(), Counter(), Counter()
    near = Counter()
    rows = changed_rows = changed_active = 0
    t0 = time.perf_counter()
    try:
        for row in _rows(conn, fields, limit, since):
            lid, source, url, active = row[:4]
            rows += 1
            group = _group(source, url)
            per[group]["rows"] += 1
            any_change = False
            for name, value in zip(fields, row[4:]):
                if not value:
                    continue
                misses: list = []
                new, hits = privacy.find(value, cfg=cfg, misses=misses)
                for m in misses:
                    near[f"{m.shape} ({_NEAR_REASON.get(m.reason, m.reason)})"] += 1
                if new == value:
                    continue
                any_change = True
                per[group][f"{name}_rows"] += 1
                if active:
                    per[group][f"{name}_rows_active"] += 1
                per[group][f"{name}_forms"] += len(hits)
                for h in hits:
                    kinds[h.kind] += 1
                    patterns[h.pattern] += 1
                    if h.shape:
                        shapes[f"{h.kind}:{h.shape}"] += 1
            if any_change:
                changed_rows += 1
                changed_active += bool(active)
    finally:
        conn.close()
    top = cfg.scan.top_patterns
    return {
        "db": str(path), "fields": list(fields), "enabled": cfg.phone.enabled,
        "rows_scanned": rows, "limit": limit, "since": since,
        "seconds": round(time.perf_counter() - t0, 2),
        "rows_would_change": changed_rows, "active_rows_would_change": changed_active,
        "forms": sum(kinds.values()), "by_kind": dict(kinds.most_common()),
        "by_shape": dict(shapes.most_common()),
        "per_group": {g: dict(c) for g, c in sorted(per.items())},
        "patterns": patterns.most_common(top), "patterns_distinct": len(patterns),
        "near_misses": near.most_common(top),
    }


def render_scan(rep: dict) -> str:
    fields = rep["fields"]
    lines = [f"база: {rep['db']} (лише читання)",
             f"рядків переглянуто: {rep['rows_scanned']}"
             + (f" (--limit {rep['limit']})" if rep["limit"] else "")
             + (f" (нові: first_seen ≥ {rep['since']})" if rep.get("since") else "")
             + f"; {rep['seconds']} с"]
    if not rep["enabled"]:
        lines.append("УВАГА: phone.enabled = false — слухачі нічого не замінюють; звіт — як було б")
    lines.append(f"рядків, що змінились би: {rep['rows_would_change']} "
                 f"(з них актуальних: {rep['active_rows_would_change']}); фрагментів: {rep['forms']}")
    head = f"  {'група':<14}{'рядків':>8}"
    for f in fields:
        head += f"{f + ' (акт.)':>22}{'фрагм.':>8}"
    lines.append(head)
    for group, st in rep["per_group"].items():
        line = f"  {group:<14}{st.get('rows', 0):>8}"
        for f in fields:
            line += (f"{str(st.get(f + '_rows', 0)) + ' (' + str(st.get(f + '_rows_active', 0)) + ')':>22}"
                     f"{st.get(f + '_forms', 0):>8}")
        lines.append(line)
    lines.append("види: " + (", ".join(f"{k} {v}" for k, v in rep["by_kind"].items()) or "—"))
    lines.append("форми груп: " + (", ".join(f"{k} {v}" for k, v in rep["by_shape"].items()) or "—"))
    lines.append(f"найчастіші форми замін ({len(rep['patterns'])} з {rep['patterns_distinct']}; "
                 f"цифри номера — X, маска — *):")
    for pattern, n in rep["patterns"]:
        lines.append(f"  {n:>5}  {pattern}")
    lines.append("відкинуті «майже номери» (10–13 цифр: форма груп і причина; «інший "
                 "роздільник» — форма без цифр) — не замінюються:")
    for shape, n in rep["near_misses"] or [("—", 0)]:
        lines.append(f"  {n:>5}  {shape}")
    return "\n".join(lines)


# --- impact ---------------------------------------------------------------------------------


class _Redacted:
    """Рядок оголошення з іншим описом і назвою — для обчислень без запису в базу."""

    def __init__(self, row, description, title) -> None:
        self._row = row
        self.description = description
        self.title = title

    def __getattr__(self, name):
        return getattr(self._row, name)


def impact(*, db: str | None = None, dedup: bool = True, force_dedup: bool = False,
           cfg=None) -> dict:
    """Чи змінює заміна похідні величини (див. докстрінг модуля). Лише читання."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.exc import InvalidRequestError
    from sqlalchemy.orm import Session, defer

    from .models import Listing
    from .quality import rules

    cfg = cfg or privacy.config()
    path = db_path(db)
    eng = create_engine("sqlite://", creator=lambda: open_readonly(path), future=True)
    thresholds = None
    if rules.THRESHOLDS_FILE.exists():
        thresholds = rules.Thresholds.from_json(
            json.loads(rules.THRESHOLDS_FILE.read_text(encoding="utf-8")))
    t0 = time.perf_counter()
    try:
        with eng.connect() as c:
            have = {r[1] for r in c.exec_driver_sql('PRAGMA table_info("listings")')}
        # База до міграції E6 (копія Етапу 0): колонок, яких ще немає (site_key), не
        # читаємо; звернення до них — помилка з підказкою, а не тихе None.
        missing = [col.key for col in Listing.__table__.columns if col.name not in have]
        opts = [defer(getattr(Listing, k), raiseload=True) for k in missing]
        with Session(bind=eng, autoflush=False) as s:
            listings = list(s.scalars(select(Listing).options(*opts).order_by(Listing.id)))
            try:
                result = _impact_rows(s, listings, cfg, thresholds, dedup, force_dedup)
            except InvalidRequestError as e:
                raise SystemExit(f"колонок {missing} у базі ще немає, а вони потрібні "
                                 f"({type(e).__name__}) — спершу `cli.py db migrate` на копії") \
                    from None
    finally:
        eng.dispose()
    result["db"] = str(path)
    result["seconds"] = round(time.perf_counter() - t0, 1)
    return result


def _impact_rows(s, listings, cfg, thresholds, dedup: bool, force_dedup: bool) -> dict:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from . import dedup as dd
    from .models import PriceEvent
    from .normalize import classify_condition, classify_market
    from .quality import rules

    diff = Counter()
    redacted = {}
    for r in listings:
        d = privacy.find(r.description, cfg=cfg)[0] if "description" in cfg.phone.fields \
            else r.description
        t = privacy.find(r.title, cfg=cfg)[0] if "title" in cfg.phone.fields else r.title
        if d == r.description and t == r.title:
            continue
        redacted[r.id] = (d, t)
        # Обидві форми виклику: проста (як у джерелах) і та, що в
        # housekeeping.reclassify — назва ЖК третім текстом, стан — з ринком.
        m0 = classify_market(r.title, r.description, r.complex_name, built_year=r.built_year)
        m1 = classify_market(t, d, r.complex_name, built_year=r.built_year)
        if classify_condition(r.title, r.description) != classify_condition(t, d) or \
                classify_condition(r.title, r.description, market=m0) != \
                classify_condition(t, d, market=m1):
            diff["classify_condition"] += 1
        if classify_market(r.title, r.description, built_year=r.built_year,
                           complex_name=r.complex_name) != \
                classify_market(t, d, built_year=r.built_year, complex_name=r.complex_name) \
                or m0 != m1:
            diff["classify_market"] += 1
        if dd.street_from_text(r.description) != dd.street_from_text(d):
            diff["street_from_text"] += 1
        if thresholds is not None:
            rec = {"price": r.price, "rooms": r.rooms, "location": r.location,
                   "original_url": r.original_url, "price_usd": r.price_usd,
                   "price_per_sqm": r.price_per_sqm, "area_total": r.area_total,
                   "condition": r.condition, "market_type": r.market_type,
                   "built_year": r.built_year}
            if rules.validate({**rec, "title": r.title, "description": r.description},
                              thresholds) != \
                    rules.validate({**rec, "title": t, "description": d}, thresholds):
                diff["quality_validate"] += 1
    result = {"rows_would_change": len(redacted),
              "differences": {k: diff.get(k, 0) for k in (
                  "classify_condition", "classify_market", "street_from_text",
                  "quality_validate")},
              "quality_thresholds": thresholds is not None}
    if not dedup:
        return result
    history = defaultdict(list)
    for lid, at, usd in s.execute(
            select(PriceEvent.listing_id, PriceEvent.observed_at, PriceEvent.price_usd)
            .where(PriceEvent.price_usd.is_not(None))
            .order_by(PriceEvent.listing_id, PriceEvent.observed_at)):
        history[lid].append((dd._naive(at), usd))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    manual = dd.Manual.load(s)
    base = [dd.shape_of(r, tuple(history.get(r.id, ())), now) for r in listings]
    alt = [dd.shape_of(_Redacted(r, *redacted[r.id]) if r.id in redacted else r,
                       tuple(history.get(r.id, ())), now) for r in listings]
    shapes_differ = sum(1 for a, b in zip(base, alt) if a != b)
    runs = {}
    for label, rule_set in (("active", dd.active_rules()), ("all", frozenset(dd.RULES))):
        if not shapes_differ and not force_dedup:
            # Зведення — чиста функція форм оголошень: однакові форми дають
            # однакові групи. Перерахунок — 4 повні зведення (M4 ≈210 с,
            # Fedora ≈35 хв) — лише якщо форми різняться або --force-dedup.
            runs[label] = {"rules": sorted(rule_set), "groups_before": None,
                           "groups_after": None, "listings_in_changed_groups": 0,
                           "skipped": "форми однакові"}
            continue
        g1 = dd.cluster(base, rule_set, manual, {})
        g2 = dd.cluster(alt, rule_set, manual, {})
        p1 = sorted(sorted(g) for g in g1)
        p2 = sorted(sorted(g) for g in g2)
        part1 = {i: n for n, g in enumerate(p1) for i in g}
        part2 = {i: n for n, g in enumerate(p2) for i in g}
        moved = sum(1 for i in part1 if set(p1[part1[i]]) != set(p2[part2[i]]))
        runs[label] = {"rules": sorted(rule_set), "groups_before": len(p1),
                       "groups_after": len(p2), "listings_in_changed_groups": moved}
    result["dedup"] = {"shapes_differ": shapes_differ, **runs}
    return result


def render_impact(rep: dict) -> str:
    lines = [f"база: {rep['db']} (лише читання); {rep['seconds']} с",
             f"рядків, що змінились би: {rep['rows_would_change']}",
             "розбіжностей до/після заміни:"]
    for k, v in rep["differences"].items():
        lines.append(f"  {k:<22}{v}")
    if not rep["quality_thresholds"]:
        lines.append("  (контроль якості не звірено: немає data/quality_thresholds.json)")
    if "dedup" in rep:
        d = rep["dedup"]
        lines.append(f"зведення: форм оголошень змінилось би {d['shapes_differ']}")
        for label in ("active", "all"):
            r = d[label]
            if r.get("skipped"):
                lines.append(f"  правила {label} {r['rules'] or '—'}: не перераховано — "
                             f"{r['skipped']}, отже групи ті самі (--force-dedup — перерахувати)")
                continue
            lines.append(f"  правила {label} {r['rules'] or '—'}: груп {r['groups_before']} → "
                         f"{r['groups_after']}; оголошень у змінених групах: "
                         f"{r['listings_in_changed_groups']}")
    total = sum(rep["differences"].values()) + sum(
        rep["dedup"][x]["listings_in_changed_groups"] for x in ("active", "all")
    ) if "dedup" in rep else sum(rep["differences"].values())
    lines.append("НЕЗАЛЕЖНІСТЬ ПІДТВЕРДЖЕНО: 0 розбіжностей" if total == 0
                 else f"УВАГА: розбіжностей {total} — показати власнику до разової заміни")
    return "\n".join(lines)


# --- apply ----------------------------------------------------------------------------------


class _BatchMismatch(Exception):
    pass


def _backup_ok(max_age_h: float) -> tuple[bool, str]:
    from datetime import timedelta

    from . import backup, ops

    try:
        last = backup.last_success_at()
    except Exception as e:                           # noqa: BLE001
        return False, f"не вдалося прочитати журнал бекапів: {e}"
    if last is None:
        return False, "успішного бекапу немає"
    age = ops._now() - last
    if age > timedelta(hours=max_age_h):
        return False, f"останній успішний бекап {last:%d.%m %H:%M} — старший за {max_age_h:g} год"
    return True, f"останній успішний бекап {last:%d.%m %H:%M}"


def apply(*, db: str | None = None, yes: bool = False, wait_min: float = 10.0,
          no_backup_check: bool = False, ids_out: str | None = None, cfg=None,
          out=print) -> int:
    """0 — готово, 1 — розбіжність/стоп, 2 — відмова (без --yes, бекап, замок, вимкнено)."""
    from . import db as dbmod
    from .dbmigrate import wait_cycle_lock

    cfg = cfg or privacy.config()
    fields = tuple(cfg.phone.fields)
    path = db_path(db)
    before_scan = scan(db=str(path), cfg=cfg)
    out(f"база: {path}")
    out(f"до: рядків, що змінились би: {before_scan['rows_would_change']} "
        f"(актуальних {before_scan['active_rows_would_change']}), фрагментів {before_scan['forms']}")
    if not cfg.phone.enabled:
        out("ВІДМОВА: phone.enabled = false у config/privacy.toml")
        return 2
    if not yes:
        out("ВІДМОВА: разова заміна змінює дані — потрібен --yes (спершу `privacy scan`, "
            "`privacy impact` і свіжий бекап)")
        return 2
    if before_scan["rows_would_change"] > cfg.apply.max_rows:
        out(f"СТОП: {before_scan['rows_would_change']} рядків > apply.max_rows "
            f"({cfg.apply.max_rows}) — перегляд і рішення власника")
        return 1
    if not no_backup_check:
        ok, why = _backup_ok(cfg.apply.backup_max_age_h)
        if not ok:
            out(f"ВІДМОВА: {why}. Спершу `cli.py backup` (зі status ok).")
            return 2
        out(why)
    else:
        out("перевірку бекапу пропущено (--no-backup-check — лише для копій)")
    lock = wait_cycle_lock(wait_min)
    if lock is None:
        out(f"ВІДМОВА: цикл збору не звільнив замок за {wait_min:.0f} хв — запустіть між циклами.")
        return 2
    eng = write_engine(path)
    batch = cfg.apply.batch_rows
    changed_ids: list[int] = []
    txns = 0
    max_txn_s = 0.0
    stopped = None
    try:
        with eng.connect() as conn:
            fp_before = dbmod.data_fingerprint(conn)
            other = [c for c in listing_columns(conn) if c not in fields]
        # Кандидати — з одного проходу читання; кожен пакет перечитує свої рядки
        # у власній транзакції (рядок, змінений тим часом, отримає свіжу заміну).
        cands = []
        ro = open_readonly(path)
        try:
            for row in ro.execute(f"SELECT id, {', '.join(fields)} FROM listings ORDER BY id"):
                if any(v and privacy.find(v, cfg=cfg)[0] != v for v in row[1:]):
                    cands.append(row[0])
        finally:
            ro.close()
        t0 = time.perf_counter()
        for start in range(0, len(cands), batch):
            chunk = cands[start:start + batch]
            t_txn = time.perf_counter()
            try:
                with eng.begin() as conn:                         # BEGIN IMMEDIATE
                    d_before = rows_digest(conn, chunk, other)
                    marks = ", ".join(str(i) for i in chunk)
                    params = []
                    for row in conn.execute(text(
                            f"SELECT id, {', '.join(fields)} FROM listings WHERE id IN ({marks})")):
                        new = {f: privacy.find(v, cfg=cfg)[0] if v else v
                               for f, v in zip(fields, row[1:])}
                        if any(new[f] != v for f, v in zip(fields, row[1:])):
                            params.append({"id": row[0], **{f"n_{f}": new[f] for f in fields}})
                    if params:
                        sets = ", ".join(f"{f} = :n_{f}" for f in fields)
                        conn.execute(text(f"UPDATE listings SET {sets} WHERE id = :id"), params)
                    if rows_digest(conn, chunk, other) != d_before:
                        raise _BatchMismatch()
                    changed_ids += [p["id"] for p in params]
            except _BatchMismatch:
                stopped = (chunk[0], chunk[-1])
                break
            txns += 1
            max_txn_s = max(max_txn_s, time.perf_counter() - t_txn)
        took = time.perf_counter() - t0
        with eng.connect() as conn:
            fp_after = dbmod.data_fingerprint(conn)
            check, fk = dbmod.integrity(conn)
    finally:
        eng.dispose()
        lock.release()
    if ids_out:
        Path(ids_out).write_text("\n".join(str(i) for i in changed_ids) + "\n", encoding="utf-8")
    after_scan = scan(db=str(path), cfg=cfg)
    out(f"\nзмінено рядків: {len(changed_ids)} за {took:.2f} с; транзакцій: {txns} "
        f"(≤{batch} рядків, найдовша {max_txn_s * 1000:.0f} мс)")
    out(f"після: рядків із номерами {after_scan['rows_would_change']}, фрагментів "
        f"{after_scan['forms']}")
    out(f"\n  {'що':<28}{'до':>28}{'після':>28}")
    for key in fp_before:
        mark = "" if fp_before[key] == fp_after.get(key) else "   ← РІЗНИЦЯ"
        out(f"  {key:<28}{str(fp_before[key]):>28}{str(fp_after.get(key)):>28}{mark}")
    out(f"  integrity_check: {check}; foreign_key_check: {len(fk)} порушень")
    if ids_out:
        out(f"змінені id: {ids_out}")
    if stopped:
        out(f"УВАГА: у пакеті id {stopped[0]}…{stopped[1]} змінилось щось, крім "
            f"{'/'.join(fields)} — ROLLBACK цього пакета, зупинено. Показати числа власнику.")
        return 1
    same = all(fp_before[k] == fp_after.get(k) for k in fp_before if "listings" in k)
    ok = (same and check == "ok" and not fk and after_scan["rows_would_change"] == 0
          and len(changed_ids) == before_scan["rows_would_change"])
    out("ГОТОВО" if ok else "УВАГА: числа не збіглись — зупинитися, показати власнику.")
    return 0 if ok else 1
