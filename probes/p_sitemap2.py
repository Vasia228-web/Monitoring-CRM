"""Крок 3: чи придатні карти сайту для переліку актуальних оголошень міста.

Наявність карти ще нічого не означає. Важливо інше: чи є в ній окремі
оголошення, чи можна звузити до Івано-Франківська і скільки це важить. Карта
на всю країну в десятки мегабайтів була б дорожчою за пагінацію пошуку.
"""
from __future__ import annotations

import gzip
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from realty.config import USER_AGENT

HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
TARGETS = [
    ("dom.ria.com", "https://dom.ria.com/prodazha-kvartir/sitemap.xml"),
    ("rieltor.ua", "https://rieltor.ua/sitemap/sitemap.xml"),
]


def body(r: httpx.Response) -> str:
    raw = r.content
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def main() -> None:
    with httpx.Client(http2=True, follow_redirects=True, timeout=40.0,
                      headers=HEADERS) as client:
        for host, url in TARGETS:
            print(f"\n=== {host} — {url}")
            try:
                r = client.get(url)
            except Exception as e:                          # noqa: BLE001
                print(f"  {type(e).__name__}")
                continue
            print(f"  {r.status_code}, {len(r.content)} байт")
            if r.status_code >= 400:
                continue
            text = body(r)
            locs = re.findall(r"<loc>([^<]+)</loc>", text)
            index = "<sitemapindex" in text
            print(f"  {'індекс карт' if index else 'перелік URL'}: {len(locs)} посилань")
            frank = [u for u in locs if "frank" in u.lower()]
            print(f"  зі згадкою Франківська: {len(frank)}")
            for u in (frank or locs)[:8]:
                print(f"    {u[:110]}")
            time.sleep(1.5)

            # Зазираємо на рівень глибше в найперспективніше посилання.
            deeper = (frank or locs)[:1]
            for sub in deeper:
                if not sub.endswith((".xml", ".xml.gz")):
                    continue
                try:
                    rr = client.get(sub)
                except Exception as e:                      # noqa: BLE001
                    print(f"    → {type(e).__name__}")
                    continue
                sub_text = body(rr)
                sub_locs = re.findall(r"<loc>([^<]+)</loc>", sub_text)
                city = [u for u in sub_locs if "frank" in u.lower()]
                print(f"    → {rr.status_code}, {len(rr.content)} байт, "
                      f"{len(sub_locs)} посилань, з них по Франківську {len(city)}")
                for u in (city or sub_locs)[:4]:
                    print(f"        {u[:110]}")
                time.sleep(1.5)


if __name__ == "__main__":
    main()
