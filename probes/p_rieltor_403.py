"""403 від rieltor.ua — це обмеження темпу чи відповідь про конкретне оголошення?

Різниця принципова. Якщо це темп — треба збільшити паузу. Якщо це відповідь
саме про ці оголошення, то темп ні до чого, і збільшення паузи нічого не дасть,
а ми просто ніколи не дізнаємось долю цих записів.

Метод: беремо ту саму чергу, яку бере перевірка, ідемо по ній повільно й
поодинці, а потім повторюємо ті, що дали 403 — з великою паузою і в іншому
порядку. Якщо 403 липне до тих самих посилань — справа не в темпі.

Запуск: .venv/bin/python -u probes/p_rieltor_403.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from realty.config import USER_AGENT
from realty.db import SessionLocal
from realty.verify import collect

DELAY = 3.0
HEADERS = {"User-Agent": USER_AGENT,
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"}


def main() -> None:
    with SessionLocal() as s:
        queue = collect(s, limit_per_host=14, hosts=["rieltor.ua"])["rieltor.ua"]
    print(f"черга: {len(queue)} посилань, пауза {DELAY} с, послідовно\n")

    first: dict[str, int] = {}
    with httpx.Client(http2=True, follow_redirects=True, timeout=25.0,
                      headers=HEADERS) as client:
        for i, item in enumerate(queue, 1):
            try:
                code = client.head(item.url).status_code
            except Exception as e:                          # noqa: BLE001
                code = 0
                print(f"    {type(e).__name__}")
            first[item.url] = code
            print(f"{i:>3}. HEAD={code:<5}{item.url}")
            time.sleep(DELAY)

        suspects = [u for u, c in first.items() if c in (401, 403, 429)]
        clean = [u for u, c in first.items() if c == 200][:3]
        if not suspects:
            print("\n403 не відтворився — значить, це був темп")
            return

        print(f"\nпауза 60 с, потім повторюємо {len(suspects)} підозрілих "
              f"і {len(clean)} чистих упереміш")
        time.sleep(60)
        order = [u for pair in zip(suspects, clean + suspects) for u in pair]
        for url in dict.fromkeys(order):
            try:
                code = client.head(url).status_code
            except Exception:
                code = 0
            mark = "підозрілий" if url in suspects else "чистий    "
            print(f"  {mark} було={first[url]:<5} стало={code:<5}{url[-46:]}")
            time.sleep(DELAY)


if __name__ == "__main__":
    main()
