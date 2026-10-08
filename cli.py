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
  python cli.py snapshot               # позначити зниклі з переліку (absent_since)
  python cli.py liveness plan          # яруси перевірки актуальності (без мережі)
  python cli.py liveness fuse status   # запобіжник; fuse clear --source domria — зняти
  python cli.py liveness report        # підсумки останньої ночі (по хостах, до/після)
  python cli.py liveness report --status --liquidity  # зведення /status і строк продажу
  python cli.py night --dry-run        # план ночі: ключі × темп = тривалість по хостах
  python cli.py night --budget-min 100 # нічний диригент (таймер realty-night, 01:10 і 04:10)
  python cli.py quality diagnose       # що не так із даними
  python cli.py quality audit          # аудит дедуплікації
  python cli.py serve --port 8000      # веб-інтерфейс
  python cli.py stats                  # що вже є в базі
  python cli.py config check           # перевірити конфіги в config/ (перед розгортанням)
  python cli.py speed probe            # коротко поміряти робочий сайт (лише 127.0.0.1)
  python cli.py speed report           # зведення «Швидкість» із журналу часу
  python cli.py db migrate --dry-run   # план змін схеми (індекси, колонки), нічого не пише
  python cli.py db migrate             # застосувати під замком циклу (між циклами!)
  python cli.py db plans               # чи не проходять запити сайту всю таблицю
  python cli.py links reindex --dry-run  # ключ «сайт:id» там, де NULL (план)
  python cli.py links reindex --fix-mismatched --dry-run  # після правки links.toml
  python cli.py links selftest         # кожна адреса знаходить свій рядок за ключем
  python cli.py privacy scan           # скільки телефонів в описах (лише читання)
  python cli.py privacy apply --yes    # разова заміна на «[телефон]» (після бекапу!)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Старт процесу: стеля часу кроку «перевірка актуальності» рахується від нього, а
# не від кінця плану черги (рецензія E8, D52).
_STARTED = time.monotonic()

# Заборона зовнішньої мережі для дочірніх процесів тестів (D45): conftest
# ставить REALTY_NETGUARD=1, і `cli.py`, запущений тестом окремим процесом,
# інакше був би неохоплений. Без цієї змінної — нічого не робить (у роботі її
# немає). Стоїть до імпорту realty.config, тож .env її ввімкнути не може.
from realty import netguard as _netguard

_netguard.install_from_env()

from realty.config import SOURCES, SourceConfig  # noqa: E402


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
    if args.min_delay:
        # Нічний дозбір identity у смузі хоста (E9, D53): стрічка джерела — не швидше
        # за нічний темп хоста (policy.pace night), і в повному, і у звичайному режимі.
        import dataclasses

        for name, cfg in SOURCES.items():
            SOURCES[name] = dataclasses.replace(
                cfg, delay=max(cfg.delay, args.min_delay),
                full_delay=max(cfg.full_delay or cfg.delay, args.min_delay))
    from realty.pipeline import Pipeline

    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    report = Pipeline(sources=names, use_llm=not args.no_llm, mode=args.mode,
                      trigger=args.trigger, fetch_details=not args.no_detail).run()
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
    if args.if_due and backup.busy():
        # Інший бекап (напр., нічний на старті вікна 04:10) ще йде: другий архів поспіль
        # лише навантажив би диск (рецензія E9, D53).
        print("бекап не потрібен: інший бекап ще йде")
        return 0
    import signal

    # SIGTERM (стеля кроку циклу чи ночі, systemctl stop) — як SystemExit: запис
    # ops.backups закривається, тимчасова тека з копіями баз прибирається (рецензія E9).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    res = backup.run(upload=not args.no_upload, wait_s=(args.wait_minutes or 0) * 60)
    if res.status == "busy":
        print("\nБЕКАП: інший бекап ще йде — не дочекались")
        return 1
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


