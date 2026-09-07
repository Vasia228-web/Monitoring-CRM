#!/usr/bin/env python
"""Точка входу: збір даних і запуск веб-інтерфейсу.

  python cli.py scrape                 # зібрати з усіх джерел
  python cli.py scrape --sources olx,lun --pages 3
  python cli.py backfill               # дозібрати записи з прогалинами
  python cli.py schedule install       # фоновий розклад (launchd)
  python cli.py dedup                  # звести дублі між сайтами
  python cli.py verify --limit 200     # перевірити, які оголошення ще живі
  python cli.py serve --port 8000      # веб-інтерфейс
  python cli.py stats                  # що вже є в базі
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from realty.config import SOURCES, SourceConfig


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_scrape(args: argparse.Namespace) -> int:
    if args.pages:
        for name, cfg in SOURCES.items():
            SOURCES[name] = SourceConfig(
                name, cfg.enabled, args.pages, cfg.delay, cfg.needs_browser, cfg.extra
            )
    from realty.pipeline import Pipeline

    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    report = Pipeline(sources=names, use_llm=not args.no_llm, mode=args.mode,
                      trigger=args.trigger).run()
    print(report.render())
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from realty.db import init_db
    from realty.verify import CHECKABLE, verify_batch

    init_db()
    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    st = verify_batch(limit=args.limit, sources=names)
    print("\n" + "=" * 52)
    print("ПЕРЕВІРКА АКТУАЛЬНОСТІ")
    print("=" * 52)
    print(f"  перевірено:        {st['checked']}")
    print(f"  живі:              {st['alive']}")
    print(f"  знято з продажу:   {st['delisted']}")
    print(f"  повернулись:       {st['restored']}")
    print(f"  без висновку:      {st['unknown']}")
    if st.get("skipped_blocked"):
        print(f"  пропущено:         {st['skipped_blocked']} "
              f"(джерело відмовляє: {', '.join(st['blocked_sources'])})")
    print("=" * 52)
    print(f"  (blago не перевіряється: сайт не відрізняє видалене планування)")
    return 0


def cmd_dedup(args: argparse.Namespace) -> int:
    from realty.db import init_db, session_scope
    from realty.dedup import rebuild

    init_db()
    with session_scope() as s:
        st = rebuild(s, dry_run=args.dry_run)
    print("\n" + "=" * 58)
    print("МІЖПЛАТФОРМНА ДЕДУПЛІКАЦІЯ" + ("  (пробний прогін)" if args.dry_run else ""))
    print("=" * 58)
    print(f"  оголошень:            {st['listings']}")
    print(f"  унікальних об'єктів:  {st['properties']}")
    print(f"  об'єднано груп:       {st['merged_groups']} "
          f"({st['merged_listings']} оголошень)")
    print(f"  з них між джерелами:  {st['cross_source']}")
    print("=" * 58)
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from realty import scheduler

    if args.action == "install":
        print(scheduler.install(int(args.hours * 3600)))
    elif args.action == "worker":
        print(f"Воркер запущено: прогін кожні {args.hours} год. Ctrl+C — зупинити.")
        return scheduler.worker(int(args.hours * 3600), runs=args.runs)
    elif args.action == "uninstall":
        print(scheduler.uninstall())
    elif args.action == "cron":
        print("Рядок для crontab (crontab -e):")
        print("  " + scheduler.cron_line(int(args.hours)))
    else:
        print(scheduler.status())
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    from realty.pipeline import Pipeline

    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    report = Pipeline(sources=names, use_llm=not args.no_llm).backfill(limit=args.limit)
    print(report.render())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"Веб-інтерфейс: http://{args.host}:{args.port}")
    uvicorn.run("realty.web.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from realty.db import SessionLocal, init_db
    from realty.models import Condition, Listing, MarketType

    init_db()
    with SessionLocal() as s:
        total = s.scalar(select(func.count(Listing.id))) or 0
        print(f"Усього оголошень: {total}")
        if not total:
            print("База порожня — запустіть: python cli.py scrape")
            return 0
        for label, col in (("Джерела", Listing.source),
                           ("Стан", Listing.condition),
                           ("Ринок", Listing.market_type),
                           ("Кімнат", Listing.rooms)):
            print(f"\n{label}:")
            for val, n in s.execute(select(col, func.count(Listing.id))
                                    .group_by(col).order_by(func.count(Listing.id).desc())):
                name = val.label if isinstance(val, (Condition, MarketType)) else val
                print(f"  {str(name or '—'):<22} {n}")
        avg = s.scalar(select(func.avg(Listing.price_per_sqm))
                       .where(Listing.price_per_sqm.isnot(None)))
        print(f"\nСередня ціна за м²: ${avg:.0f}" if avg else "")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Агрегатор нерухомості Івано-Франківська")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="зібрати оголошення")
    sc.add_argument("--sources", help="через кому: domria,lun,olx,flombu,blago")
    sc.add_argument("--pages", type=int, help="сторінок на джерело")
    sc.add_argument("--no-llm", action="store_true", help="вимкнути LLM-фолбек")
    sc.add_argument("--trigger", default="cli",
                    choices=("cli", "manual", "schedule"),
                    help="звідки запущено — для телеметрії дашборда")
    sc.add_argument("--mode", choices=("fresh", "full"), default="fresh",
                    help="fresh — тільки нові оголошення; full — без стелі глибини")
    sc.set_defaults(func=cmd_scrape)

    bf = sub.add_parser("backfill", help="дозібрати наявні записи з прогалинами")
    bf.add_argument("--sources", help="через кому: domria,lun,olx,flombu,blago")
    bf.add_argument("--limit", type=int, default=300)
    bf.add_argument("--no-llm", action="store_true")
    bf.set_defaults(func=cmd_backfill)

    vf = sub.add_parser("verify", help="перевірити, які оголошення ще живі")
    vf.add_argument("--limit", type=int, default=200, help="скільки перевірити за раз")
    vf.add_argument("--sources", help="через кому; типово — усі, що вміємо перевіряти")
    vf.set_defaults(func=cmd_verify)

    dd = sub.add_parser("dedup", help="звести однакові квартири в майстер-записи")
    dd.add_argument("--dry-run", action="store_true", help="лише порахувати, без запису")
    dd.set_defaults(func=cmd_dedup)

    sh = sub.add_parser("schedule", help="фоновий розклад збору")
    sh.add_argument("action", nargs="?", default="status",
                    choices=("status", "install", "uninstall", "cron", "worker"))
    sh.add_argument("--hours", type=float, default=3, help="інтервал у годинах")
    sh.add_argument("--runs", type=int, help="обмежити кількість прогонів (для воркера)")
    sh.set_defaults(func=cmd_schedule)

    sv = sub.add_parser("serve", help="запустити веб-інтерфейс")
    sv.add_argument("--host", default="127.0.0.1")
    # Порт може задати середовище (PORT) — так його передає планувальник
    # прев'ю. Явний --port лишається пріоритетним.
    sv.add_argument("--port", type=int,
                    default=int(os.getenv("PORT", "8000")))
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    st = sub.add_parser("stats", help="підсумки по базі")
    st.set_defaults(func=cmd_stats)

    args = p.parse_args()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
