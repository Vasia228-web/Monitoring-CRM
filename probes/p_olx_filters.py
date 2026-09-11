"""Які параметри фільтрації OLX насправді розуміє.

Замість вгадування імен параметрів читаємо їх зі сторінки: посилання фільтрів
у розмітці містять готові URL.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.fetcher import BrowserFetcher
from realty.sources.olx import CARD_SEL, LIST_URL, RECENT_FIRST


def main() -> None:
    browser = BrowserFetcher(delay=2.8, label="probe")
    try:
        html = browser.render(f"{LIST_URL}?{RECENT_FIRST}", wait_selector=CARD_SEL)
    finally:
        browser.close()

    params = Counter(re.findall(r"search(?:%5B|\[)([a-z_0-9:]+)", html, re.I))
    print("параметри пошуку, що трапляються в розмітці:")
    for name, n in params.most_common(20):
        print(f"  {name:<40}{n}")

    print("\nприклади посилань із фільтрами:")
    links = set(re.findall(r'href="([^"]*search(?:%5B|\[)[^"]*)"', html))
    for link in sorted(links)[:12]:
        print(f"  {link[:150]}")


if __name__ == "__main__":
    main()