def _print_verify(st: dict) -> None:
    print("\n" + "=" * 64)
    print("ПЕРЕВІРКА АКТУАЛЬНОСТІ")
    print("=" * 64)
    print(f"  запитів:           {st['requests']}")
    print(f"  перевірено:        {st['checked']}")
    print(f"  живі:              {st['alive']}")
    print(f"  знято з продажу:   {st['delisted']}")
    print(f"  повернулись:       {st['restored']}")
    print(f"  без висновку:      {st['unknown']}"
          + (f" (з них один 404 — {st['not_found']})" if st.get("not_found") else ""))
    if st.get("repaired"):
        print(f"  полагоджено посилань: {st['repaired']}")
    if st.get("tiers"):
        print(f"\n  {'сайт':<20}" + "".join(f"{t:>11}" for t in (
            "canary", "random", "opened", "repeat404", "absent", "reseen", "held",
            "rm_sample", "sweep")))
        for host, tiers in sorted(st["tiers"].items()):
            print(f"  {host:<20}" + "".join(f"{tiers.get(t, 0):>11}" for t in (
                "canary", "random", "opened", "repeat404", "absent", "reseen", "held",
                "rm_sample", "sweep")))
    if st["by_host"]:
        print(f"\n  {'сайт':<16}{'запитів':>9}{'відмов':>9}{'':>4}")
        for host, h in sorted(st["by_host"].items()):
            note = "  чергу зупинено" if h["stopped_early"] else ""
            if h.get("skipped"):
                note += f"  не дійшла черга: {h['skipped']}"
            share = 100 * h["blocked"] / h["requests"] if h["requests"] else 0
            print(f"  {host:<16}{h['requests']:>9}{h['blocked']:>9}"
                  f" ({share:.1f}%){note}")
            if h.get("signatures"):
                print("                    " + ", ".join(
                    f"{k} {v}" for k, v in sorted(h["signatures"].items())))
    if st["by_source"]:
        print(f"\n  {'джерело':<10}{'перевірено':>11}{'живі':>7}{'знято':>7}{'поверн.':>9}"
              f"{'без висн.':>11}")
        for name, b in sorted(st["by_source"].items()):
            print(f"  {name:<10}{b['checked']:>11}{b['alive']:>7}{b['delisted']:>7}"
                  f"{b.get('restored', 0):>9}{b['unknown']:>11}")
    for t in st.get("trips") or []:
        print(f"\n  ЗАПОБІЖНИК: {t['source']} — «знято» {t['removed']} із {t['checked']} "
              f"({t['reason']}); нічого не знято й не повернуто, чекає рішення на /status")
    if st.get("held_sources"):
        print(f"  під запобіжником: {', '.join(st['held_sources'])}")
    for g in st.get("canary_genuine") or []:
        print(f"  контрольний {g['key']}: знято на джерелі {g['source_removed_at']}, після "
              f"появи в стрічці {g['seen']} — справжнє зняття, запобіжник не тримає")
    print("=" * 64)
    print("  (blago не перевіряється: сайт не відрізняє видалене планування)")


def _cycle_refusal() -> str | None:
    """Чому ручну перевірку зараз запускати не можна (None — можна)."""
    from realty import runner

    if runner.DISABLED_FLAG.exists():
        return ("збір на цій машині вимкнено (data/COLLECTOR_OFF) — перевірку не запускаю; "
                "--force — свідомо")
    holder = runner.lock_busy(runner.LOCK_PATH)
    if holder:
        return (f"іде цикл (PID {holder.get('pid') or holder.get('lock_pid') or '?'}): друга "
                f"перевірка подвоїла б темп запитів до кожного сайту (rieltor 3,0 с → ~1,5 с, "
                f"403 після п'яти запитів, Етап 0) — спробуйте після циклу; --force — свідомо")
    return None


