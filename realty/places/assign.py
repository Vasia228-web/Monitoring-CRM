"""Крок циклу «райони й ЖК» (`cli.py places assign [--dry-run]`; Блок 4, крок E10, D57).

0 запитів до джерел: докази, що вже є в рядках (поля джерел і place_raw), → довідник
config/places/*.toml → ключі. Порядок:

  1. читання всіх оголошень (без описів; описи — лише кандидатам у «не в ЖК»: вторинка
     без ознаки ЖК у полях);
  2. визначення району й ЖК кожного оголошення (places.resolve) у два проходи: спершу
     кожне саме, потім — з доказами будинку (places.address: інші квартири за тією самою
     вулицею й номером — ступінь «addr», вето мітки села, вето «не в ЖК»). СТРОГО «лише
     туди, де порожньо або не визначено»: district_key NULL → заповнюємо; complex_key
     NULL чи '_none' («не в ЖК» уточнюється конкретним ЖК) → заповнюємо; інакше
     відмінність лише рахується як would_change (видно на /status, тривога сторожа) і НЕ
     записується (інтеграція, конфлікт 13; rules.guard.allow_changes = false); доказ, що
     зник (визначене значення, а ступені зараз не дають нічого), — окремо, would_change
     _lost (без тривоги). place_area — похідне від district_key і довідника (як row_*),
     переписується. Виправити визначене — лише `places reassign --apply` (свіжий бекап;
     рішення власника);
  3. слабкі ступені (координати, заголовок) — лише якщо ввімкнені й самоперевірка
     точності на відкладених орієнтирах не нижча за поріг;
  4. квартири: places.resolve.property_place з ключів її оголошень → properties
     (district_key, complex_key, place_area, place_conflict), лише змінені;
  5. row_* — dedup._sync_rows (одна точка синхронізації кешу квартири);
  6. запис ops.places_runs (охоплення до/після, ступені, точність, нерозпізнані назви,
     ЖК без району) — його показує /status і GET /api/status/places.

Записи — пакетами ≤ rules.assign.batch_rows рядків у BEGIN IMMEDIATE; у кожному пакеті
відбиток УСІХ інших колонок рядків до й після UPDATE: змінилось щось, крім колонок
місця, — ROLLBACK і стоп (контрольна сума сирих полів — недоторкана). last_seen не
пишеться ніколи (D43).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text

from . import address, extract, geo
from .directory import NONE, Directory
from .normalize import suggest
from .resolve import SRC_TIERS, property_place, resolve

log = logging.getLogger(__name__)

LISTING_PLACE_COLUMNS = ("district_key", "district_how", "complex_key", "complex_how",
                         "place_area", "place_at", "place_sig")
ROW_COLUMNS = ("row_district", "row_complex", "row_area")
WRITTEN_COLUMNS = frozenset(LISTING_PLACE_COLUMNS + ROW_COLUMNS)

_COLUMNS = ("id", "source", "market_type", "property_id", "district", "complex_name",
            "location", "title", "identity", "place_raw", "quality_status", "is_active",
            "manual_active") + LISTING_PLACE_COLUMNS + ROW_COLUMNS


class _Row:
    __slots__ = _COLUMNS + ("active",)

    def __init__(self, values) -> None:
        for name, value in zip(_COLUMNS, values):
            setattr(self, name, value)
        for name in ("identity", "place_raw"):
            v = getattr(self, name)
            if isinstance(v, str):
                try:
                    setattr(self, name, json.loads(v))
                except ValueError:
                    setattr(self, name, None)
        active = self.manual_active if self.manual_active is not None else self.is_active
        self.active = bool(active)


class _Mismatch(Exception):
    pass


@dataclass
class AssignReport:
    dry_run: bool
    rows: int = 0
    filled: Counter = field(default_factory=Counter)
    would_change: Counter = field(default_factory=Counter)
    would_change_examples: list = field(default_factory=list)
    # Визначене значення, якого ступені зараз не дають (доказ зник: агент змінив поле,
    # ЖК без району, мітку села відкинуто) — не тривога, лише число на /status.
    would_change_lost: Counter = field(default_factory=Counter)
    # Мітка села LUN відкинута за будинком (DOM.RIA інших квартир — місто): скільки, приклади.
    area_conflicts: dict = field(default_factory=dict)
    mode: str = "fill"
    reassigned: Counter = field(default_factory=Counter)
    changed_ids: list = field(default_factory=list)
    cas_skipped: int = 0
    tiers: dict = field(default_factory=dict)
    precision: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    unknown: list = field(default_factory=list)
    unlinked: list = field(default_factory=list)
    properties_changed: int = 0
    rows_synced: int = 0
    sig_changed: int = 0
    transactions: int = 0
    max_txn_ms: float = 0.0
    seconds: float = 0.0
    stopped: str | None = None

    @property
    def wrote(self) -> bool:
        return bool(sum(self.filled.values()) or self.properties_changed or self.rows_synced
                    or self.sig_changed or sum(self.reassigned.values()))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sig(view, d: Directory, rules_hash: str) -> str:
    raw = json.dumps([view.inputs(), d.version, rules_hash], ensure_ascii=False,
                     sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# Слова ЖК у заголовку чи описі (для «не в ЖК»): маркери rules.text.markers і відмінки
# («житлового комплексу», «клубному будинку» — рецензія E10: 19 рядків «не в ЖК» з ними).
_ZHK_FORMS = (r"ж\.к\.?", r"житлов\w* комплекс\w*", r"клубн\w* будин\w*")


def _zhk_regex(rules):
    marks = sorted((re.escape(m.casefold()) for m in rules.text.markers), key=len, reverse=True)
    return re.compile(r"(?<![\w/])(?:" + "|".join([*_ZHK_FORMS, *marks]) + r")(?![\w/])")


def _district_vote(r, v, d) -> tuple[str | None, bool]:
    """(корінь району, чи це DOM.RIA) — голос рядка за свій будинок: поле району DOM.RIA
    або мітка LUN (як у рецензії E10), лише назви з довідника."""
    if r.source == "domria":
        for fld, name in v.district_fields:
            kind, key = d.match_district(name)
            if kind == "district":
                return d.root(key), True
    elif r.source == "lun":
        for fld, name in v.district_fields:
            if fld == "lun.label":
                kind, key = d.match_district(name)
                return (d.root(key), False) if kind == "district" else (None, False)
    return None, False


def addr_index(rows, views, results, d: Directory) -> address.Index:
    """Будинки → голоси інших квартир (районі з полів, ЖК «з джерела» першого проходу)."""
    idx = address.Index()
    for r in rows:
        v = views[r.id]
        if v.addr is None:
            continue
        vote, ria = _district_vote(r, v, d)
        res = results[r.id]
        ck = (res.complex_key if res.complex_how in SRC_TIERS
              and res.complex_key not in (None, NONE) else None)
        idx.add(r.id, r.property_id if r.property_id is not None else ("l", r.id), v.addr,
                vote=vote, area_city=bool(vote) and d.area(vote) == "city", ria=ria,
                complex_key=ck)
    return idx


def _load(conn) -> list[_Row]:
    cols = ", ".join(f'"{c}"' for c in _COLUMNS)
    return [_Row(r) for r in conn.execute(text(f"SELECT {cols} FROM listings ORDER BY id"))]


def _descriptions(conn, ids: list[int]) -> dict[int, str]:
    out = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        q = f"SELECT id, description FROM listings WHERE id IN ({', '.join(map(str, chunk))})"
        out.update({i: d for i, d in conn.execute(text(q))})
    return out


def _coverage(rows: list[_Row], keys: dict[int, tuple]) -> dict:
    """Охоплення АКТУАЛЬНИХ оголошень за джерелами: район, ЖК (разом із «не в ЖК»)."""
    by: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if not r.active:
            continue
        dk, _dh, ck, _ch = keys[r.id]
        for scope in (r.source, "_all"):
            c = by[scope]
            c["active"] += 1
            c["district"] += dk is not None
            c["complex"] += ck is not None
            c["complex_named"] += ck is not None and ck != NONE
            c["none"] += ck == NONE
    return {s: dict(c) for s, c in sorted(by.items())}


def _row_coverage(conn) -> dict:
    """Охоплення рядків сайту: квартири з актуальними чистими оголошеннями (одна на рядок)."""
    from ..web.queries import list_ids_select

    ids = list(conn.execute(list_ids_select()).scalars())
    c = Counter()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        q = (f"SELECT row_district, row_complex FROM listings WHERE id IN "
             f"({', '.join(map(str, chunk))})")
        for rd, rc in conn.execute(text(q)):
            c["rows"] += 1
            c["district"] += rd is not None
            c["complex"] += rc is not None
            c["none"] += rc == NONE
    return dict(c)


def _digest(conn, ids: list[int], columns: list[str]) -> str:
    h = hashlib.sha256()
    cols = ", ".join(f'"{c}"' for c in columns)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        q = f"SELECT {cols} FROM listings WHERE id IN ({', '.join(map(str, chunk))}) ORDER BY id"
        for row in conn.execute(text(q)):
            h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def run(*, engine=None, write_engine=None, dry_run: bool = False, d: Directory | None = None,
        rules=None, rules_hash: str = "", measure_weak: bool = False,
        now=None, mode: str = "fill", include_lost: bool = False) -> AssignReport:
    """Один прогін кроку. `engine` — читання; `write_engine` — запис (BEGIN IMMEDIATE).

    `mode="reassign"` — `cli.py places reassign`: визначені ключі, що відрізняються від
    нинішніх ступенів, ПЕРЕПИСУЮТЬСЯ (умова в SQL — старе значення, яке прочитали);
    з `include_lost` — і ті, доказу яких зараз немає (стають NULL). Лише за рішенням
    власника, зі свіжим бекапом (commands.reassign)."""
    from .. import db as dbmod
    from .. import dedup

    started = time.perf_counter()
    eng = engine or dbmod.engine
    weng = write_engine or eng
    now = now or _now()
    rep = AssignReport(dry_run=dry_run, mode=mode)
    zhk_rx = _zhk_regex(rules)
    with eng.connect() as conn:
        rows = _load(conn)
        rep.rows = len(rows)
        before = {r.id: (r.district_key, r.district_how, r.complex_key, r.complex_how)
                  for r in rows}
        rep.coverage["before"] = _coverage(rows, before)
        rep.coverage["rows_before"] = _row_coverage(conn)
        # Кандидати в «не в ЖК»: ринок із rules.secondary.markets і жодної ознаки ЖК у полях.
        views = {r.id: extract.view(r) for r in rows}
        cand = [r.id for r in rows if views[r.id].market in rules.secondary.markets
                and views[r.id].zhk_observed and not views[r.id].signal_complex()]
        texts = _descriptions(conn, cand) if rules.secondary.forbid_zhk_words else {}
    by_id = {r.id: r for r in rows}
    for lid in cand:
        r = by_id[lid]
        blob = f"{r.title or ''}\n{texts.get(lid) or ''}".casefold()
        views[lid].zhk_words = bool(zhk_rx.search(blob))

    # --- 1. ступені з джерела; потім — з доказами будинку (інші квартири) -------------------
    results = {r.id: resolve(views[r.id], d, rules) for r in rows}
    idx = addr_index(rows, views, results, d)
    evidence = {r.id: idx.evidence(r.id) for r in rows if views[r.id].addr is not None}
    for lid, ev in evidence.items():
        results[lid] = resolve(views[lid], d, rules, addr=ev)

    # --- 2. слабкі ступені: координати (самоперевірка точності) і заголовок -----------------
    coords_voters = None
    weak = rules.tiers.coords_enabled or rules.tiers.text_enabled or measure_weak
    if weak:
        coords_voters = _weak_tiers(rows, views, results, d, rules, rep,
                                    apply=not measure_weak or rules.tiers.coords_enabled)
        if rules.tiers.coords_enabled or rules.tiers.text_enabled:
            text_ok = rules.tiers.text_enabled and rep.precision.get("text", {}).get("enabled")
            for r in rows:
                res = results[r.id]
                if res.district_key and res.complex_key:
                    continue
                results[r.id] = resolve(views[r.id], d, rules, coords=coords_voters,
                                        text_enabled=bool(text_ok),
                                        addr=evidence.get(r.id))

    # --- 3. рішення «лише туди, де порожньо» ------------------------------------------------
    unknown: dict[tuple, dict] = {}
    tiers = Counter()
    plans: dict[int, dict] = {}
    after = dict(before)
    for r in rows:
        res, v = results[r.id], views[r.id]
        for fld, name in res.unknown:
            e = unknown.setdefault((fld, name), {"field": fld, "name": name, "all": 0,
                                                 "active": 0, "examples": []})
            e["all"] += 1
            e["active"] += r.active
            if len(e["examples"]) < rules.status.examples and r.active:
                e["examples"].append(r.id)
        if res.district_how:
            tiers[f"district:{res.district_how}"] += 1
        if res.complex_how:
            tiers[f"complex:{res.complex_how}"] += 1
        upd: dict = {}
        dk, dh, ck, ch = before[r.id]
        if res.area_conflict:
            ac = rep.area_conflicts.setdefault("by_label", Counter())
            ac[res.area_conflict] += 1
            rep.area_conflicts["n"] = rep.area_conflicts.get("n", 0) + 1
            rep.area_conflicts["active"] = rep.area_conflicts.get("active", 0) + r.active
            ex = rep.area_conflicts.setdefault("examples", [])
            if r.active and len(ex) < 20:
                ex.append({"id": r.id, "label": res.area_conflict,
                           "district": res.district_key})
        want_dk, want_ck = res.district_key, res.complex_key
        if mode == "reassign":
            if want_dk != dk:
                if want_dk is None and not include_lost:
                    rep.would_change_lost["district"] += 1
                else:
                    upd.update(district_key=want_dk, district_how=res.district_how,
                               g_dk=dk)
                    rep.reassigned["district" if dk is not None else "district_filled"] += 1
                    _example(rep, r.id, "district", dk, want_dk)
                    dk, dh = want_dk, res.district_how
            if want_ck != ck:
                if want_ck is None and not include_lost:
                    rep.would_change_lost["complex"] += 1
                else:
                    upd.update(complex_key=want_ck, complex_how=res.complex_how, g_ck=ck)
                    rep.reassigned["complex" if ck is not None else "complex_filled"] += 1
                    _example(rep, r.id, "complex", ck, want_ck)
                    ck, ch = want_ck, res.complex_how
        else:
            if want_dk and dk is None:
                upd.update(district_key=want_dk, district_how=res.district_how, g_dk=None)
                rep.filled["district"] += 1
                dk, dh = want_dk, res.district_how
            elif dk is not None and want_dk is None:
                rep.would_change_lost["district"] += 1
            elif dk is not None and want_dk != dk:
                rep.would_change["district"] += 1
                _example(rep, r.id, "district", dk, want_dk)
            if want_ck and (ck is None or (ck == NONE and want_ck != NONE)):
                upd.update(complex_key=want_ck, complex_how=res.complex_how, g_ck=ck)
                rep.filled["complex" if want_ck != NONE else "complex_none"] += 1
                ck, ch = want_ck, res.complex_how
            elif ck is not None and want_ck is None:
                rep.would_change_lost["complex"] += 1
            elif ck is not None and want_ck != ck:
                rep.would_change["complex"] += 1
                _example(rep, r.id, "complex", ck, want_ck)
        # Місцевість — похідна від району й довідника (як row_*), не «дані»: рішення
        # власника про село (КАТОТТГ) інакше лишало б оголошенню стару (рецензія E10).
        area = d.area(dk)
        if r.place_area != area:
            upd["place_area"] = area
        sig = _sig(v, d, rules_hash)
        if sig != r.place_sig:
            upd["place_sig"] = sig
            rep.sig_changed += 1
        if upd:
            if set(upd) & {"district_key", "complex_key"}:
                # Формат DateTime SQLAlchemy для SQLite (UPDATE — сирий text(), без типів).
                upd["place_at"] = now.strftime("%Y-%m-%d %H:%M:%S.%f")
            plans[r.id] = upd
        after[r.id] = (dk, dh, ck, ch)
    rep.tiers = dict(sorted(tiers.items()))
    rep.unknown = sorted((e for e in unknown.values() if e["all"] >= rules.status.unknown_min_count),
                         key=lambda e: (-e["active"], -e["all"], e["name"]))[: rules.status.unknown_limit]
    for e in rep.unknown:
        kind = "complex" if e["field"].endswith(("complex_name", "complex", "zhk")) else "district"
        e["suggest"] = suggest(d.name_key(e["name"], kind), d.known_names(kind))
    rep.coverage["after"] = _coverage(rows, after)
    rep.precision.update(_src_precision(rows, results, views, d))
    if "by_label" in rep.area_conflicts:
        rep.area_conflicts["by_label"] = dict(rep.area_conflicts["by_label"].most_common())

    # --- 4. квартири ------------------------------------------------------------------------
    members: dict[int, list] = defaultdict(list)
    for r in rows:
        if r.property_id is not None:
            members[r.property_id].append(after[r.id])
    with eng.connect() as conn:
        stored = {pid: (dk, ck, area, json.loads(conf) if isinstance(conf, str) else conf)
                  for pid, dk, ck, area, conf in conn.execute(text(
                      "SELECT id, district_key, complex_key, place_area, place_conflict "
                      "FROM properties"))}
    prop_plans = []
    for pid, mem in members.items():
        if pid not in stored:
            continue
        want = property_place(mem, d)
        if want != stored[pid]:
            prop_plans.append((pid, want))
    rep.properties_changed = len(prop_plans)
    rep.unlinked = _unlinked(rows, after, d)

    if dry_run:
        rep.seconds = round(time.perf_counter() - started, 2)
        return rep

    # --- 5. запис -----------------------------------------------------------------------------
    batch = rules.assign.batch_rows
    with weng.connect() as c0:
        other = [r[1] for r in c0.execute(text('PRAGMA table_info("listings")'))
                 if r[1] not in WRITTEN_COLUMNS]
    ids = sorted(plans)
    rep.changed_ids = [i for i in ids if set(plans[i]) & {"district_key", "complex_key"}]
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        _write_batch(weng, chunk, other, rep, lambda conn, chunk=chunk: _apply_listing_plans(
            conn, chunk, plans, rep))
    for start in range(0, len(prop_plans), batch):
        chunk = prop_plans[start:start + batch]
        t0 = time.perf_counter()
        with weng.begin() as conn:
            conn.execute(text(
                "UPDATE properties SET district_key = :dk, complex_key = :ck, "
                "place_area = :a, place_conflict = :pc WHERE id = :id"),
                [{"id": pid, "dk": w[0], "ck": w[1], "a": w[2],
                  "pc": json.dumps(w[3], ensure_ascii=False) if w[3] else None}
                 for pid, w in chunk])
        rep.transactions += 1
        rep.max_txn_ms = max(rep.max_txn_ms, (time.perf_counter() - t0) * 1000)
    # row_* — та сама функція, що й у зведенні (dedup._sync_rows); пакетами з відбитком.
    from sqlalchemy.orm import Session

    with Session(bind=weng) as s:
        def wrap(chunk_ids, run_chunk):
            t0 = time.perf_counter()
            conn = s.connection()
            before = _digest(conn, chunk_ids, other)
            run_chunk()
            if _digest(conn, chunk_ids, other) != before:
                s.rollback()
                raise _Mismatch(f"row_*: {chunk_ids[0]}…{chunk_ids[-1]}")
            s.commit()
            rep.transactions += 1
            rep.max_txn_ms = max(rep.max_txn_ms, (time.perf_counter() - t0) * 1000)

        try:
            rep.rows_synced = dedup._sync_rows(s, None, batch=batch, wrap=wrap)
        except _Mismatch as e:
            rep.stopped = str(e)
    with eng.connect() as conn:
        rep.coverage["rows_after"] = _row_coverage(conn)
    rep.seconds = round(time.perf_counter() - started, 2)
    return rep


def _example(rep: AssignReport, lid: int, what: str, old, new) -> None:
    if len(rep.would_change_examples) < 20:
        rep.would_change_examples.append({"id": lid, "field": what, "stored": old, "would": new})


def _apply_listing_plans(conn, chunk: list[int], plans: dict, rep=None) -> None:
    groups: dict[tuple, list] = defaultdict(list)
    for lid in chunk:
        upd = plans[lid]
        groups[tuple(sorted(k for k in upd if not k.startswith("g_")))].append({"id": lid, **upd})
    for cols, params in groups.items():
        sets = ", ".join(f"{c} = :{c}" for c in cols)
        # «Лише туди, де порожньо» — і в самому SQL: ключ змінюється, лише якщо в базі досі
        # те значення, яке крок прочитав (NULL чи '_none' при заповненні; старе — при
        # reassign). Значення, що з'явилось між читанням і записом (інший процес), — ціле.
        guard = []
        if "district_key" in cols:
            guard.append("district_key IS :g_dk")
        if "complex_key" in cols:
            guard.append("complex_key IS :g_ck")
        where = " AND ".join(["id = :id", *guard])
        res = conn.execute(text(f"UPDATE listings SET {sets} WHERE {where}"), params)
        if rep is not None and guard and res.rowcount is not None and res.rowcount >= 0:
            rep.cas_skipped += max(0, len(params) - res.rowcount)


def _write_batch(weng, chunk: list[int], other: list[str], rep: AssignReport, apply) -> None:
    t0 = time.perf_counter()
    with weng.begin() as conn:
        before = _digest(conn, chunk, other)
        apply(conn)
        if _digest(conn, chunk, other) != before:
            raise _Mismatch(f"оголошення {chunk[0]}…{chunk[-1]}")
    rep.transactions += 1
    rep.max_txn_ms = max(rep.max_txn_ms, (time.perf_counter() - t0) * 1000)


def _unlinked(rows: list[_Row], keys: dict, d: Directory) -> list[dict]:
    """ЖК без району в довіднику, що мають актуальні оголошення (для /status)."""
    cnt: Counter = Counter()
    for r in rows:
        ck = keys[r.id][2]
        if r.active and ck and ck != NONE and not d.complex_district(ck):
            cnt[ck] += 1
    return [{"key": k, "name": d.complex_label(k), "active": n} for k, n in cnt.most_common()]


def _src_precision(rows, results, views, d: Directory) -> dict:
    """Узгодженість ступенів «з джерела» між джерелами (точність без ручної вибірки).

    * lun_vs_domria — квартири, де є район з поля DOM.RIA і з мітки/мікрорайону LUN:
      частка збігу за коренем (батько/дитина — збіг); «comparable» — лише мітки LUN,
      що називають район зі словника DOM.RIA (мікрорайони, яких у DOM.RIA немає, —
      Надрічна, Патріот, села — збігтися з ним не можуть за побудовою);
    * complex_vs_agent — оголошення DOM.RIA, де район визначено «за ЖК»: частка, де
      він збігся з районом, який указав агент (решта — «район ЖК сильніший», D57).
    """
    ria: dict[int, Counter] = defaultdict(Counter)
    lun: dict[int, Counter] = defaultdict(Counter)
    agree = total = 0
    for r in rows:
        res = results[r.id]
        if r.property_id is None or not res.district_src:
            continue
        if r.source == "domria":
            ria[r.property_id][d.root(res.district_src)] += 1
        elif r.source == "lun":
            lun[r.property_id][d.root(res.district_src)] += 1
    ria_vocab = {k for c in ria.values() for k in c}
    c_total = c_agree = 0
    for pid, labs in lun.items():
        if pid not in ria:
            continue
        top = ria[pid].most_common(1)[0][0]
        for lab, n in labs.items():
            total += n
            agree += n * (lab == top)
            if lab in ria_vocab:
                c_total += n
                c_agree += n * (lab == top)
    comparable = {"n": c_total, "agree": c_agree,
                  "share": round(c_agree / c_total, 4) if c_total else None}
    c_agree = c_total = 0
    for r in rows:
        res = results[r.id]
        if r.source == "domria" and res.district_how == "complex_src" and res.district_src:
            c_total += 1
            c_agree += d.root(res.district_src) == d.root(res.district_key)
    return {"lun_vs_domria": {"n": total, "agree": agree,
                              "share": round(agree / total, 4) if total else None,
                              "comparable": comparable},
            "complex_vs_agent": {"n": c_total, "agree": c_agree,
                                 "share": round(c_agree / c_total, 4) if c_total else None}}


def _weak_tiers(rows, views, results, d: Directory, rules, rep: AssignReport, *, apply: bool):
    """Координати й заголовок: орієнтири, самоперевірка, увімкнення за порогом."""
    c = rules.coords
    d_refs, c_refs = [], []
    for r in rows:
        v, res = views[r.id], results[r.id]
        if v.geo not in c.precise_geo or v.lat is None:
            continue
        if res.district_how == "src":
            d_refs.append((v.lat, v.lon, res.district_key, r.id))
        if res.complex_how in ("src_id", "src_name") and res.complex_key not in (None, NONE):
            c_refs.append((v.lat, v.lon, res.complex_key, r.id))
    d_index, c_index = geo.PointIndex(d_refs), geo.PointIndex(c_refs)

    def vd(lat, lon, exclude_m=0.0, exclude_id=None):
        return geo.vote_district(d_index, lat, lon, c, d.root, exclude_m=exclude_m,
                                 exclude_id=exclude_id)

    def vc(lat, lon, exclude_m=0.0, exclude_id=None):
        return geo.vote_complex(c_index, lat, lon, c, exclude_m=exclude_m,
                                exclude_id=exclude_id)

    same_root = lambda a, b: d.root(a) == d.root(b)            # noqa: E731
    pd = geo.validate(d_index, d_refs, vd, exclude_m=c.validate_exclude_m,
                      sample=c.validate_sample, same=same_root)
    pc = geo.validate(c_index, c_refs, vc, exclude_m=c.validate_exclude_m,
                      sample=c.validate_sample)
    pd["enabled"] = bool(rules.tiers.coords_enabled and pd["precision"] is not None
                         and pd["precision"] >= c.min_precision_district)
    pc["enabled"] = bool(rules.tiers.coords_enabled and pc["precision"] is not None
                         and pc["precision"] >= c.min_precision_complex)
    rep.precision["coords_district"] = pd
    rep.precision["coords_complex"] = pc
    # Заголовок: у DOM.RIA заголовки без «ЖК» (на копії Етапу 0 — 0 з 10 183), тож
    # точність міряється на оголошеннях LUN/OLX/flombu, у квартирі яких є ЖК «з
    # джерела» іншого оголошення (DOM.RIA, Благо): збіг чи парасолька — правильно.
    from .resolve import _text_complex

    truth: dict[int, Counter] = defaultdict(Counter)
    for r in rows:
        res = results[r.id]
        if (r.property_id is not None and res.complex_how in ("src_id", "src_name")
                and res.complex_key not in (None, NONE)):
            truth[r.property_id][res.complex_key] += 1
    n = ok = 0
    for r in rows:
        res = results[r.id]
        if (r.property_id not in truth or res.complex_how in ("src_id", "src_name")
                or r.source in ("domria", "blago")):
            continue
        got = _text_complex(views[r.id], d, rules)
        if got is None:
            continue
        n += 1
        ok += d.related(got, truth[r.property_id].most_common(1)[0][0]) in ("same", "umbrella")
    pt = {"n": n, "correct": ok, "precision": round(ok / n, 4) if n else None}
    pt["enabled"] = bool(rules.tiers.text_enabled and pt["precision"] is not None
                         and pt["precision"] >= rules.text.min_precision)
    rep.precision["text"] = pt
    if not apply:
        return None
    return (vd if pd["enabled"] else None, vc if pc["enabled"] else None)


def record(rep: AssignReport, *, d: Directory, rules_hash: str, kind: str, status: str,
           message: str | None = None) -> int:
    from .. import ops

    ops.init_ops()
    with ops.ops_session() as s:
        row = ops.PlacesRun(
            status=status, kind=kind, directory_ver=d.version if d else None,
            rules_hash=(rules_hash or "")[:16], rows=rep.rows,
            filled=json.dumps({**dict(rep.filled),
                               **{f"reassign:{k}": v for k, v in rep.reassigned.items()}},
                              ensure_ascii=False),
            # reassign (виправлення за рішенням власника) — не «змінилось би».
            would_change=sum(rep.would_change.values()) if rep.mode != "reassign" else 0,
            would_change_detail=json.dumps({"by_field": dict(rep.would_change),
                                            "examples": rep.would_change_examples,
                                            "lost": dict(rep.would_change_lost),
                                            "area_conflicts": rep.area_conflicts,
                                            "reassigned": dict(rep.reassigned),
                                            "cas_skipped": rep.cas_skipped,
                                            # reassign --apply: які оголошення змінено.
                                            "changed_ids": (rep.changed_ids
                                                            if rep.mode == "reassign" else [])},
                                           ensure_ascii=False, default=str),
            coverage=json.dumps(rep.coverage, ensure_ascii=False),
            tiers=json.dumps(rep.tiers, ensure_ascii=False),
            precision=json.dumps(rep.precision, ensure_ascii=False),
            unknown=json.dumps(rep.unknown, ensure_ascii=False),
            unlinked=json.dumps(rep.unlinked, ensure_ascii=False),
            properties_changed=rep.properties_changed, rows_synced=rep.rows_synced,
            seconds=rep.seconds, message=message, finished_at=ops._now())
        s.add(row)
        s.flush()
        return row.id


def render(rep: AssignReport, d: Directory) -> str:
    """Підсумок для людини (`cli.py places assign`)."""
    lines = ["", "=" * 74, "РАЙОНИ Й ЖК" + ("  (пробний прогін — нічого не записано)"
                                            if rep.dry_run else ""), "=" * 74,
             f"  оголошень: {rep.rows}; довідник {d.version}; {rep.seconds:.1f} с"]
    lines.append("  охоплення АКТУАЛЬНИХ оголошень (район / ЖК разом із «не в ЖК» / з них «не в ЖК»):")
    b, a = rep.coverage.get("before", {}), rep.coverage.get("after", {})
    for src in sorted(set(b) | set(a), key=lambda s: (s == "_all", s)):
        x, y = b.get(src, {}), a.get(src, {})
        n = y.get("active") or x.get("active") or 0
        pct = lambda v: f"{100 * v / n:5.1f}%" if n else "  —  "  # noqa: E731
        lines.append(f"    {('усі' if src == '_all' else src):<8} {n:>6}  район "
                     f"{pct(x.get('district', 0))} → {pct(y.get('district', 0))}   ЖК "
                     f"{pct(x.get('complex', 0))} → {pct(y.get('complex', 0))}   "
                     f"не в ЖК {y.get('none', 0)}")
    for key in ("rows_before", "rows_after"):
        r = rep.coverage.get(key)
        if r and r.get("rows"):
            lines.append(f"  рядки сайту ({'до' if key == 'rows_before' else 'після'}): "
                         f"{r['rows']}, район {100 * r['district'] / r['rows']:.1f}%, ЖК "
                         f"{100 * r['complex'] / r['rows']:.1f}% (не в ЖК {r.get('none', 0)})")
    lines.append(f"  заповнено: {dict(rep.filled) or 0}; квартир змінено: "
                 f"{rep.properties_changed}; row_* синхронізовано: {rep.rows_synced}")
    if rep.mode == "reassign":
        lines.append(f"  ВИПРАВЛЕНО{' б' if rep.dry_run else ''} (reassign): "
                     f"{dict(rep.reassigned) or 0}; без доказу (лишилось як є"
                     f"{'' if rep.dry_run else ', --include-lost не задано'}): "
                     f"{dict(rep.would_change_lost) or 0}")
        for e in rep.would_change_examples[:10]:
            lines.append(f"    #{e['id']} {e['field']}: {e['stored']} → {e['would']}")
    else:
        lines.append(f"  would_change (НЕ застосовано): {dict(rep.would_change) or 0}; доказ "
                     f"зник (без тривоги): {dict(rep.would_change_lost) or 0}")
    if rep.area_conflicts.get("n"):
        lines.append(f"  мітку села LUN відкинуто за будинком (DOM.RIA — місто): "
                     f"{rep.area_conflicts['n']} (актуальних {rep.area_conflicts.get('active', 0)}): "
                     f"{rep.area_conflicts.get('by_label')}")
    if rep.cas_skipped:
        lines.append(f"  не записано (значення змінилось між читанням і записом): {rep.cas_skipped}")
    lines.append(f"  ступені: {rep.tiers}")
    for k, v in rep.precision.items():
        lines.append(f"  точність {k}: {v}")
    lines.append(f"  нерозпізнаних назв: {len(rep.unknown)}; ЖК без району з актуальними "
                 f"оголошеннями: {len(rep.unlinked)}")
    for e in rep.unknown[:15]:
        hint = f" (схоже на «{e['suggest']}»)" if e.get("suggest") else ""
        lines.append(f"    {e['field']:<20} {e['name'][:40]!r:<44} актуальних {e['active']}, "
                     f"усього {e['all']}{hint}")
    if rep.transactions:
        lines.append(f"  транзакцій: {rep.transactions}, найдовша {rep.max_txn_ms:.0f} мс")
    if rep.stopped:
        lines.append(f"  ЗУПИНЕНО: відбиток інших колонок змінився ({rep.stopped}) — ROLLBACK")
    lines.append("=" * 74)
    return "\n".join(lines)
