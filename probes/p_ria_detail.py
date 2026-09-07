"""Чи має HTML-сторінка DIM.RIA більше, ніж JSON-картка realty/data/{id}?"""
import sys, json, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bs4 import BeautifulSoup
from realty.fetcher import Fetcher

RID = 34787508
URL = ("https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-"
       "arsenal-konovaltsa-evgeniya-ulitsa-34787508.html")
f = Fetcher(delay=1.0, use_cache=False)

card = f.get_json(f"https://dom.ria.com/realty/data/{RID}", {"lang_id": 4})
print(f"JSON-картка: {len(card)} полів; characteristics_values={card.get('characteristics_values')}")
print(f"теги: {[t.get('tag_synonym') for t in (card.get('tag_uk') or [])]}")

html = f.get(URL)
Path("probes/_ria_detail.html").write_text(html, encoding="utf-8")
print(f"\nHTML: {len(html)} байт")
s = BeautifulSoup(html, "lxml")
txt = s.get_text(" ", strip=True)
for kw in ("Характеристик", "Стан", "ремонт", "Тип пропозиції", "Опалення", "Тип стін", "Рік"):
    i = txt.find(kw)
    print(f"  '{kw}': {'@'+str(i) if i>=0 else 'немає'}")
print("\n--- вузли з двокрапкою (пари) ---")
seen = 0
for el in s.find_all(["li","div","span","p"]):
    t = el.get_text(" ", strip=True)
    if 5 < len(t) < 70 and (":" in t) and not el.find(["li","div","span","p"]):
        print(f"  <{el.name}> class={' '.join(el.get('class',[]))[:34]} :: {t}")
        seen += 1
        if seen >= 14: break