def cmd_verify(args: argparse.Namespace) -> int:
    """Перевірка актуальності. Диригент циклу запускає її кроком (runner.STEP_ENV) —
    тоді це прогін циклу. Запущена вручну — «manual»: не під час циклу й не з
    вимкненим збором (інакше дві смуги до одного сайту; D50, рецензія E8, D52),
    не закриває відкладених завдань і не переписує зведення /status."""
    import signal

    from realty import runner
    from realty.db import init_db
    from realty.verify import verify_batch

    in_cycle = bool(os.environ.get(runner.STEP_ENV))
    if not in_cycle and not getattr(args, "force", False):
        why = _cycle_refusal()
        if why:
            print(f"ВІДМОВА: {why}")
            return 2
    # SIGTERM диригента (стеля кроку) — як SystemExit: прогін закривається «failed»
    # (service.run), а не лишається «running» назавжди (як у cmd_lookup).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    init_db()
    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    st = verify_batch(limit=args.limit, sources=names, kind=None if in_cycle else "manual",
                      started=_STARTED)
    _print_verify(st)
    return 0


def cmd_liveness(args: argparse.Namespace) -> int:
    """Блок 1 (E8, D52): прогін, запобіжник, зведення, план без мережі."""
    import json

    from realty.db import SessionLocal, init_db

    init_db()
    if args.action == "run":
        return cmd_verify(args)
    if args.action == "fuse":
        from realty.liveness import fuse

        if args.fuse_action == "clear":
            if not args.source:
                print("потрібно --source")
                return 2
            ok = fuse.clear(args.source, by="cli")
            print(f"{args.source}: {'запобіжник знято' if ok else 'запобіжник не тримався'}")
            return 0
        rows = fuse.state()
        if not rows:
            print("запобіжник не спрацьовував")
        for r in rows:
            print(f"  {r['source']:<10} {r['state']:<6} {r['reason'] or '':<13} "
                  f"«знято» {r['removed']} із {r['checked']}  з {r['tripped_at']}"
                  + (f"  знято {r['cleared_at']} ({r['cleared_by']})" if r["cleared_at"] else ""))
        log_rows = fuse.history(10)
        if log_rows:
            print("  історія (новіші першими):")
        for r in log_rows:
            what = (f"спрацював: «знято» {r['removed']} із {r['checked']} ({r['reason']})"
                    if r["action"] == "trip" else f"знято: {r['by']}")
            print(f"    {r['at']}  {r['source']:<10} {what}")
        return 0
    if args.action == "report" and not args.status:
        # Підсумки нічних вікон (E9, D53): по хостах, актуальні до/після, строк продажу.
        from realty.night import report as night_report

        rows = night_report.runs(limit=args.last, run_id=args.night)
        if not rows:
            print("нічних вікон ще не було (cli.py night); зведення /status — --status")
            return 0
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
            return 0
        for d in reversed(rows):
            print(night_report.render_run(d))
        return 0
    from realty.liveness import policy, queue, report

    cfg = policy.load()
    with SessionLocal() as s:
        if args.action == "plan":
            plan = queue.plan_run(s, cfg, held_sources=set())
            for host, tiers in sorted(plan.tiers.items()):
                print(f"  {host:<20} {sum(tiers.values()):>5}  " + ", ".join(
                    f"{k} {v}" for k, v in sorted(tiers.items())))
            print(f"  завдань при відкритті (відкладених): {len(plan.jobs)}")
            return 0
        rep = report.status_block(s, cfg, now=queue._now())
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
        if args.liquidity:
            print("\nСТРОК ПРОДАЖУ (Каплан—Меєр):")
            for name, row in report.liquidity(s).items():
                print(f"  {name:<8} медіана {row.get('median_days')} дн., подій "
                      f"{row.get('events')}, цензурованих {row.get('censored')}, "
                      f"S(30) {row.get('S30')}, S(90) {row.get('S90')}")
    return 0


