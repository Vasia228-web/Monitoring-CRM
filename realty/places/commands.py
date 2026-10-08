"""`cli.py places check|assign|report|sample` (Блок 4, крок E10, D57).

  * `check`  — довідник цілком (колізії псевдонімів, райони ЖК, id): код ≠ 0 при помилці;
  * `assign [--dry-run] [--measure-weak]` — крок циклу «райони й ЖК». Диригент циклу
    запускає його кроком (runner.STEP_ENV) під своїм замком; вручну — під замком циклу з
    очікуванням ≤ --wait-min (як `links reindex`), інакше відмова (код 2). `--dry-run` —
    нічого не пише в realty.db (рядок у ops.places_runs зі статусом dry_run пишеться);
    `--measure-weak` — заміряти точність вимкнених ступенів (координати, заголовок) без
    застосування;
    У циклі крок діє лише з `enabled = true` у config/cycle.toml ([places]): до перегляду
    вибірки власником диригент його не ставить, а запуск із позначкою кроку циклу —
    пропуск з кодом 0 (рецензія E10, D57). Без колонок S4 — відмова «спершу `cli.py db
    migrate`» (код 2): --dry-run нічого не додає в схему (init_db лише для запису);
  * `reassign [--apply [--include-lost]] [--ids-out ФАЙЛ]` — виправити ВЖЕ визначені
    ключі за нинішніми доказами (рішення власника; план Блоку 4). Типово — пробний
    прогін (що змінилось би, нічого не пише). `--apply` — лише з успішним бекапом,
    свіжішим за rules.reassign.backup_max_age_min, під замком циклу, пакетами
    BEGIN IMMEDIATE з тим самим відбитком інших колонок; id змінених рядків — у
    ops.places_runs (would_change_detail.changed_ids) і в --ids-out;
  * `report [--json]` — останній прогін (те саме, що /api/status/places);
  * `sample --out CSV [--per-source 50]` — розшарована вибірка для ручної перевірки
    (джерело × ступінь і шар «громада»; поля, які бачить людина, і докази будинку —
    що кажуть інші квартири за тією самою адресою; без телефонів).
"""
from __future__ import annotations

import csv
import json
import os
import random
import signal
import sys


def check(out=print) -> int:
    from .. import configfiles
    from .directory import DirectoryError, load

    try:
        d = load()
    except configfiles.ConfigError as e:
        out(f"ПОМИЛКА конфігу: {e}")
        return 1
    except DirectoryError as e:
        out("ПОМИЛКА довідника:\n  " + "\n  ".join(e.problems))
        return 1
    areas = {}
    for k in d.districts.values():
        areas[k.area] = areas.get(k.area, 0) + 1
    out(f"довідник {d.version}: районів {len(d.districts)} ({areas}), ігнорованих назв "
        f"{len(d.ignore)}, ЖК {len(d.complexes)} (з районом "
        f"{sum(1 for c in d.complexes.values() if c.district)}), id DOM.RIA за назвою: "
        f"{sorted(d.ria_by_name)}")
    out("довідник чинний")
    return 0


S4_COLUMNS = ("district_key", "complex_key", "row_district", "row_complex", "row_area",
              "place_sig")


def _schema_missing() -> list[str]:
    """Колонки S4, яких немає в listings (лише читання схеми)."""
    from sqlalchemy import inspect

    from .. import db as dbmod

    try:
        have = {c["name"] for c in inspect(dbmod.engine).get_columns("listings")}
    except Exception:                                   # noqa: BLE001 — таблиці немає
        return list(S4_COLUMNS)
    return [c for c in S4_COLUMNS if c not in have]


def cycle_enabled() -> bool:
    """config/cycle.toml [places] enabled — крок у циклі лише після перегляду вибірки."""
    from .. import configfiles

    try:
        return bool(configfiles.load("cycle").places.enabled)
    except configfiles.ConfigError:
        return False


