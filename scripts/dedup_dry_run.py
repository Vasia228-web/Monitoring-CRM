"""Пробний прогін нових правил зведення (D41) на КОПІЇ бази — нічого не пише.

    python scripts/dedup_dry_run.py --db /шлях/до/копії.db [--legacy old_dedup.py]
        [--sample sample.json] [--evidence sample_evidence.json] [--json out.json]

Показує: скільки квартир розщеплює кожне правило окремо і всі разом, як
розкладаються квартири з ручної вибірки, і чи лишаються цілими ті, що за
доказами — справді одна квартира.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from realty import dedup
from realty.models import Base, Listing


def partition(groups) -> dict[int, int]:
    return {i: n for n, g in enumerate(groups) for i in g}


def compare(base: dict[int, int], new: dict[int, int]) -> dict:
    """Скільки груп базового зведення розпалось і скільки нових злилось."""
    members = defaultdict(list)
    for i, g in base.items():
        members[g].append(i)
    split = [g for g, ids in members.items() if len(ids) > 1 and len({new[i] for i in ids}) > 1]
    back = defaultdict(set)
    for i, g in new.items():
        back[g].add(base[i])
    merged = [g for g, olds in back.items() if len(olds) > 1]
    detached = sum(len(members[g]) - Counter(new[i] for i in members[g]).most_common(1)[0][1]
                   for g in split)
    return {"split": len(split), "merged": len(merged), "detached": detached,
            "split_ids": split}


def load_legacy(path: str):
    code = Path(path).read_text().replace("from .models import", "from realty.models import")
    spec = importlib.util.spec_from_loader("legacy_dedup", loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["legacy_dedup"] = mod
    exec(compile(code, path, "exec"), mod.__dict__)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--legacy", help="dedup.py до D41 — для звірки базового режиму")
    ap.add_argument("--sample")
    ap.add_argument("--evidence")
    ap.add_argument("--json")
    args = ap.parse_args()

    engine = create_engine(f"sqlite:///{args.db}", future=True)
    Base.metadata.create_all(engine)            # dedup_decisions на старій копії
    Session = sessionmaker(bind=engine, future=True)
    out: dict = {}
    with Session() as s:
        listings = list(s.scalars(select(Listing)))
        shapes = dedup.load_shapes(s, listings)
        manual = dedup.Manual.load(s)
    by_id = {r.id: r for r in listings}
    current = {r.id: r.property_id for r in listings}
    print(f"оголошень: {len(listings)}; з id квартири DIM.RIA: "
          f"{sum(1 for sh in shapes if sh.flat)}; з корпусом: "
          f"{sum(1 for sh in shapes if sh.korpus)}; з точними координатами: "
          f"{sum(1 for sh in shapes if sh.geo in ('point', 'building'))}")

    out["calibration"] = calibrate(shapes)

    t = time.time()
    base_groups = dedup.cluster(shapes, (), manual)
    base = partition(base_groups)
    print(f"базове зведення (без правил): {len(base_groups)} квартир, {time.time() - t:.0f} с")
    if args.legacy:
        old = load_legacy(args.legacy)
        legacy = partition(old.cluster(
            [old.Shape(*[getattr(sh, f) for f in ("id", "source", "url", "rooms", "area",
                                                   "floor", "street", "house", "district",
                                                   "price")]) for sh in shapes]))
        c = compare(legacy, base)
        print(f"  звірка зі старим кодом: розпалось {c['split']}, злилось {c['merged']}")
        out["legacy_vs_base"] = {k: v for k, v in c.items() if k != "split_ids"}
    have = {i: p for i, p in current.items() if p is not None}
    now_vs = compare(have, {i: base[i] for i in have})
    print(f"  проти поточних квартир у базі: розпалось {now_vs['split']}, злилось {now_vs['merged']}"
          " (нові оголошення з останньої перебудови)")

    print("\nКожне правило окремо (проти базового зведення):")
    out["rules"] = {}
    variants = {}
    for rule in dedup.RULES:
        t = time.time()
        st: dict = {}
        groups = dedup.cluster(shapes, {rule}, manual, st)
        variants[rule] = partition(groups)
        c = compare(base, variants[rule])
        out["rules"][rule] = {**{k: v for k, v in c.items() if k != "split_ids"},
                              "properties": len(groups), "ambiguous": st.get("ambiguous", 0)}
        print(f"  {rule:10s} {dedup.RULE_LABELS[rule][:48]:48s} розщеплено {c['split']:5d} "
              f"(відокремлено оголошень {c['detached']:5d}), зведено нових {c['merged']:4d}"
              f"{'; неоднозначних безадресних ' + str(st['ambiguous']) if st.get('ambiguous') else ''}"
              f"  [{time.time() - t:.0f} с]")
    st: dict = {}
    t = time.time()
    groups = dedup.cluster(shapes, set(dedup.RULES), manual, st)
    allp = partition(groups)
    c = compare(base, allp)
    out["all"] = {**{k: v for k, v in c.items() if k != "split_ids"}, "properties": len(groups),
                  "ambiguous": st.get("ambiguous", 0), "blocked": st.get("blocked", {})}
    print(f"  УСІ РАЗОМ: квартир {len(base_groups)} → {len(groups)}; розщеплено {c['split']}, "
          f"відокремлено оголошень {c['detached']}, зведено нових {c['merged']} [{time.time() - t:.0f} с]")

    big = [g for g in base_groups if len(g) >= 8]
    big_split = sum(1 for g in big if len({allp[i] for i in g}) > 1)
    print(f"  квартир із 8+ оголошеннями: {len(big)}, з них розщеплено всіма правилами: {big_split}")
    out["big"] = {"total": len(big), "split": big_split}

    if args.sample:
        out["sample"] = sample_report(args.sample, args.evidence, current, base, allp, variants)
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


def calibrate(shapes) -> dict:
    """Наскільки правила помиляються на парах, про які DIM.RIA знає відповідь.

    «Одна»: однаковий id квартири DIM.RIA — кожне вето на такій парі було б
    помилковим розщепленням. «Двійнята»: різні id квартири, але та сама
    кімнатність, поверх і площа ±0,6 м² у тому самому будинку — саме такі пари
    старе зведення зливало; добре правило їх розрізняє."""
    rules = [r for r in dedup.RULES if r not in ("ria_flat",)]
    flats = defaultdict(list)
    for sh in shapes:
        if sh.flat:
            flats[sh.flat].append(sh)
    same = [(a, b) for g in flats.values() for i, a in enumerate(g) for b in g[i + 1:]]
    twins = []
    buckets = defaultdict(list)
    for sh in shapes:
        if sh.flat and sh.floor is not None and sh.area:
            buckets[(sh.street, sh.floor, sh.rooms)].append(sh)
    for g in buckets.values():
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                if a.flat != b.flat and abs(a.area - b.area) <= dedup.AREA_TOLERANCE and (
                        not (a.house and b.house) or a.house & b.house):
                    twins.append((a, b))
    res = {"same_pairs": len(same), "twin_pairs": len(twins),
           "twins_merged_by_old_score": sum(1 for a, b in twins
                                            if dedup.match_score(a, b) >= dedup.MERGE_THRESHOLD),
           "false_veto": {}, "twin_caught": {}}
    print(f"\nКалібрування на id квартири DIM.RIA: пар «та сама квартира» {len(same)}, "
          f"«двійнят» (різні квартири, однакові кімнати/поверх/площа в будинку) {len(twins)}, "
          f"з них старе правило зливало {res['twins_merged_by_old_score']}")
    for rule in rules:
        # Сильний зв'язок (той самий id) скасовує м'яке вето — тут міряємо
        # саме правило, тому flat на час перевірки прибираємо.
        fv = sum(1 for a, b in same if dedup.veto(dedup.replace(a, flat=None, url=None),
                                                   dedup.replace(b, flat=None, url=None), {rule}))
        tc = sum(1 for a, b in twins if dedup.veto(dedup.replace(a, flat=None),
                                                    dedup.replace(b, flat=None), {rule}))
        res["false_veto"][rule], res["twin_caught"][rule] = fv, tc
        if rule == "newbuild":
            continue
        print(f"  {rule:10s} помилково розділило б {fv:5d} з {len(same)} "
              f"({100 * fv / max(1, len(same)):4.1f}%), розрізнило б двійнят {tc:5d} з {len(twins)} "
              f"({100 * tc / max(1, len(twins)):4.1f}%)")
    dists = sorted(d for a, b in same if (d := dedup.distance(a, b)) is not None)
    if dists:
        q = lambda p: round(dists[min(len(dists) - 1, int(p * len(dists)))])
        res["same_distance_m"] = {"n": len(dists), "p50": q(.5), "p90": q(.9), "p99": q(.99),
                                  "max": round(dists[-1])}
        print(f"  відстань між координатами однієї квартири: {res['same_distance_m']}")
    return res


VERDICT = {
    "одна": [232, 3007, 3228, 3238], "ймовірно одна": [107], "неясно": [1177, 318, 3630],
    "кілька": [24, 1783, 2529, 2783, 2246, 2289, 3611, 1448, 2201, 3354, 3555, 203],
}


def _norm(t):
    return " ".join(re.findall(r"\w+", (t or "").lower()))[:500]


def _ham(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def evidence_pairs(rows):
    """Пари «точно одна» (id DIM.RIA, фото, опис) і «точно різні» (різні id DIM.RIA)."""
    same, diff = set(), set()
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
            if a.get("flat") and b.get("flat"):
                (same if a["flat"] == b["flat"] else diff).add(key)
                continue
            if a.get("hash") and b.get("hash") and _ham(a["hash"], b["hash"]) <= 6:
                same.add(key)
            da = _norm(a.get("description") or a.get("db_description"))
            db = _norm(b.get("description") or b.get("db_description"))
            if len(da) > 80 and len(db) > 80 and SequenceMatcher(None, da, db).ratio() >= 0.9:
                same.add(key)
    return same, diff


def sample_report(sample_path, evidence_path, current, base, allp, variants):
    sample = json.loads(Path(sample_path).read_text())["sample"]
    evidence = {p["property_id"]: p["listings"] for p in
                json.loads(Path(evidence_path).read_text())} if evidence_path else {}
    verdict = {pid: v for v, ids in VERDICT.items() for pid in ids}
    print("\nРучна вибірка 20 квартир (22.09): як розклались усіма правилами")
    rows_out = []
    for prop in sample:
        pid = prop["property_id"]
        ids = [l["id"] for l in prop["listings"] if l["id"] in allp]
        parts = Counter(allp[i] for i in ids)
        sizes = sorted(parts.values(), reverse=True)
        same, diff = evidence_pairs(evidence.get(pid, []))
        broken_same = sum(1 for a, b in same if a in allp and b in allp and allp[a] != allp[b])
        kept_diff = sum(1 for a, b in diff if a in allp and b in allp and allp[a] == allp[b])
        by_rule = {r: len({v[i] for i in ids}) for r, v in variants.items()
                   if len({v[i] for i in ids}) > 1}
        rows_out.append({"property_id": pid, "verdict": verdict.get(pid), "listings": len(ids),
                         "parts": sizes, "evidence_same": len(same), "same_split": broken_same,
                         "evidence_diff": len(diff), "diff_still_together": kept_diff,
                         "by_rule": by_rule})
        print(f"  /property/{pid:<5d} {verdict.get(pid, '?'):14s} {len(ids):3d} огол. → "
              f"{len(sizes)} частин {sizes[:8]}{'…' if len(sizes) > 8 else ''} | "
              f"доведено-одна пар {len(same)}, з них розірвано {broken_same} | "
              f"доведено-різні пар {len(diff)}, досі разом {kept_diff}"
              f"{' | правила: ' + ', '.join(f'{r}→{n}' for r, n in by_rule.items()) if by_rule else ''}")
    ones = [r for r in rows_out if r["verdict"] == "одна"]
    print("  «одна» цілі: " + ", ".join(
        f"{r['property_id']}: {'так' if len(r['parts']) == 1 else 'НІ ' + str(r['parts'])}" for r in ones))
    return rows_out


if __name__ == "__main__":
    sys.exit(main())
