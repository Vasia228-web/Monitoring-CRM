"""Чи можна перелічити всі оголошення OLX однією пагінацією.

Снапшот має сенс лише повний: якщо видача обривається на 25-й сторінці, усе
з наступних виглядатиме «зниклим». Питання просте — чи віддає OLX сторінки
за межею 25 і чи не повторюються на них ті самі картки.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from realty.fetcher import BrowserFetcher
from realty.sources.olx import CARD_SEL, LIST_URL, RECENT_FIRST

PAGES = [1, 24, 25, 26, 30, 40, 50]


def page_url(page: int) -> str:
    params = [RECENT_FIRST] + ([f"page={page}"] if page > 1 else [])
    return f"{LIST_URL}?{'&'.join(params)}"


def main() -> None:
    browser = BrowserFetcher(delay=2.8, label="probe")
    seen: dict[int, set[str]] = {}
    try:
        for page in PAGES:
            url = page_url(page)
            try:
                html = browser.render(url, wait_selector=CARD_SEL)
            except Exception as e:                          # noqa: BLE001
                print(f"  стор. {page:>3}: {type(e).__name__}")
                continue
            cards = BeautifulSoup(html, "lxml").select(CARD_SEL)
            links = set()
            for c in cards:
                a = c.find("a", href=True)
                if a:
                    links.add(a["href"].split("?")[0])
            seen[page] = links
            overlap = len(links & seen.get(1, set())) if page != 1 else 0
            print(f"  стор. {page:>3}: {len(cards):>3} карток, "
                  f"{len(links):>3} унікальних посилань, збіг зі стор.1: {overlap}")
    finally:
        browser.close()

    if 25 in seen and 26 in seen:
        print(f"\n  збіг стор.25 і стор.26: {len(seen[25] & seen[26])}")


if __name__ == "__main__":
    main()