def _load_inputs(out):
    from .. import configfiles
    from .directory import DirectoryError, load

    try:
        d = load()
        rules, rules_hash = configfiles.load_with_hash("places/rules")
    except (configfiles.ConfigError, DirectoryError) as e:
        out(f"ВІДМОВА: довідник районів і ЖК не завантажився — {e}")
        return None
    return d, rules, rules_hash


def assign(*, dry_run: bool, wait_min: float, measure_weak: bool = False, out=print) -> int:
    from .. import runner

    in_cycle = bool(os.environ.get(runner.STEP_ENV))
    if in_cycle and not cycle_enabled():
        # Диригент не ставить кроку з enabled = false; це — запуск із позначкою кроку в
        # обхід нього (старий диригент, ручний запуск із REALTY_CYCLE_STEP).
        out("крок «райони й ЖК» у циклі вимкнено до перегляду вибірки власником "
            "(config/cycle.toml [places] enabled = false) — пропущено")
        return 0
    return _run(mode="fill", dry_run=dry_run, wait_min=wait_min, measure_weak=measure_weak,
                out=out)


def reassign(*, apply: bool, include_lost: bool = False, wait_min: float = 10.0,
             ids_out: str | None = None, no_backup_check: bool = False, out=print) -> int:
    """Виправлення вже визначених ключів (рішення власника). Без --apply — пробний прогін."""
    if apply and not no_backup_check:
        inputs = _load_inputs(out)
        if inputs is None:
            return 1
        ok, why = _backup_ok(inputs[1].reassign.backup_max_age_min)
        if not ok:
            out(f"ВІДМОВА: {why}. Спершу `cli.py backup run` (зі status ok), потім одразу "
                f"`places reassign --apply`.")
            return 2
        out(why)
    elif apply:
        out("перевірку бекапу пропущено (--no-backup-check — лише для копій бази)")
    return _run(mode="reassign", dry_run=not apply, wait_min=wait_min, include_lost=include_lost,
                ids_out=ids_out, out=out)


def _backup_ok(max_age_min: float) -> tuple[bool, str]:
    from datetime import timedelta

    from .. import backup, ops

    try:
        last = backup.last_success_at()
    except Exception as e:                           # noqa: BLE001
        return False, f"не вдалося прочитати журнал бекапів: {e}"
    if last is None:
        return False, "успішного бекапу немає"
    if ops._now() - last > timedelta(minutes=max_age_min):
        return False, (f"останній успішний бекап {last:%d.%m %H:%M} UTC — старший за "
                       f"{max_age_min:g} хв (rules.reassign.backup_max_age_min)")
    return True, f"останній успішний бекап {last:%d.%m %H:%M} UTC"


