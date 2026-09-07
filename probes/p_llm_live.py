"""Жива перевірка LLM-фолбеку.

Бере реальні оголошення, у яких парсер уже все дістав, приховує критичні поля
й пропонує моделі відновити їх із очищеного тексту сторінки. Так видно і те,
що виклик працює, і те, наскільки відповідь збігається з фактом.

Потребує ANTHROPIC_API_KEY у .env. Запуск:
    .venv/bin/python probes/p_llm_live.py [кількість]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from sqlalchemy import select

from realty.config import LLM_MAX_CHARS, LLM_MODEL
from realty.db import SessionLocal
from realty.llm import LLMExtractor, clean_html
from realty.models import Listing
from realty.normalize import to_usd
from realty.pipeline import Pipeline

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
if not LLMExtractor().available:
    sys.exit("Немає ANTHROPIC_API_KEY — додайте його в .env і повторіть.")

with SessionLocal() as s:
    rows = s.scalars(
        select(Listing).where(Listing.source == "olx",
                              Listing.rooms.isnot(None),
                              Listing.area_total.isnot(None),
                              Listing.price.isnot(None)).limit(N)
    ).all()
    facts = [{"url": r.original_url, "price": r.price, "currency": r.currency,
              "rooms": r.rooms, "area_total": r.area_total} for r in rows]

pipe = Pipeline(use_llm=True)
print(f"\nМодель: {LLM_MODEL} · стеля тексту: {LLM_MAX_CHARS} символів")
print("=" * 72)

hits = total = 0
for fact in facts:
    html = pipe._page_text({"source": "olx", "original_url": fact["url"]})
    if not html:
        print(f"\n{fact['url'][-40:]}: сторінку не завантажено")
        continue
    text = clean_html(html)
    got = pipe.llm.extract(html, fact["url"])
    print(f"\n{fact['url'][-46:]}")
    print(f"  надіслано: {len(text)} символів з {len(html)} байт HTML")
    if got is None:
        print("  модель не відповіла")
        continue
    # Ціну звіряємо в доларах: сайт показує і гривні, і долари, тож модель
    # цілком законно може повернути іншу валюту, ніж вибрав парсер.
    want_usd = to_usd(fact["price"], fact["currency"])
    have_usd = to_usd(got.price, got.currency or "USD")
    checks = [
        ("price, $", want_usd, have_usd, max(1.0, (want_usd or 0) * 0.02)),
        ("rooms", fact["rooms"], got.rooms, 0.01),
        ("area_total", fact["area_total"], got.area_total, 0.51),
    ]
    for name, want, have, tol in checks:
        ok = want is not None and have is not None and abs(float(want) - float(have)) <= tol
        hits += ok
        total += 1
        w = f"{want:.0f}" if isinstance(want, float) else str(want)
        h = f"{have:.0f}" if isinstance(have, float) else str(have)
        print(f"  {name:<11} факт={w:<12} модель={h:<12} {'збіг' if ok else 'РОЗБІЖНІСТЬ'}")
    print(f"  {'location':<11} модель={got.location}")
    print(f"  {'condition':<11} модель={got.condition} · market={got.market_type}")

print("\n" + "=" * 72)
print(f"Збігів: {hits}/{total} · викликів: {pipe.llm.calls} · "
      f"надіслано символів: {pipe.llm.sent_chars}")
for obj in (pipe._http, pipe._browser):
    if obj is not None:
        obj.close()