def cmd_night(args: argparse.Namespace) -> int:
    """Нічний диригент (E9, D53): одне вікно ночі під замком циклу; смуги хостів —
    окремі процеси (`night lane`); `--dry-run` — план без мережі й запису."""
    import json
    from pathlib import Path

    if args.action == "lane":
        from realty.night import lane

        if not args.plan or not args.out:
            print("потрібно --plan і --out")
            return 2
        return lane.main(Path(args.plan), Path(args.out))
    from realty.night import conductor, report as night_report

    if args.action == "unhold":
        if not args.host:
            print("потрібно --host")
            return 2
        ok = conductor.unhold(args.host, by="cli")
        print(f"{args.host}: {'смугу знову дозволено' if ok else 'хост не чекав рішення'}")
        return 0
    import signal

    from realty.db import init_db

    if args.dry_run:
        init_db()
        d = conductor.dry_run()
        print(json.dumps(d, ensure_ascii=False, indent=1, default=str) if args.json
              else night_report.render_plan(d))
        return 0
    # SIGTERM (systemctl stop, TimeoutStartSec) — як SystemExit: запис ночі закривається
    # «failed», смуги зупиняються, замок звільняється (а не «running» назавжди).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        init_db()
        night = conductor.Conductor(budget_min=args.budget_min)
    except Exception as e:
        # Збій до власного запису диригента (конфіг, база): запис «failed» однаково —
        # інакше systemd «failed», а сторож мовчить (рецензія E9, D53).
        conductor.record_failure(f"диригент не стартував: {type(e).__name__}: {e}")
        raise
    res = night.run()
    rows = night_report.runs(limit=1, run_id=res.get("night_run_id"))
    if rows:
        print(night_report.render_run(rows[0]))
    else:
        print(f"ніч: {res['status']} — {res.get('message') or ''}")
    # Ненульовий код — лише там, де systemd має показати збій служби: бекап не вдався
    # (нічого не писали) і запуск поза вікном (не таймером). Пропущене через цикл
    # вікно — стан, а не збій (його видно в ops.night_runs і `liveness report`).
    return {"backup_failed": 1, "outside_window": 2}.get(res["status"], 0)


def cmd_snapshot(args: argparse.Namespace) -> int:
    from realty.db import init_db
    from realty.snapshot import run

    init_db()
    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    rep = run(names, force=args.force)
    print("\n" + "=" * 78)
    print("РІЗНИЦЯ СПИСКІВ")
    print("=" * 78)
    head = f"  {'джерело':<9}{'було':>7}{'стало':>7}{'запитів':>9}{'нових зниклих':>15}"
    print(head + f"{'повернулись':>13}{'зниклих актуальних':>20}")
    for name, e in rep["sources"].items():
        if "error" in e:
            print(f"  {name:<9} помилка: {e['error'][:52]}")
            continue
        absent = e["absent_active"] if e["absent_active"] is not None else "—"
        print(f"  {name:<9}{e['previous']:>7}{e['current']:>7}{e['requests']:>9}"
              f"{e['newly_absent']:>15}{e['back_in_list']:>13}{absent:>20}")
        if not e["used"] or not e["complete"]:
            print(f"             ↳ позначки не змінено: {e['reason']}")
    for name, reason in rep.get("skipped", {}).items():
        print(f"  {name:<9} перелік не застосовний: {reason}")
    print("-" * 78)
    print(f"  запитів на перелік:     {rep['requests_enumerate']}")
    print("=" * 78)
    print("  Зникнення з переліку нікого не знімає: воно лише ставить оголошення в")
    print("  чергу перевірки (ярус absent). «Знято» — тільки за явним сигналом.")
    # Неповний чи невдалий перелік — крок «різниця списків» не «ok» (видно на /status
    # у кроках циклу; рецензія E8, D52). Позначки й так не змінено.
    bad = sorted(n for n, e in rep["sources"].items() if "error" in e or not e.get("complete"))
    if bad:
        print(f"  НЕПОВНИЙ ПЕРЕЛІК: {', '.join(bad)} — снапшот не збережено")
        return 1
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
    if not args.dry_run:
        # Квартири перебудовано — знімок «Аналітики» сайту застарів (Блок 2, E5).
        from realty import webcache
        webcache.bump("analytics", "cli dedup")
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


