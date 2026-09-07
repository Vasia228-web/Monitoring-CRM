#!/usr/bin/env python
"""Одноразовий повний збір усієї видачі по всіх п'яти джерелах.

Ізольований від щоденного розкладу: знімає стелю глибини, тримає безпечний
темп і зберігає прогрес, тож перерваний прогін продовжується з тієї сторінки,
на якій зупинився, а не починається спочатку.

    .venv/bin/python scripts/full_scrape.py                 # усі джерела
    .venv/bin/python scripts/full_scrape.py --sources domria
    .venv/bin/python scripts/full_scrape.py --reset         # почати з нуля
    .venv/bin/python scripts/full_scrape.py --dry-run       # тільки план і оцінка
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.config import DATA_DIR, SOURCES, enabled_sources          # noqa: E402
from realty.pipeline import Pipeline                                   # noqa: E402

STATE_FILE = DATA_DIR / "full_scrape_state.json"
log = logging.getLogger("full_scrape")

# Скільки запитів у середньому припадає на сторінку: DIM.RIA додатково тягне
# картку кожного оголошення, тому оцінка часу для нього значно вища.
REQUESTS_PER_PAGE = {"domria": 21, "lun": 1, "flombu": 1, "olx": 1, "blago": 1}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Стан пошкоджено — починаємо спочатку")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def estimate(names: list[str], state: dict) -> tuple[float, list[str]]:
    """Оцінка часу за конфігурацією темпу — без жодного мережевого запиту."""
    total, rows = 0.0, []
    for name in names:
        cfg = SOURCES[name].paced("full")
        done = state.get(name, {}).get("page", 0)
        left = max(0, cfg.max_pages - done)
        secs = left * REQUESTS_PER_PAGE.get(name, 1) * cfg.delay
        total += secs
        rows.append(f"  {name:<8} сторінок {done}/{cfg.max_pages}  темп {cfg.delay}с  "
                    f"≈ {timedelta(seconds=int(secs))}")
    return total, rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Повний історичний збір")
    ap.add_argument("--sources", help="через кому; типово — всі")
    ap.add_argument("--reset", action="store_true", help="ігнорувати збережений прогрес")
    ap.add_argument("--dry-run", action="store_true", help="лише план і оцінка часу")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    names = [s.strip() for s in args.sources.split(",")] if args.sources else enabled_sources()
    state = {} if args.reset else load_state()
    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()

    secs, rows = estimate(names, state)
    print("\nПЛАН ПОВНОГО ЗБОРУ")
    print("=" * 62)
    print("\n".join(rows))
    print("=" * 62)
    print(f"  Орієнтовно: {timedelta(seconds=int(secs))} "
          f"(без урахування сторінок деталей)\n")
    if args.dry_run:
        return 0

    # Прогрес зберігаємо на кожній сторінці — переривання не втрачає години.
    def checkpoint(source: str, page: int) -> None:
        state.setdefault(source, {})["page"] = page
        state[source]["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)

    interrupted = {"flag": False}

    def on_signal(signum, frame):
        if interrupted["flag"]:
            sys.exit(130)
        interrupted["flag"] = True
        log.warning("Переривання: завершую поточне джерело й зберігаю прогрес. "
                    "Ще раз Ctrl+C — вихід негайно.")

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    started = time.monotonic()
    for name in names:
        if interrupted["flag"]:
            break
        if state.get(name, {}).get("done"):
            log.info("%s: уже завершено раніше — пропускаємо", name)
            continue
        cfg = SOURCES[name].paced("full")
        start = state.get(name, {}).get("page", 0)
        log.info("=== %s: сторінки %d..%d, темп %.1f с ===",
                 name, start, cfg.max_pages, cfg.delay)
        try:
            report = Pipeline(sources=[name], use_llm=not args.no_llm, mode="full",
                              start_pages={name: start}, on_page=checkpoint).run()
            state.setdefault(name, {})["done"] = True
            save_state(state)
            print(report.render())
        except Exception:
            log.exception("%s: збір перервано помилкою — прогрес збережено", name)

    print(f"\nЗагальний час: {timedelta(seconds=int(time.monotonic() - started))}")
    print(f"Стан: {STATE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
