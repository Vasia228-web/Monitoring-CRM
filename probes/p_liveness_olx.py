"""Крок 1, продовження: чи можна довіряти HEAD на OLX.

Перша проба показала, що OLX відповідає на HEAD без браузера (GET дає 403).
Але одне оголошення, яке в базі значиться живим, віддало HEAD 410. Перш ніж
прибирати Chromium, треба знати, що це: справжнє зняття, якого не помітив
44-денний обхід, чи брехня HEAD — у другому випадку ми почали б ховати живі
квартири, а це найгірше, що може зробити ця система.

Метод: та сама вибірка перевіряється двома способами — HEAD без браузера і
Chromium, якому ми довіряємо зараз. Розбіжності показуються поштучно.

Запуск: .venv/bin/python -u probes/p_liveness_olx.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import select

from realty.config import USER_AGENT
from realty.db import SessionLocal
from realty.fetcher import BrowserFetcher
from realty.models import Listing

SAMPLE = 20
HEAD_DELAY = 1.5
HEADERS = {"User-Agent": USER_AGENT,
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"}


def head(client: httpx.Client, url: str) -> int:
    try:
        return client.head(url).status_code
    except Exception:
        return 0


def main() -> None:
    with SessionLocal() as s:
        urls = list(s.scalars(
            select(Listing.original_url)
            .where(Listing.source == "olx", Listing.is_active.is_(True))
            .order_by(Listing.last_seen.desc()).limit(SAMPLE)).all())
    print(f"вибірка: {len(urls)} оголошень OLX, які в базі значаться живими\n")

    rows = []
    with httpx.Client(http2=True, follow_redirects=True, timeout=20.0,
                      headers=HEADERS) as client:
        browser = BrowserFetcher(delay=2.0, label="probe")
        try:
            for i, url in enumerate(urls, 1):
                first = head(client, url)
                time.sleep(HEAD_DELAY)
                second = head(client, url)          # чи стабільна відповідь
                time.sleep(HEAD_DELAY)
                via_browser = browser.probe(url, delay=2.0)
                rows.append((url, first, second, via_browser))
                flag = "" if first == second == via_browser else "  ← РОЗБІЖНІСТЬ"
                print(f"{i:>3}. HEAD={first:<4} HEAD2={second:<4} "
                      f"browser={via_browser:<4}{flag}  {url[-52:]}")
        finally:
            browser.close()

    print("\n" + "=" * 70)
    agree = sum(1 for _, a, b, c in rows if a == b == c)
    unstable = [r for r in rows if r[1] != r[2]]
    mismatch = [r for r in rows if r[1] == r[2] and r[1] != r[3]]
    print(f"збіг HEAD і браузера:          {agree} із {len(rows)}")
    print(f"HEAD нестабільний між спробами: {len(unstable)}")
    print(f"HEAD стабільний, але ≠ браузер: {len(mismatch)}")
    for url, a, b, c in unstable + mismatch:
        print(f"   HEAD={a}/{b}  browser={c}  {url}")


if __name__ == "__main__":
    main()