def cmd_config(args: argparse.Namespace) -> int:
    """Перевірка конфігів: кожен файл проходить свою схему, для кожної схеми є файл.

    Запускається в процедурі розгортання ДО перезапуску служб: ненульовий код
    скасовує розгортання, і сайт не стартує з конфігом, якого не можна прочитати.
    """
    from realty import configfiles

    results = configfiles.check_all()
    print(f"тека конфігів: {configfiles.config_dir()}")
    for r in results:
        if r.ok:
            print(f"  ok      {r.name:<24} версія {r.digest[:12]}")
        else:
            print(f"  ПОМИЛКА {r.name:<24} {r.message}")
    bad = sum(not r.ok for r in results)
    print("усі конфіги чинні" if not bad else f"помилок: {bad}")
    # Перекриття читається й з .env: рядок REALTY_CONFIG_DIR там тихо підмінив
    # би конфіг із git для всіх служб. Розгортання з ним не проходить; свідомо
    # (тести, експерименти) — лише з --allow-override.
    override = configfiles.override_dir()
    if override:
        print(f"УВАГА: перекриття {configfiles.ENV_DIR}={override} — це не config/ із git.")
        if not args.allow_override:
            print("  Перевірку не зараховано: приберіть змінну (і з .env) або, якщо "
                  "це свідомо, додайте --allow-override.")
            return 1
    return 1 if bad else 0


def cmd_speed(args: argparse.Namespace) -> int:
    """Вимірювання швидкості сайту (Блок 2, D49) — без зайвих процесів сайту."""
    import json

    from realty import speedprobe

    if args.action == "report":
        from realty import ops
        from realty.web.perf import summary
        ops.init_ops()                      # таблиці журналу часу — якщо їх ще немає
        data = summary()
        print(json.dumps(data, ensure_ascii=False, indent=1) if args.json
              else speedprobe.render_summary(data))
        return 0
    if args.action == "priorities":
        data = speedprobe.priorities()
        print(json.dumps(data, ensure_ascii=False, indent=1) if args.json
              else speedprobe.render_priorities(data))
        return 0
    return speedprobe.main(phase=args.phase, base=args.base, repeats=args.repeats,
                           as_json=args.json)


def cmd_db(args: argparse.Namespace) -> int:
    """Схема бази: план і застосування міграції, плани запитів сайту (Блок 2, D50)."""
    from realty import dbmigrate

    if args.action == "plans":
        return dbmigrate.plans(preview=args.preview, verbose=args.show_all)
    return dbmigrate.migrate(dry_run=args.dry_run, wait_min=args.wait_min)


def cmd_lookup(args: argparse.Namespace) -> int:
    """Перевірки на вимогу з черги ops.lookup_checks (процес шаблону realty-lookup@).

    Ліміти — з config/speed.toml [open_check] (D47 п. 1: чисел у коді немає):
    `drain_budget_s` — після нього нових завдань не брати, `job_timeout_s` —
    ліміт процесу (TimeoutStartSec у realty-lookup@.service).
    """
    import signal

    from realty import configfiles
    from realty.db import init_db
    from realty.lookup import opened

    # SIGTERM від systemd (TimeoutStartSec, зупинка служби) — як SystemExit:
    # поточне завдання закривається «failed», а не лишається «running», і
    # виконуються обробники atexit (txnwatch). SIGKILL і OOM цього не дадуть —
    # тому покоління «lists» процес збільшує одразу після завдання, а не при виході.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    cfg = configfiles.load("speed").open_check
    init_db()
    done = opened.run(args.job, drain=not args.no_drain, budget_s=cfg.drain_budget_s,
                      timeout_s=cfg.job_timeout_s)
    for job_id, state in done:
        print(f"завдання {job_id}: {state}")
    return 0


