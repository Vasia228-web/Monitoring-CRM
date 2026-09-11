"""Зведення аудиту полів: скільки помилок, у яких полях, по яких джерелах."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA = Path(__file__).with_name("_audit_fields.json")


def close(a, b, tol=0.02) -> bool:
    if a is None or b is None:
        return True                    # немає чим звіряти — не помилка
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if a == b:
        return True
    return abs(a - b) <= tol * max(abs(a), abs(b))


def main() -> None:
    rows = json.loads(DATA.read_text())
    checked = [r for r in rows if r.get("truth")]
    errors: dict[str, list] = defaultdict(list)
    by_source: dict[str, Counter] = defaultdict(Counter)
    coverage: dict[str, Counter] = defaultdict(Counter)

    dead = [r for r in rows if r.get("alive") is False]
    failed = [r for r in rows if r.get("error")]

    for r in checked:
        db, truth, src = r["db"], r["truth"], r["source"]
        by_source[src]["звірено"] += 1

        # --- стан ---
        if truth.get("condition"):
            coverage[src]["стан"] += 1
            if db["condition"] != truth["condition"]:
                by_source[src]["стан"] += 1
                errors["стан"].append(r)

        # --- ринок (тільки там, де джерело його заявляє) ---
        if truth.get("market"):
            coverage[src]["ринок"] += 1
            if db["market"] != truth["market"]:
                by_source[src]["ринок"] += 1
                errors["ринок"].append(r)

        # --- кімнатність ---
        if truth.get("rooms") is not None:
            coverage[src]["кімнат"] += 1
            if db["rooms"] != truth["rooms"]:
                by_source[src]["кімнат"] += 1
                errors["кімнат"].append(r)

        # --- площа ---
        if truth.get("area") is not None:
            coverage[src]["площа"] += 1
            if not close(db["area"], truth["area"], 0.03):
                by_source[src]["площа"] += 1
                errors["площа"].append(r)

        # --- ціна ---
        if truth.get("price_usd") is not None:
            coverage[src]["ціна"] += 1
            if not close(db["price_usd"], truth["price_usd"], 0.02):
                by_source[src]["ціна"] += 1
                errors["ціна"].append(r)

    print("=" * 74)
    print("АУДИТ ПОЛІВ: база проти того, що заявляє сайт-джерело")
    print("=" * 74)
    print(f"\nВибірка: {len(rows)}   звірено: {len(checked)}   "
          f"мертвих посилань: {len(dead)}   не вдалось відкрити: {len(failed)}")

    print(f"\n{'поле':<10}{'звірено':>9}{'помилок':>9}{'частка':>9}")
    print("-" * 40)
    for field in ("стан", "ринок", "кімнат", "площа", "ціна"):
        total = sum(c[field] for c in coverage.values())
        bad = len(errors[field])
        share = f"{100 * bad / total:.1f}%" if total else "—"
        print(f"{field:<10}{total:>9}{bad:>9}{share:>9}")

    print(f"\n{'джерело':<10}{'звірено':>9}{'стан':>7}{'ринок':>7}"
          f"{'кімнат':>8}{'площа':>7}{'ціна':>7}")
    print("-" * 56)
    for src, c in sorted(by_source.items(), key=lambda kv: -kv[1]["звірено"]):
        print(f"{src:<10}{c['звірено']:>9}{c['стан']:>7}{c['ринок']:>7}"
              f"{c['кімнат']:>8}{c['площа']:>7}{c['ціна']:>7}")

    for field in ("стан", "ринок", "кімнат", "площа", "ціна"):
        bad = errors[field]
        if not bad:
            continue
        print(f"\n--- {field.upper()}: {len(bad)} помилок, приклади ---")
        for r in bad[:5]:
            db, truth = r["db"], r["truth"]
            key = {"стан": "condition", "ринок": "market", "кімнат": "rooms",
                   "площа": "area", "ціна": "price_usd"}[field]
            print(f"  {r['source']:<7} у базі={db.get(key)}  насправді={truth.get(key)}")
            ev = truth.get("condition_evidence") or truth.get("description", "")
            print(f"          доказ: {ev[:150]}")
            print(f"          {r['url'][:110]}")

    if dead:
        print(f"\n--- МЕРТВІ ПОСИЛАННЯ: {len(dead)} ---")
        for r in dead[:5]:
            print(f"  {r['source']:<7} {r['url'][:110]}")


if __name__ == "__main__":
    main()
