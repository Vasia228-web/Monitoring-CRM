"""Словник характеристик зі сторінки DIM.RIA (secondaryParams / mainCharacteristics).

JSON-ендпоінт realty/data віддає characteristics_values порожнім, а HTML-сторінка
несе ті самі дані вже з людськими назвами груп і значень.
"""
import sys, json, re, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select, and_, or_
from realty.db import SessionLocal
from realty.fetcher import Fetcher, FetchError
from realty.models import Condition, Listing, MarketType


def extract_state(html: str) -> dict:
    i = html.find("__INITIAL_STATE__")
    if i < 0:
        return {}
    j = html.find("{", i)
    depth, in_str, esc = 0, False, False
    for k in range(j, len(html)):
        c = html[k]
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == '"': in_str = not in_str; continue
        if in_str: continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(html[j:k + 1])
                except json.JSONDecodeError: return {}
    return {}


with SessionLocal() as s:
    urls = list(s.scalars(select(Listing.original_url).where(and_(
        Listing.source == "domria",
        or_(Listing.condition == Condition.UNKNOWN,
            Listing.market_type == MarketType.UNKNOWN))).limit(12)))

f = Fetcher(delay=1.2)
groups, ok = collections.Counter(), 0
for u in urls:
    try:
        st = extract_state(f.get(u))
    except FetchError as e:
        print(f"  {e}"); continue
    realty = ((st.get("listing") or {}).get("data") or {}).get("realty") or {}
    if not realty: continue
    ok += 1
    for grp in realty.get("secondaryParams") or []:
        labels = [i.get("label") for i in (grp.get("items") or [])]
        groups[grp.get("groupName")] += 1
        if ok <= 3:
            print(f"  [{grp.get('groupName')}] -> {labels}")
    if ok <= 1:
        mc = realty.get("mainCharacteristics") or {}
        print("\n  mainCharacteristics.chars:")
        for c in (mc.get("chars") or []):
            print(f"    charId={c.get('charId')} value={c.get('value')}")
        print()
print(f"\nсторінок розібрано: {ok}/{len(urls)}")
print("\nгрупи secondaryParams (частота):")
for g, n in groups.most_common(20):
    print(f"  {n:>3}  {g}")
