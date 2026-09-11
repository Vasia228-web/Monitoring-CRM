"""Крок 1: чи потрібен Chromium для перевірки живості.

Перевірці живості не потрібен вміст сторінки — потрібен лише HTTP-статус.
Питання, на яке відповідає ця проба: чи розрізняє звичайний HTTP-клієнт
мертве й живе оголошення на рівні протоколу, і чи підтримують джерела HEAD.

Запуск: .venv/bin/python probes/p_liveness.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from realty.config import USER_AGENT

DELAY = 2.0          # ввічливо: проба не має сама спровокувати блокування
TIMEOUT = 20.0

# Живі беруться з бази; мертві — відомі зняті плюс завідомо неіснуючі
# ідентифікатори, побудовані за шаблоном кожного джерела.
FABRICATED = {
    "domria": "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-99999999.html",
    "lun": "https://rieltor.ua/ivano-frankovsk/flats-sale/view/99999999/",
    "flombu": "https://flombu.com/uk/estate_deal_sales/99999999",
    "olx": "https://www.olx.ua/d/uk/obyavlenie/neisnuyuche-ogoloshennya-IDzzzzzz.html",
    "blago": "https://blagodeveloper.com/plannings/99999999/",
}

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}


def attempt(client: httpx.Client, method: str, url: str) -> dict:
    try:
        r = client.request(method, url)
    except Exception as e:                       # noqa: BLE001 — тут цікавий сам факт
        return {"status": 0, "error": type(e).__name__, "final": "", "redirects": 0,
                "length": 0}
    return {
        "status": r.status_code,
        "error": "",
        "final": str(r.url),
        "redirects": len(r.history),
        "length": len(r.content) if method == "GET" else int(
            r.headers.get("content-length") or 0),
    }


def live_urls(limit_per_source: int = 3) -> dict[str, list[str]]:
    from sqlalchemy import select

    from realty.db import SessionLocal
    from realty.models import Listing

    out: dict[str, list[str]] = {}
    with SessionLocal() as s:
        for source in FABRICATED:
            rows = s.scalars(
                select(Listing.original_url)
                .where(Listing.source == source, Listing.is_active.is_(True))
                .order_by(Listing.last_seen.desc()).limit(limit_per_source)).all()
            out[source] = list(rows)
    return out


def dead_urls() -> dict[str, list[str]]:
    from sqlalchemy import select

    from realty.db import SessionLocal
    from realty.models import Listing

    out: dict[str, list[str]] = {s: [url] for s, url in FABRICATED.items()}
    with SessionLocal() as s:
        for source, url in s.execute(
                select(Listing.source, Listing.original_url)
                .where(Listing.delisted_at.isnot(None))).all():
            out.setdefault(source, []).append(url)
    return out


def main() -> None:
    live, dead = live_urls(), dead_urls()
    results: list[dict] = []
    with httpx.Client(http2=True, follow_redirects=True, timeout=TIMEOUT,
                      headers=HEADERS) as client:
        for source in FABRICATED:
            for kind, urls in (("живе", live.get(source, [])),
                               ("мертве", dead.get(source, []))):
                for url in urls:
                    for method in ("GET", "HEAD"):
                        r = attempt(client, method, url)
                        r.update(source=source, kind=kind, method=method, url=url)
                        results.append(r)
                        print(f"{source:<8}{kind:<8}{method:<5}"
                              f"{r['status'] or r['error']:<8}"
                              f"redir={r['redirects']}  len={r['length']:<8}"
                              f"{r['final'][:70]}")
                        time.sleep(DELAY)

    print("\n" + "=" * 78)
    print(f"{'джерело':<10}{'метод':<7}{'живе':<24}{'мертве':<24}{'розрізняє?'}")
    print("=" * 78)
    for source in FABRICATED:
        for method in ("GET", "HEAD"):
            def codes(kind):
                return sorted({r["status"] or r["error"] for r in results
                               if r["source"] == source and r["kind"] == kind
                               and r["method"] == method})
            alive, gone = codes("живе"), codes("мертве")
            ok = bool(alive) and bool(gone) and not (set(alive) & set(gone))
            print(f"{source:<10}{method:<7}{str(alive):<24}{str(gone):<24}"
                  f"{'ТАК' if ok else 'ні'}")


if __name__ == "__main__":
    main()