def cmd_links(args: argparse.Namespace) -> int:
    """Ключ «сайт:id» (listings.site_key, крок E6, D51): заповнення, звірка, розбір."""
    from realty import links_index

    if args.action == "reindex":
        return links_index.reindex(db=args.db, dry_run=args.dry_run, wait_min=args.wait_min,
                                   fix_mismatched=args.fix_mismatched)
    if args.action == "selftest":
        import json

        rep = links_index.selftest(db=args.db)
        print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json
              else links_index.render_selftest(rep))
        return 0 if rep["unparsed"] == 0 and rep["stored_mismatch"] == 0 else 1
    if not args.text:
        print("потрібен текст: cli.py links parse 'посилання'")
        return 2
    print(links_index.describe(" ".join(args.text)))
    return 0


def cmd_privacy(args: argparse.Namespace) -> int:
    """Телефони в наявних описах і назвах (рішення власника 5, D46; E6–E7, D51)."""
    import json

    from realty import privacy_pass

    if args.action == "scan":
        if args.since:
            try:
                args.since = privacy_pass.parse_since(args.since)
            except ValueError as e:
                print(e)
                return 2
        rep = privacy_pass.scan(db=args.db, limit=args.limit, since=args.since)
        print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json
              else privacy_pass.render_scan(rep))
        return 0
    if args.action == "impact":
        rep = privacy_pass.impact(db=args.db, dedup=not args.no_dedup,
                                  force_dedup=args.force_dedup)
        print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json
              else privacy_pass.render_impact(rep))
        total = sum(rep["differences"].values()) + sum(
            rep["dedup"][x]["listings_in_changed_groups"] for x in ("active", "all")
        ) if "dedup" in rep else sum(rep["differences"].values())
        return 0 if total == 0 else 1
    return privacy_pass.apply(db=args.db, yes=args.yes, wait_min=args.wait_min,
                              no_backup_check=args.no_backup_check, ids_out=args.ids_out)


