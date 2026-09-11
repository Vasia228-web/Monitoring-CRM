"""Чому rieltor.ua відмовляє: справа в методі HEAD чи в темпі?

Питання практичне. Якщо сайт не любить саме HEAD — лишимо для нього GET і
втратимо трохи трафіку. Якщо справа в темпі — збільшимо паузу. Якщо ні те,
ні те — доведеться відкочувати й питати.

Проба навмисно дрібна й повільна: вона не має сама створити ту проблему,
яку міряє.

Запуск: .venv/bin/python -u probes/p_rieltor_block.py
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
from realty.models import Listing

N = 8
HEADERS = {"User-Agent": USER_AGENT,
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"}


def urls(n: int) -> list[str]:
    with SessionLocal() as s:
        return list(s.scalars(
            select(Listing.original_url)
            .where(Listing.source == "lun", Listing.is_active.is_(True),
                   Listing.original_url.like("%rieltor.ua%"))
            .order_by(Listing.last_seen.desc()).limit(n * 3)).all())


def run(client: httpx.Client, method: str, sample: list[str], delay: float) -> list[int]:
    codes = []
    for url in sample:
        try:
            codes.append(client.request(method, url).status_code)
        except Exception:
            codes.append(0)
        time.sleep(delay)
    return codes


def main() -> None:
    pool = urls(N)
    print(f"вибірка: {len(pool)} посилань rieltor.ua\n")
    scenarios = [
        ("HEAD", 1.8), ("GET", 1.8), ("HEAD", 5.0), ("GET", 5.0),
    ]
    with httpx.Client(http2=True, follow_redirects=True, timeout=25.0,
                      headers=HEADERS) as client:
        for i, (method, delay) in enumerate(scenarios):
            sample = pool[i * N:(i + 1) * N] or pool[:N]
            codes = run(client, method, sample, delay)
            blocked = sum(1 for c in codes if c in (401, 403, 429))
            print(f"{method:<5} пауза {delay:<4} → {codes}  "
                  f"відмов {blocked}/{len(codes)}")
            print("      пауза 30 с перед наступним сценарієм")
            time.sleep(30)


if __name__ == "__main__":
    main()
