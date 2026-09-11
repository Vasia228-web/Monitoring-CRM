"""Крок 3, вимірювання: чи є в джерел sitemap з переліком актуальних оголошень.

Якщо є — перелічення опублікованого коштуватиме десятки запитів замість
сотень, і різниця снапшотів стане майже безплатною.

Запуск: .venv/bin/python -u probes/p_sitemap.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from realty.config import USER_AGENT

CANDIDATES = {
    "dom.ria.com": ["https://dom.ria.com/robots.txt", "https://dom.ria.com/sitemap.xml"],
    "rieltor.ua": ["https://rieltor.ua/robots.txt", "https://rieltor.ua/sitemap.xml"],
    "olx.ua": ["https://www.olx.ua/robots.txt", "https://www.olx.ua/sitemap.xml"],
    "flombu.com": ["https://flombu.com/robots.txt", "https://flombu.com/sitemap.xml"],
}
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}


def main() -> None:
    with httpx.Client(http2=True, follow_redirects=True, timeout=25.0,
                      headers=HEADERS) as client:
        for host, urls in CANDIDATES.items():
            print(f"\n=== {host} ===")
            for url in urls:
                try:
                    r = client.get(url)
                except Exception as e:                      # noqa: BLE001
                    print(f"  {url:<44} {type(e).__name__}")
                    continue
                body = r.text if r.status_code < 400 else ""
                print(f"  {url:<44} {r.status_code}  {len(r.content)} байт")
                if url.endswith("robots.txt") and body:
                    for line in body.splitlines():
                        if line.lower().startswith("sitemap"):
                            print(f"      → {line.strip()}")
                if url.endswith(".xml") and body:
                    kind = ("індекс карт" if "<sitemapindex" in body
                            else "перелік URL" if "<urlset" in body else "не XML")
                    locs = re.findall(r"<loc>([^<]+)</loc>", body)
                    print(f"      {kind}, {len(locs)} посилань")
                    for loc in locs[:6]:
                        print(f"        {loc[:96]}")
                time.sleep(1.5)


if __name__ == "__main__":
    main()
