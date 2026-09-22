#!/usr/bin/env python
"""Точка входу: збір даних і запуск веб-інтерфейсу.

  python cli.py cycle                  # регулярний цикл із лімітами часу (для розкладу)
  python cli.py backup                 # бекап бази з перевіркою відновлення
  python cli.py watchdog               # сигнал тиші: Telegram, якщо системі погано
  python cli.py tunnel                 # доступ ззовні через Cloudflare Tunnel
  python cli.py scrape                 # зібрати з усіх джерел
  python cli.py scrape --sources olx,lun --pages 3
  python cli.py backfill               # дозібрати записи з прогалинами
  python cli.py schedule install       # фоновий розклад (launchd)
  python cli.py dedup                  # звести дублі між сайтами
  python cli.py verify                 # перевірити, які оголошення ще живі
  python cli.py snapshot               # знайти зниклі різницею списків
  python cli.py quality diagnose       # що не так із даними
  python cli.py quality audit          # аудит дедуплікації
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


def cmd_cycle(args: argparse.Namespace) -> int:
    """Регулярний цикл: кожен крок — окремий процес зі стелею часу."""
    from realty import runner

    names = [x.strip() for x in args.sources.split(",")] if args.sources else None
    kwargs = {}
    if args.run_timeout:
        kwargs["run_timeout"] = args.run_timeout * 60
    result = runner.run_cycle(trigger=args.trigger, sources=names,
                              tasks=not args.only_sources, **kwargs)
    print(runner.render(result))
    # Ненульовий код — лише коли сам диригент не зміг відпрацювати. «Нічого не
    # зібрано» — це стан системи, його бачить сигнал тиші, а не systemd.
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from pathlib import Path

    from realty import backup

    if args.action == "verify":
        r = backup.verify_archive(Path(args.file))
        print(f"цілісність: {r['integrity']}")
        print(f"рядків:     {r['rows']}")
        print(f"маніфест:   {r['manifest']['rows']}  ({r['manifest']['created']})")
        print("ВІДНОВЛЕННЯ ЗБІГЛОСЬ" if r["match"] else "НЕ ЗБІГЛОСЬ")
        return 0 if r["match"] else 1
    if args.action == "restore":
        r = backup.restore(Path(args.file), Path(args.target))
        print(f"розгорнуто в {args.target}: {r['rows']}")
        return 0
    if args.action == "status":
        last = backup.last_attempt()
        if last is None:
            print("бекапів ще не було")
            return 0
        print(f"остання спроба: {last.created_at:%Y-%m-%d %H:%M} UTC — {last.status}")
        print(f"  файл: {last.file} ({last.size / 1e6:.1f} МБ), відновлення: "
              f"{'так' if last.restored_ok else 'ні'}")
        print(f"  поза машиною: {last.offsite or '—'}")
        if last.message:
            print(f"  проблеми: {last.message}")
        return 0

    if args.if_due and not backup.is_due():
        print(f"бекап не потрібен: останній успішний {backup.last_success_at():%Y-%m-%d %H:%M} UTC")
        return 0
    res = backup.run(upload=not args.no_upload)
    print(f"\nБЕКАП: {res.status.upper()}")
    print(f"  файл:         {res.file} ({res.size / 1e6:.1f} МБ)")
    print(f"  рядків:       {res.rows}")
    print(f"  відновлення:  {'збіглось' if res.restored_ok else 'НЕ перевірено/не збіглось'}")
    print(f"  поза машиною: {', '.join(res.offsite) or '—'}")
    if res.pruned:
        print(f"  прибрано старих: {len(res.pruned)}")
    for p in res.problems:
        print(f"  ! {p}")
    return 0 if res.status in ("ok", "local") else 1


def cmd_watchdog(args: argparse.Namespace) -> int:
    from realty import notify, watchdog

    if not notify.configured():
        print("Telegram не налаштовано: задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у .env")
        return 2
    if args.test:
        msg_id = watchdog.test_message()
        print(f"тестове повідомлення прийняте Telegram, message_id={msg_id}")
        return 0
    rep = watchdog.run()
    print(f"активні: {rep['active'] or '—'}")
    print(f"надіслано: {rep['sent'] or '—'}   притримано: {rep['held'] or '—'}   "
          f"відновилось: {rep['resolved'] or '—'}")
    for e in rep["errors"]:
        print(f"  ! {e}")
    return 1 if rep["errors"] else 0


def cmd_tunnel(args: argparse.Namespace) -> int:
    from realty import tunnel

    if args.plan:
        p = tunnel.plan()
        print(p.get("error") or f"режим: {p['mode']}, адреса: {p['url'] or 'видасть Cloudflare'}")
        return tunnel.EX_CONFIG if "error" in p else 0
    return tunnel.run()


def cmd_schema(args: argparse.Namespace) -> int:
    """Перевірка й ремонт зовнішніх ключів робочої бази."""
    import sqlite3
    from pathlib import Path

    from realty import schema_repair
    from realty.config import DB_URL

    path = Path(args.db or DB_URL.removeprefix("sqlite:///"))
    if args.action == "check":
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        bad = schema_repair.find_dangling(con)
        print(f"база: {path}")
        print("биті зовнішні ключі:", [f"{d.table}.{d.column}→{d.missing}" for d in bad] or "немає")
        print("foreign_key_check:", con.execute("PRAGMA foreign_key_check").fetchall()
              if not bad else "пропущено: є биті ключі")
        print("integrity_check:", con.execute("PRAGMA integrity_check").fetchone()[0])
        return 1 if bad else 0

    rep = schema_repair.repair(path, apply=not args.dry_run)
    print(f"база: {path}   режим: {'проба (відкат)' if args.dry_run else 'РЕМОНТ'}")
    for d in rep.dangling:
        print(f"  {d.table}.{d.column}: {d.missing} → {d.target}")
    if rep.before:
        print(f"  {'таблиця':<14}{'рядків до':>11}{'рядків після':>14}  вміст")
        for t, (n, h) in rep.before.items():
            n2, h2 = rep.after.get(t, (None, None))
            print(f"  {t:<14}{n:>11}{str(n2):>14}  {'збігається' if h == h2 else 'РІЗНИЙ'}")
    print(f"  integrity_check: {rep.integrity or '—'}")
    print(f"  foreign_key_check: {len(rep.fk_violations)} порушень")
    for p_ in rep.problems:
        print(f"  ! {p_}")
    print("ЗАФІКСОВАНО" if rep.applied else ("НЕ ЗАФІКСОВАНО" if rep.dangling else "ремонт не потрібен"))
    return 0 if rep.ok else 1


def cmd_identity(args: argparse.Namespace) -> int:
    """Нічний дозбір сильних ознак квартири й будинку (не паралельно зі збором)."""
    from realty import identity_backfill

    sources = [x.strip() for x in args.sources.split(",")]
    rep = identity_backfill.run(sources, budget_s=args.budget_min * 60)
    print(f"дозбір identity: {rep['status']}")
    for src, n in rep["done"].items():
        print(f"  {src}: оброблено {n}; лишилось без ознак {rep['left'].get(src, '—')}")
    if rep["errors"]:
        print(f"  карток, що не відкрились: {rep['errors']}")
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    import json

    from realty.db import init_db
    from realty.quality import audit, diagnose, housekeeping
    from realty.quality.rules import compute_thresholds, load_thresholds, save_thresholds

    init_db()
    action = args.action

    if action == "reclassify":
        st = housekeeping.reclassify()
        print(f"\nПЕРЕКЛАСИФІКАЦІЯ: перевірено {st['checked']}")
        print(f"  змінено стан:  {st['changed']['condition']}")
        print(f"  змінено ринок: {st['changed']['market']}")
        for field in ("condition", "market"):
            print(f"\n  {field}:")
            keys = sorted(set(st["before"][field]) | set(st["after"][field]))
            for k in keys:
                b, a = st["before"][field].get(k, 0), st["after"][field].get(k, 0)
                mark = "" if a == b else f"  ({a - b:+d})"
                print(f"    {k:<14} {b:>6} → {a:>6}{mark}")
        return 0

    if action == "diagnose":
        d = diagnose.run()
        print(json.dumps(d, ensure_ascii=False, indent=1, default=str))
        return 0

    if action == "thresholds":
        from realty.db import SessionLocal

        if args.recompute:
            with SessionLocal() as s:
                t = compute_thresholds(s)
            save_thresholds(t)
        else:
            t = load_thresholds()
        print(f"\nПОРОГИ (вибірка {t.sample_size}, {t.computed_at[:16]})")
        print("=" * 66)
        for name, label in (("price_usd", "ціна, $"), ("price_per_sqm", "$/м²"),
                            ("area_total", "площа, м²")):
            b = getattr(t, name)
            print(f"  {label:<12} перегляд {b.review_low:>9,.0f}..{b.review_high:>9,.0f}"
                  f"   відхилення {b.reject_low:>8,.0f}..{b.reject_high:>10,.0f}")
        return 0

    if action == "revalidate":
        r = housekeeping.revalidate(limit=args.limit)
        print(f"\nРЕВАЛІДАЦІЯ: перевірено {r['checked']}")
        print(f"  було:  {r['before']}")
        print(f"  стало: {r['after']}")
        return 0

    if action == "audit":
        a = audit.run(limit=args.limit or 500)
        print("\nАУДИТ ДЕДУПЛІКАЦІЇ")
        print("=" * 52)
        print(f"  оголошень:                    {a['listings']}")
        print(f"  пропущених пар (майже дублі): {a['missed_pairs']}")
        print(f"    з них нижче порога:         {a['missed_below_threshold']}")
        print(f"  злиття з низькою впевненістю: {a['low_confidence_merges']}")
        print(f"  суперечливих об'єктів:        {a['wrong_merges']}")
        for w in a["wrong_examples"][:5]:
            print(f"    об'єкт {w['property_id']} ({w['members']}): {w['problems']}")
        return 0

    routine = housekeeping.ROUTINES.get(action)
    if routine is None:
        print(f"невідома дія: {action}")
        return 1
    result = routine()
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str)[:3000])
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from realty.db import init_db
    from realty.verify import verify_batch

    init_db()
    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    st = verify_batch(limit=args.limit, sources=names)
    print("\n" + "=" * 64)
    print("ПЕРЕВІРКА АКТУАЛЬНОСТІ")
    print("=" * 64)
    print(f"  запитів:           {st['requests']}")
    print(f"  перевірено:        {st['checked']}")
    print(f"  живі:              {st['alive']}")
    print(f"  знято з продажу:   {st['delisted']}")
    print(f"  повернулись:       {st['restored']}")
    print(f"  без висновку:      {st['unknown']}")
    if st["by_host"]:
        print(f"\n  {'сайт':<16}{'запитів':>9}{'відмов':>9}{'':>4}")
        for host, h in sorted(st["by_host"].items()):
            note = "  чергу зупинено" if h["stopped_early"] else ""
            share = 100 * h["blocked"] / h["requests"] if h["requests"] else 0
            print(f"  {host:<16}{h['requests']:>9}{h['blocked']:>9}"
                  f" ({share:.1f}%){note}")
    if st["by_source"]:
        print(f"\n  {'джерело':<10}{'перевірено':>11}{'живі':>7}{'знято':>7}{'без висн.':>11}")
        for name, b in sorted(st["by_source"].items()):
            print(f"  {name:<10}{b['checked']:>11}{b['alive']:>7}{b['delisted']:>7}"
                  f"{b['unknown']:>11}")
    print("=" * 64)
    print("  (blago не перевіряється: сайт не відрізняє видалене планування)")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from realty.db import init_db
    from realty.snapshot import run

    init_db()
    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    rep = run(names, confirm=not args.no_confirm, force=args.force)
    print("\n" + "=" * 74)
    print("РІЗНИЦЯ СПИСКІВ")
    print("=" * 74)
    head = f"  {'джерело':<9}{'було':>7}{'стало':>7}{'запитів':>9}{'кандидатів':>12}"
    print(head + f"{'знято':>8}{'живі':>7}{'?':>4}")
    for name, e in rep["sources"].items():
        if "error" in e:
            print(f"  {name:<9} помилка: {e['error'][:52]}")
            continue
        print(f"  {name:<9}{e['previous']:>7}{e['current']:>7}{e['requests']:>9}"
              f"{e['candidates']:>12}{e['confirmed']:>8}{e['still_alive']:>7}"
              f"{e['unclear']:>4}")
        if not e["used"]:
            print(f"             ↳ порівняння пропущено: {e['reason']}")
    for name, reason in rep.get("skipped", {}).items():
        print(f"  {name:<9} перелік не застосовний: {reason}")
    print("-" * 74)
    print(f"  запитів на перелік:     {rep['requests_enumerate']}")
    print(f"  запитів на підтвердження: {rep['requests_confirm']}")
    print(f"  підтверджено знятих:    {rep['delisted']}")
    print("=" * 74)
    print("  Випадіння зі списку — лише кандидат. Статус «знято» ставиться")
    print("  тільки за явним 404/410 від поодинокого запиту.")
    return 0


def cmd_dedup(args: argparse.Namespace) -> int:
    from realty import dedup
    from realty.db import init_db, session_scope
    from realty.dedup import rebuild

    init_db()
    if args.action == "sample":
        from realty import dedup_sample
        res = dedup_sample.run()
        print(dedup_sample.render(res))
        try:
            dedup_sample.notify_owner(res)
        except Exception as e:                  # звіт уже записаний на /status
            print(f"  Telegram: не надіслано ({type(e).__name__}: {e})")
        return 0
    rules = None if args.rules is None else (
        dedup.RULES if args.rules == "all" else [r for r in args.rules.split(",") if r])
    with session_scope() as s:
        st = rebuild(s, dry_run=args.dry_run, rules=rules)
    print("\n" + "=" * 58)
    print("МІЖПЛАТФОРМНА ДЕДУПЛІКАЦІЯ" + ("  (пробний прогін)" if args.dry_run else ""))
    print("=" * 58)
    print(f"  правила D41:          {', '.join(st['rules']) or 'вимкнені (як до D41)'}")
    print(f"  оголошень:            {st['listings']}")
    print(f"  унікальних об'єктів:  {st['properties']}")
    print(f"  об'єднано груп:       {st['merged_groups']} "
          f"({st['merged_listings']} оголошень)")
    print(f"  з них між джерелами:  {st['cross_source']}")
    if st.get("ambiguous"):
        print(f"  лишено окремо як неоднозначні: {st['ambiguous']}")
    print("=" * 58)
    if args.dry_run:
        return 0
    # Самоперевірка після кожного зведення: протиріччя всередині квартир і
    # пропущені дублі — у чергу на перегляд і на /status.
    from realty import dedup_audit
    try:
        with session_scope() as s:
            res = dedup_audit.audit(s)
        dedup_audit.record(res, st["rules"])
    except Exception as e:                      # зведення вже записане — лише сигналимо
        print(f"  самоперевірка не вдалась: {type(e).__name__}: {e}")
        return 1
    print(f"  самоперевірка: підозрілих квартир {res['suspicious']} з {res['properties']}, "
          f"пропущених дублів {res['missed']}")
    for kind, n in sorted(res["by_kind"].items(), key=lambda kv: -kv[1]):
        print(f"    {dedup_audit.KINDS[kind]}: {n}")
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


def cmd_analytics(args: argparse.Namespace) -> int:
    import json

    from realty.analytics import inventory, report
    from realty.db import init_db

    init_db()
    if args.action == "inventory":
        data = inventory.run()
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str)
              if args.json else report.render(data))
        return 0

    if args.action == "forecast":
        from realty.analytics import forecast
        from realty.db import SessionLocal

        with SessionLocal() as s:
            state = forecast.state(s)
        print(state["message"])
        print("\nКоли який горизонт стане доступним:")
        for row in state["schedule"]:
            mark = "✓" if row["reached"] else " "
            print(f"  {mark} {row['date']:%d.%m.%Y}  {row['history_months']:>2} міс. "
                  f"історії → горизонт {row['horizon_months']} міс. ({row['note']})")
        return 0

    if args.action == "segments":
        from realty.analytics import cache
        from realty.db import SessionLocal

        with SessionLocal() as s:
            snapshot = cache.get(s, force=True)
        print(f"{'сегмент':<44}{'n':>6}{'медіана':>10}{'IQR':>16}")
        for r in snapshot.segments:
            name = f"{r['rooms_label']}, {r['condition_label']}, {r['market_label']}"
            print(f"  {name:<42}{r['n']:>6}{r['median_ppsqm']:>10}"
                  f"{f'{r["q1"]}–{r["q3"]}':>16}")
        print(f"\nПоза статистикою: {snapshot.below['segments']} сегментів "
              f"на {snapshot.below['objects']} об'єктів "
              f"(поріг {snapshot.below['threshold']}).")
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

    cy = sub.add_parser("cycle", help="регулярний цикл: збір і обслуговування з лімітами часу")
    cy.add_argument("--sources", help="через кому; типово всі ввімкнені")
    cy.add_argument("--trigger", default="schedule", choices=("cli", "manual", "schedule"))
    cy.add_argument("--run-timeout", type=float, help="стеля циклу в хвилинах")
    cy.add_argument("--only-sources", action="store_true",
                    help="лише збір, без переліку, перевірки, дублів і бекапу (для проб)")
    cy.set_defaults(func=cmd_cycle)

    bk = sub.add_parser("backup", help="бекап бази: копія, перевірка, відновлення, вивантаження")
    bk.add_argument("action", nargs="?", default="run",
                    choices=("run", "verify", "restore", "status"))
    bk.add_argument("file", nargs="?", help="архів для verify/restore")
    bk.add_argument("target", nargs="?", help="куди розгорнути (restore; файл не має існувати)")
    bk.add_argument("--if-due", action="store_true",
                    help="лише якщо останній успішний бекап старший за BACKUP_EVERY_HOURS")
    bk.add_argument("--no-upload", action="store_true", help="не вивантажувати поза машину")
    bk.set_defaults(func=cmd_backup)

    wd = sub.add_parser("watchdog", help="сигнал тиші й інші тривоги в Telegram")
    wd.add_argument("--test", action="store_true",
                    help="надіслати тестове повідомлення тим самим шляхом, що й тривогу")
    wd.set_defaults(func=cmd_watchdog)

    tn = sub.add_parser("tunnel", help="доступ ззовні через Cloudflare Tunnel")
    tn.add_argument("--plan", action="store_true", help="лише показати, що буде запущено")
    tn.set_defaults(func=cmd_tunnel)

    sm = sub.add_parser("schema", help="перевірка й ремонт зовнішніх ключів бази")
    sm.add_argument("action", choices=("check", "repair"))
    sm.add_argument("--dry-run", action="store_true", help="усе порахувати й відкотити")
    sm.add_argument("--db", help="шлях до файлу бази (типово — робоча)")
    sm.set_defaults(func=cmd_schema)

    idn = sub.add_parser("identity", help="нічний дозбір сильних ознак квартири й будинку")
    idn.add_argument("action", choices=("backfill",))
    idn.add_argument("--sources", default="domria,lun,flombu")
    idn.add_argument("--budget-min", type=float, default=110,
                     help="стеля часу; має закінчитись до наступного циклу")
    idn.set_defaults(func=cmd_identity)

    bf = sub.add_parser("backfill", help="дозібрати наявні записи з прогалинами")
    bf.add_argument("--sources", help="через кому: domria,lun,olx,flombu,blago")
    bf.add_argument("--limit", type=int, default=300)
    bf.add_argument("--no-llm", action="store_true")
    bf.set_defaults(func=cmd_backfill)

    ql = sub.add_parser("quality", help="контроль якості даних")
    ql.add_argument("action", choices=("diagnose", "thresholds", "revalidate",
                                       "reclassify", "audit", "daily", "weekly",
                                       "monthly"))
    ql.add_argument("--limit", type=int, help="скільки записів обробити")
    ql.add_argument("--recompute", action="store_true", help="перерахувати пороги")
    ql.set_defaults(func=cmd_quality)

    vf = sub.add_parser("verify", help="перевірити, які оголошення ще живі")
    vf.add_argument("--limit", type=int, default=None,
                    help="скільки перевірити за раз НА КОЖЕН САЙТ; "
                         "без цього — власна порція кожного сайту")
    vf.add_argument("--sources", help="через кому; типово — усі, що вміємо перевіряти")
    vf.set_defaults(func=cmd_verify)

    sn = sub.add_parser("snapshot",
                        help="знайти зниклі оголошення різницею списків")
    sn.add_argument("--sources", help="через кому; типово всі")
    sn.add_argument("--no-confirm", action="store_true",
                    help="лише знайти кандидатів, без поодинокої перевірки")
    sn.add_argument("--force", action="store_true",
                    help="перелічити зараз, не чекаючи інтервалу")
    sn.set_defaults(func=cmd_snapshot)

    dd = sub.add_parser("dedup", help="звести однакові квартири в майстер-записи")
    dd.add_argument("action", nargs="?", default="rebuild", choices=("rebuild", "sample"),
                    help="sample — щотижнева перевірка 20 випадкових квартир")
    dd.add_argument("--dry-run", action="store_true", help="лише порахувати, без запису")
    dd.add_argument("--rules", help="правила D41 через кому або all; без — з DEDUP_RULES")
    dd.set_defaults(func=cmd_dedup)

    sh = sub.add_parser("schedule", help="фоновий розклад збору")
    sh.add_argument("action", nargs="?", default="status",
                    choices=("status", "install", "uninstall", "cron", "worker"))
    sh.add_argument("--hours", type=float, default=3, help="інтервал у годинах")
    sh.add_argument("--runs", type=int, help="обмежити кількість прогонів (для воркера)")
    sh.set_defaults(func=cmd_schedule)

    sv = sub.add_parser("serve", help="запустити веб-інтерфейс")
    sv.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    # Порт може задати середовище (PORT) — так його передає планувальник
    # прев'ю. Явний --port лишається пріоритетним.
    sv.add_argument("--port", type=int,
                    default=int(os.getenv("PORT", "8000")))
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    an = sub.add_parser("analytics", help="аналітика по зібраній базі")
    an.add_argument("action", choices=["inventory", "segments", "forecast"],
                    help="inventory — що взагалі можна побудувати чесно; "
                         "segments — медіани по сегментах; "
                         "forecast — межі прогнозу й коли вони зсунуться")
    an.add_argument("--json", action="store_true", help="сирі числа замість таблиці")
    an.set_defaults(func=cmd_analytics)

    st = sub.add_parser("stats", help="підсумки по базі")
    st.set_defaults(func=cmd_stats)

    args = p.parse_args()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