def _run(*, mode: str, dry_run: bool, wait_min: float, measure_weak: bool = False,
         include_lost: bool = False, ids_out: str | None = None, out=print) -> int:
    from pathlib import Path

    from .. import runner, webcache
    from .. import db as dbmod
    from ..dbmigrate import locked_engine, wait_cycle_lock
    from . import assign as step

    in_cycle = bool(os.environ.get(runner.STEP_ENV))
    # SIGTERM диригента (стеля кроку) — як SystemExit: рядок прогону «failed», а пакети,
    # що вже записані, — цілі (кожен — своя транзакція).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    inputs = _load_inputs(out)
    if inputs is None:
        return 1
    d, rules, rules_hash = inputs
    missing = _schema_missing()
    if missing:
        # init_db() додав би колонки поза міграцією (без індексу й звірки даних) — ні.
        out(f"ВІДМОВА: у listings немає колонок S4 ({', '.join(missing)}) — спершу "
            f"`cli.py db migrate`.")
        return 2
    if not dry_run:
        dbmod.init_db()
    lock = None
    if not dry_run and not in_cycle:
        lock = wait_cycle_lock(wait_min)
        if lock is None:
            out(f"ВІДМОВА: цикл збору не звільнив замок за {wait_min:.0f} хв — запустіть між "
                f"циклами.")
            return 2
    weng = None if dry_run else locked_engine(dbmod.engine.url)
    kind = "reassign" if mode == "reassign" else ("cycle" if in_cycle else "manual")
    try:
        try:
            rep = step.run(dry_run=dry_run, d=d, rules=rules, rules_hash=rules_hash,
                           measure_weak=measure_weak, write_engine=weng, mode=mode,
                           include_lost=include_lost)
        except BaseException as e:
            rep = step.AssignReport(dry_run=dry_run, mode=mode)
            step.record(rep, d=d, rules_hash=rules_hash, kind=kind, status="failed",
                        message=f"{type(e).__name__}: {str(e)[:300]}")
            raise
        status = "dry_run" if dry_run else ("failed" if rep.stopped else "ok")
        run_id = step.record(rep, d=d, rules_hash=rules_hash, kind=kind, status=status,
                             message=rep.stopped)
    finally:
        if weng is not None:
            weng.dispose()
        if lock is not None:
            lock.release()
    out(step.render(rep, d))
    if mode == "reassign" and not dry_run:
        out(f"id змінених оголошень ({len(rep.changed_ids)}) — у ops.places_runs №{run_id} "
            f"(would_change_detail.changed_ids)")
        if ids_out:
            Path(ids_out).write_text("".join(f"{i}\n" for i in rep.changed_ids),
                                     encoding="utf-8")
            out(f"… і у файлі {ids_out}")
    if not dry_run and rep.wrote:
        # Квартири отримали нові район/ЖК — знімок «Аналітики» сайту застарів; список —
        # покоління «lists» (диригент і txnwatch при виході).
        webcache.bump("analytics", f"places {mode}")
    return 1 if rep.stopped else 0


def last_run() -> dict | None:
    """Останній прогін кроку (не dry_run, якщо є) — для /api/status/places і `report`."""
    from sqlalchemy import select

    from .. import ops

    ops.init_ops()
    with ops.ops_session() as s:
        row = s.scalars(select(ops.PlacesRun).where(ops.PlacesRun.status != "dry_run")
                        .order_by(ops.PlacesRun.id.desc()).limit(1)).first()
        if row is None:
            row = s.scalars(select(ops.PlacesRun).order_by(ops.PlacesRun.id.desc())
                            .limit(1)).first()
        if row is None:
            return None
        load = lambda v, empty: json.loads(v) if v else empty  # noqa: E731
        return {"id": row.id, "at": ops.as_utc_iso(row.at),
                "finished_at": ops.as_utc_iso(row.finished_at), "status": row.status,
                "kind": row.kind, "directory_ver": row.directory_ver,
                "rules_hash": row.rules_hash, "rows": row.rows,
                "filled": load(row.filled, {}), "would_change": row.would_change,
                "would_change_detail": load(row.would_change_detail, {}),
                "coverage": load(row.coverage, {}), "tiers": load(row.tiers, {}),
                "precision": load(row.precision, {}), "unknown": load(row.unknown, []),
                "unlinked": load(row.unlinked, []),
                "properties_changed": row.properties_changed,
                "rows_synced": row.rows_synced, "seconds": row.seconds,
                "message": row.message}


def report(*, as_json: bool, out=print) -> int:
    run = last_run()
    if run is None:
        out("прогонів кроку «райони й ЖК» ще не було")
        return 0
    if as_json:
        out(json.dumps(run, ensure_ascii=False, indent=1))
        return 0
    out(f"прогін №{run['id']} {run['at']} · {run['kind']} · {run['status']} · довідник "
        f"{run['directory_ver']} · {run['seconds']} с")
    for scope in ("before", "after"):
        cov = run["coverage"].get(scope, {}).get("_all", {})
        n = cov.get("active") or 0
        if n:
            out(f"  {('до' if scope == 'before' else 'після'):<6} актуальних {n}: район "
                f"{100 * cov.get('district', 0) / n:.1f}%, ЖК (з «не в ЖК») "
                f"{100 * cov.get('complex', 0) / n:.1f}%, «не в ЖК» {cov.get('none', 0)}")
    out(f"  заповнено {run['filled']}; would_change {run['would_change']}; нерозпізнаних "
        f"назв {len(run['unknown'])}; ЖК без району {len(run['unlinked'])}")
    return 0