def cmd_webcache(args: argparse.Namespace) -> int:
    """Покоління кешу сайту: показати або збільшити вручну."""
    from realty import webcache

    if args.action == "bump":
        ok = webcache.bump(args.scope, args.reason or "вручну")
        print("збільшено" if ok else "не вдалося (див. журнал)")
        return 0 if ok else 1
    for row in webcache.describe() or [{"name": "—", "gen": 0, "changed_at": None,
                                        "reason": "ще ніхто не збільшував"}]:
        print(f"  {row['name']:<10} {row['gen']:>6}  {row['changed_at'] or '—'}  {row['reason'] or ''}")
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
    sc.add_argument("--no-detail", action="store_true",
                    help="лише стрічка: без сторінок деталей (нічний дозбір identity)")
    sc.add_argument("--min-delay", type=float,
                    help="пауза між запитами до сайту джерела — не менша за цю, с")
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
    bk.add_argument("--wait-minutes", type=float,
                    help="інший бекап ще йде — чекати стільки хвилин і віддати його результат")
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
    idn.add_argument("--budget-min", type=float, default=105,
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
    vf.add_argument("--force", action="store_true",
                    help="запустити вручну навіть під час циклу чи з COLLECTOR_OFF")
    vf.set_defaults(func=cmd_verify)

    lv = sub.add_parser("liveness", help="перевірка актуальності (Блок 1): прогін, "
                                         "запобіжник, зведення, план")
    lv.add_argument("action", choices=("run", "fuse", "report", "plan"),
                    help="run — як verify; fuse — стан чи зняття запобіжника; report — "
                         "підсумки ночі (--status — зведення /status); plan — яруси черги "
                         "без мережі")
    lv.add_argument("fuse_action", nargs="?", default="status", choices=("status", "clear"))
    lv.add_argument("--source", help="fuse clear: джерело (domria, olx, lun, …)")
    lv.add_argument("--limit", type=int, default=None, help="run: стеля на кожен сайт")
    lv.add_argument("--sources", help="run: джерела через кому")
    lv.add_argument("--force", action="store_true",
                    help="run: навіть під час циклу чи з COLLECTOR_OFF")
    lv.add_argument("--liquidity", action="store_true",
                    help="report --status: ще й строк продажу (медіана, події, цензуровані)")
    lv.add_argument("--status", action="store_true",
                    help="report: зведення /status (JSON) замість підсумків ночі")
    lv.add_argument("--night", type=int, help="report: номер нічного вікна (ops.night_runs)")
    lv.add_argument("--last", type=int, default=1, help="report: скільки останніх вікон")
    lv.add_argument("--json", action="store_true", help="report: сирі числа")
    lv.set_defaults(func=cmd_liveness)

    nt = sub.add_parser("night", help="нічний диригент: перевірка актуальності, M2/M3, дозбір "
                                      "(таймер realty-night, 01:10 і 04:10)")
    nt.add_argument("action", nargs="?", default="run", choices=("run", "lane", "unhold"),
                    help="run — одне вікно ночі; lane — смуга хоста (запускає сам диригент); "
                         "unhold — знову дозволити смугу хоста після блокувань")
    nt.add_argument("--budget-min", type=float, default=None,
                    help="стеля видачі запитів від старту, хв (і так не пізніше stop_requests)")
    nt.add_argument("--dry-run", action="store_true",
                    help="лише план: ключі × темп = тривалість по хостах, без мережі й запису")
    nt.add_argument("--json", action="store_true", help="--dry-run: сирі числа")
    nt.add_argument("--plan", help="lane: файл плану смуги")
    nt.add_argument("--out", help="lane: файл результатів смуги")
    nt.add_argument("--host", help="unhold: хост (rieltor.ua, olx.ua, …)")
    nt.set_defaults(func=cmd_night)

    sn = sub.add_parser("snapshot",
                        help="знайти зниклі оголошення різницею списків")
    sn.add_argument("--sources", help="через кому; типово всі")
    sn.add_argument("--no-confirm", action="store_true",
                    help="(застаріле, нічого не змінює: зниклих перевіряє крок verify)")
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

    cf = sub.add_parser("config", help="конфіги в config/: перевірка перед розгортанням")
    cf.add_argument("action", choices=("check",))
    cf.add_argument("--allow-override", action="store_true",
                    help="свідомо перевірити теку з REALTY_CONFIG_DIR (тести, експерименти)")
    cf.set_defaults(func=cmd_config)

    sp = sub.add_parser("speed", help="швидкість сайту: зонд, зведення, пріоритети служб")
    sp.add_argument("action", choices=("probe", "report", "priorities"),
                    help="probe — коротко поміряти робочий сайт на 127.0.0.1; "
                         "report — зведення з журналу часу (ops.db); "
                         "priorities — чинні пріоритети служб (cgroup, nice, ionice)")
    sp.add_argument("--phase", choices=("idle", "cycle", "dedup"), default="idle",
                    help="idle — одразу; cycle — дочекатися циклу; dedup — кроку «дублі»")
    sp.add_argument("--base", help="адреса сайту (лише 127.0.0.1/localhost); типово — з config/speed.toml")
    sp.add_argument("--repeats", type=int, help="повторів кожної адреси (типово — з конфігу)")
    sp.add_argument("--json", action="store_true", help="сирі числа замість таблиці")
    sp.set_defaults(func=cmd_speed)

    dbp = sub.add_parser("db", help="схема бази: міграція (індекси, колонки) і плани запитів")
    dbp.add_argument("action", choices=("migrate", "plans"),
                     help="migrate — привести схему до моделі під замком циклу; "
                          "plans — EXPLAIN запитів сайту (лише читання)")
    dbp.add_argument("--dry-run", action="store_true", help="migrate: лише план DDL")
    dbp.add_argument("--wait-min", type=float, default=10.0,
                     help="migrate: скільки чекати, поки цикл звільнить замок (типово 10)")
    dbp.add_argument("--preview", action="store_true",
                     help="plans: на тимчасовій копії бази з індексами міграції")
    dbp.add_argument("--all", dest="show_all", action="store_true",
                     help="plans: показати плани всіх запитів, а не лише поганих")
    dbp.set_defaults(func=cmd_db)

    lk = sub.add_parser("lookup", help="перевірки на вимогу з черги (процес realty-lookup@)")
    lk.add_argument("action", choices=("check",))
    lk.add_argument("--job", type=int, required=True, help="номер завдання ops.lookup_checks")
    lk.add_argument("--no-drain", action="store_true",
                    help="лише це завдання, без інших, що чекають у черзі")
    lk.set_defaults(func=cmd_lookup)

    lnk = sub.add_parser("links", help="ключ «сайт:id» оголошень (listings.site_key)")
    lnk.add_argument("action", choices=("reindex", "selftest", "parse"),
                     help="reindex — заповнити ключ там, де NULL (під замком циклу); "
                          "selftest — звірка ключів (лише читання); parse — розбір посилання")
    lnk.add_argument("text", nargs="*", help="parse: посилання чи id")
    lnk.add_argument("--db", help="файл бази SQLite (типово — з DB_URL)")
    lnk.add_argument("--dry-run", action="store_true", help="reindex: лише кількості")
    lnk.add_argument("--fix-mismatched", action="store_true",
                     help="reindex: ще й переписати ключі, що не збігаються з адресою "
                          "(після правки config/links.toml)")
    lnk.add_argument("--wait-min", type=float, default=10.0,
                     help="reindex: скільки чекати, поки цикл звільнить замок (типово 10)")
    lnk.add_argument("--json", action="store_true", help="selftest: сирі числа")
    lnk.set_defaults(func=cmd_links)

    pv = sub.add_parser("privacy", help="телефони в описах і назвах → «[телефон]»")
    pv.add_argument("action", choices=("scan", "impact", "apply"),
                    help="scan — що було б замінено (лише читання, без цифр); "
                         "impact — чи не зміниться класифікація й зведення (лише читання); "
                         "apply — разова заміна в наявних рядках (потрібен --yes)")
    pv.add_argument("--db", help="файл бази SQLite (типово — з DB_URL)")
    pv.add_argument("--limit", type=int, help="scan: переглянути лише перші N рядків")
    pv.add_argument("--since", help="scan: лише нові оголошення (first_seen ≥ «2026-10-09», "
                                    "«2026-10-09 01:00» чи «2026-10-09T01:00Z»; час — UTC, "
                                    "як у базі)")
    pv.add_argument("--json", action="store_true", help="scan/impact: сирі числа")
    pv.add_argument("--no-dedup", action="store_true", help="impact: без пробного зведення")
    pv.add_argument("--force-dedup", action="store_true",
                    help="impact: перерахувати зведення, навіть якщо форми оголошень однакові")
    pv.add_argument("--yes", action="store_true", help="apply: так, змінити дані")
    pv.add_argument("--no-backup-check", action="store_true",
                    help="apply: не вимагати свіжого бекапу (лише для копій бази)")
    pv.add_argument("--wait-min", type=float, default=10.0,
                    help="apply: скільки чекати, поки цикл звільнить замок (типово 10)")
    pv.add_argument("--ids-out", help="apply: файл для id змінених рядків")
    pv.set_defaults(func=cmd_privacy)

    wc = sub.add_parser("webcache", help="покоління кешу сайту (lists, analytics)")
    wc.add_argument("action", choices=("show", "bump"))
    wc.add_argument("scope", nargs="?", default="lists", choices=("lists", "analytics"))
    wc.add_argument("--reason")
    wc.set_defaults(func=cmd_webcache)

    args = p.parse_args()
    _setup_logging(args.verbose)
    # Вікна транзакцій запису кроку циклу (Блок 2, D49): лише якщо диригент
    # поставив TXN_WATCH=1. Вмикається до першого звернення до бази.
    from realty import txnwatch
    txnwatch.install_from_env(sys.argv)
    # Покоління кешу сайту (Блок 2, крок E5, D50): будь-яка команда, що
    # зафіксувала запис у realty.db, при виході збільшує «lists» — сайт
    # побачить зміни, навіть якщо команда не з переліку. Сайт (serve) свої
    # записи бачить у власному процесі.
    if args.cmd != "serve":
        txnwatch.install_autobump(sys.argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
