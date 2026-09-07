"""Верифікація rieltor.ua — сторінок, на які веде original_url оголошень LUN."""
import sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bs4 import BeautifulSoup
from sqlalchemy import select
from realty.db import SessionLocal
from realty.fetcher import Fetcher
from realty.models import Listing

with SessionLocal() as s:
    urls = list(s.scalars(select(Listing.original_url).where(
        Listing.source == "lun", Listing.original_url.like("%rieltor.ua%")).limit(12)))

f = Fetcher(delay=1.2)
keys, states, rows_vocab = collections.Counter(), collections.Counter(), collections.Counter()
ok = 0
for u in urls:
    try:
        soup = BeautifulSoup(f.get(u), "lxml")
    except Exception as e:
        print(f"  ПОМИЛКА {type(e).__name__}: {u[-30:]}"); continue
    ok += 1
    for el in soup.select(".offer-view-planning-text"):
        t = el.get_text(" | ", strip=True)
        if "|" in t:
            k, _, v = t.partition("|")
            keys[k.strip().rstrip(":")] += 1
            if "стан" in k.lower():
                states[v.strip()] += 1
    for el in soup.select(".offer-view-details-row"):
        rows_vocab[el.get_text(" ", strip=True)[:40]] += 1
print(f"сторінок завантажено: {ok}/{len(urls)}\n")
print("ключі .offer-view-planning-text:")
for k, n in keys.most_common(20): print(f"  {n:>3}  {k}")
print("\nзначення «Загальний стан квартири»:")
for k, n in states.most_common(): print(f"  {n:>3}  {k}")
print("\nчасті значення .offer-view-details-row:")
for k, n in rows_vocab.most_common(14): print(f"  {n:>3}  {k}")