def _top(c, n: int = 3) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n))


def sample(*, out_path: str, per_source: int, seed: int = 20261008, out=print) -> int:
    """Вибірка для ручної перевірки: до `per_source` актуальних оголошень на (джерело,
    ступінь району), (джерело, ступінь ЖК) і шар «громада» (місцевість села),
    детерміновано. Колонки — що визначено й з чого, і докази будинку: що кажуть ІНШІ
    квартири за тією самою вулицею й номером (поле району DOM.RIA, мітка LUN, ЖК DOM.RIA)
    — без них переглянути 500 рядків очима нереально (рецензія E10). Без описів і
    контактів."""
    from sqlalchemy import text

    from .. import db as dbmod
    from .. import links
    from . import address, extract
    from . import assign as step
    from .directory import load

    missing = _schema_missing()
    if missing:
        out(f"ВІДМОВА: у listings немає колонок S4 ({', '.join(missing)}) — спершу "
            f"`cli.py db migrate`.")
        return 2
    d = load()
    with dbmod.engine.connect() as conn:
        rows = step._load(conn)
        urls = dict(conn.execute(text("SELECT id, original_url FROM listings")).all())
    views = {r.id: extract.view(r) for r in rows}
    idx = address.Index()
    for r in rows:
        v = views[r.id]
        if v.addr is None:
            continue
        extra = {}
        if r.source == "domria":
            extra["R"] = (r.district or "").strip() or None
            extra["Z"] = next((name for _fld, name in v.complex_names), None)
        elif r.source == "lun":
            extra["L"] = extract.lun_label(r.location)
        idx.add(r.id, r.property_id if r.property_id is not None else ("l", r.id), v.addr,
                extra=extra)
    strata: dict = {}
    for r in rows:
        if not r.active:
            continue
        for what, how in (("district", r.district_how), ("complex", r.complex_how)):
            if how:
                strata.setdefault((r.source, what, how), []).append(r)
        if r.place_area == "hromada" or r.row_area == "hromada":
            strata.setdefault((r.source, "area", "hromada"), []).append(r)
    picked = []
    rnd = random.Random(seed)
    for key in sorted(strata):
        items = strata[key]
        picked += [(key, r) for r in (rnd.sample(items, per_source)
                                       if len(items) > per_source else items)]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["шар", "id", "джерело", "квартира", "посилання", "поле району",
                    "location", "ЖК у полі", "район", "як", "місцевість", "ЖК", "як",
                    "ria_district_same_addr", "lun_label_same_addr", "zhk_same_addr",
                    "перевірено: район ок?", "перевірено: ЖК ок?"])
        for (src, what, how), r in picked:
            # Канонічна адреса: без піддомену агенції rieltor (там буває номер телефону, D51).
            url = links.canonical_url(urls.get(r.id)) or ""
            w.writerow([f"{src}/{what}/{how}", r.id, r.source, r.property_id or "", url,
                        r.district, r.location, r.complex_name, d.label(r.district_key) or "",
                        r.district_how or "", r.place_area or "",
                        d.complex_label(r.complex_key) or (r.complex_key or ""),
                        r.complex_how or "", _top(idx.others(r.id, "R")),
                        _top(idx.others(r.id, "L")), _top(idx.others(r.id, "Z")), "", ""])
    out(f"вибірка: {len(picked)} рядків у {len(strata)} шарах → {out_path}")
    return 0
