"""Повний словник параметрів картки OLX — чи є серед них стан/ремонт?"""
import sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bs4 import BeautifulSoup
from sqlalchemy import and_, select
from realty.db import SessionLocal
from realty.fetcher import BrowserFetcher, FetchError
from realty.models import Condition, Listing
from realty.sources.olx import DETAIL, _PARAM_RE

with SessionLocal() as s:
    urls = list(s.scalars(select(Listing.original_url).where(and_(
        Listing.source == "olx", Listing.condition == Condition.UNKNOWN,
        Listing.detail_enriched.is_(True))).limit(10)))

keys, values_by_key, ok = collections.Counter(), collections.defaultdict(collections.Counter), 0
with BrowserFetcher(delay=2.0) as b:
    for u in urls:
        try:
            html = b.render(u, settle_ms=2000)
        except FetchError as e:
            print(f"  {e}"); continue
        ok += 1
        box = BeautifulSoup(html, "lxml").select_one(DETAIL["params"])
        if not box:
            continue
        for node in box.find_all(["p", "li", "span"]):
            if node.find(["p", "li", "span"]):
                continue
            m = _PARAM_RE.match(node.get_text(" ", strip=True))
            if m:
                k = m.group(1).strip()
                keys[k] += 1
                values_by_key[k][m.group(2).strip()[:40]] += 1

print(f"\nсторінок: {ok}/{len(urls)}")
print("\nусі ключі параметрів:")
for k, n in keys.most_common(30):
    print(f"  {n:>3}  {k}")
print("\nзначення ключів, дотичних до стану:")
for k in keys:
    if any(t in k.lower() for t in ("стан", "ремонт", "оздобл", "обробк", "меблюв")):
        print(f"  [{k}] -> {dict(values_by_key[k])}")
