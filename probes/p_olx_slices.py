"""Як розрізати видачу OLX, щоб кожен зріз уміщався в 25 сторінок.

OLX обриває пагінацію на 25-й сторінці (~1100 оголошень), а в місті їх 2488.
Отже, перелік має складатися з кількох звужених запитів. Перевіряємо, чи
працює фільтр за кімнатністю і скільки оголошень дає кожен зріз.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from realty.fetcher import BrowserFetcher
from realty.sources.olx import CARD_SEL, LIST_URL, RECENT_FIRST

ROOMS = {"one": "1", "two": "2", "three": "3", "four": "4+"}
COUNT_RE = re.compile(r"([\d\s ]+)\s*оголошен", re.I)


def slice_url(value: str | None) -> str:
    params = [RECENT_FIRST]
    if value:
        params.append(f"search%5Bfilter_enum_number_of_rooms%5D%5B0%5D={value}")
    return f"{LIST_URL}?{'&'.join(params)}"


def main() -> None:
    browser = BrowserFetcher(delay=2.8, label="probe")
    try:
        for value, label in [(None, "без фільтра")] + list(ROOMS.items()):
            url = slice_url(value)
            try:
                html = browser.render(url, wait_selector=CARD_SEL)
            except Exception as e:                          # noqa: BLE001
                print(f"  {label:<12} {type(e).__name__}")
                continue
            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(CARD_SEL)
            text = soup.get_text(" ", strip=True)
            m = COUNT_RE.search(text)
            total = m.group(1).strip() if m else "?"
            pages = re.findall(r'data-testid="pagination-link-(\d+)"', html)
            last = max((int(p) for p in pages), default=0)
            print(f"  {label:<12} карток {len(cards):>3}  "
                  f"усього за фільтром: {total:<8} остання видима стор.: {last}")
    finally:
        browser.close()


if __name__ == "__main__":
    main()
