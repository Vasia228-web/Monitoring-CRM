"""Рецензія кроку E10 (Блок 4, D57): докази будинку, «не в ЖК», вето мітки села, похідна
місцевість, `places reassign`, вибірка з доказами, вимикач кроку в циклі, сторож,
точність точок і населений пункт flombu, схема без S4.

Кожен тест описує поведінку, якої до виправлень не було (на коді до рецензії падає).
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import places_kit as kit  # noqa: E402
from realty import configfiles, ops  # noqa: E402
from realty.models import Base, MarketType, Property  # noqa: E402
from realty.places import directory  # noqa: E402
from realty.places.directory import NONE  # noqa: E402


def _db(tmp_path, rows, name="review.db"):
    """База з явних рядків: [(id, квартира, джерело, поля)]; усі чисті й актуальні."""
    engine = kit.make_engine(tmp_path, name)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        for pid in sorted({pid for _i, pid, _s, _kw in rows if pid}):
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=50.0,
                           first_seen=kit.NOW - timedelta(days=30), last_seen=kit.NOW))
        s.flush()
        for i, pid, src, kw in rows:
            base = dict(quality_status="ok", is_active=True, market_type=MarketType.PRIMARY)
            s.add(kit.listing(i, pid, src, **{**base, **kw}))
        s.commit()
    return engine


def _keys(engine) -> dict:
    with engine.connect() as conn:
        return {r[0]: tuple(r[1:]) for r in conn.execute(text(
            "SELECT id, district_key, district_how, complex_key, complex_how, place_area, "
            "row_district, row_complex, row_area FROM listings"))}


# --- Докази будинку -------------------------------------------------------------------------


def test_house_evidence_beats_agent_field_and_unlinked_complex(tmp_path):
    """Поле агента DOM.RIA («Каскад») проти п'яти інших квартир будинку («Софіївка») —
    район за будинком (рецензія E10: вул. Вовчинецька 223А). ЖК з довідника без району
    (U One) поля агента не бере: без доказів будинку — «не визначено»."""
    house = "вул. Вовчинецька, 223А"
    rows = [(i, 10 + i, "domria", dict(district="Софіївка", location=house))
            for i in range(1, 5)]
    rows.append((5, 15, "lun", dict(location="Вовчинецька вул., 223-А, Софіївка")))
    rows += [
        (20, 30, "domria", dict(district="Каскад", location=house)),
        (21, 31, "domria", dict(district="Каскад", identity={"complex": "ria:9316"},
                                location=house)),
        (22, 32, "domria", dict(district="Софіївка", identity={"complex": "ria:9316"},
                                location="вул. Інша, 7")),
    ]
    engine = _db(tmp_path, rows)
    rep = kit.assign(engine)
    keys = _keys(engine)
    assert keys[20][:2] == ("sofiivka", "addr")
    assert keys[21][:4] == ("sofiivka", "addr", "u-one", "src_id")
    assert keys[22][0] is None and keys[22][2] == "u-one"
    # Поле, підтверджене будинком, — «з джерела».
    assert keys[1][:2] == ("sofiivka", "src")
    assert rep.tiers.get("district:addr") == 2


def test_lun_village_label_vetoed_by_city_house(tmp_path):
    """Мітка села LUN («Крихівці») на будинку, який ≥3 оголошення DOM.RIA інших квартир
    кладуть у місто, відкидається: район — за будинком (≥5 голосів) або «не визначено»
    (місто); у звіті — area_conflicts (рецензія E10: Фізкультурна 27)."""
    rows = [(i, 100 + i, "domria", dict(district="Бам", location="вул. Фізкультурна, 27"))
            for i in range(1, 6)]
    rows.append((10, 200, "lun", dict(
        location="Фізкультурна вул., 27, Крихівці (Івано-Франківськ)")))
    rows += [(i, 100 + i, "domria", dict(district="Бам", location="вул. Приозерна, 35"))
             for i in range(11, 14)]
    rows.append((20, 300, "lun", dict(location="Приозерна вул., 35, Крихівці (Івано-Франківськ)")))
    # Без міських доказів будинку мітка села лишається.
    rows.append((30, 400, "lun", dict(location="Кераміків вул., 26, Кладовище, Крихівці "
                                               "(Івано-Франківськ)")))
    # Будинок, де міток села LUN більшість (13 з 16), але DOM.RIA (3) — місто: мітки села
    # не голосують і за рядок DOM.RIA без поля району.
    rows += [(40 + i, 500 + i, "domria", dict(district="Бам", location="вул. Мазепи, 9"))
             for i in range(3)]
    rows += [(50 + i, 600 + i, "lun", dict(location="Мазепи вул., 9, Крихівці"))
             for i in range(13)]
    rows.append((70, 700, "domria", dict(district="", location="вул. Мазепи, 9")))
    engine = _db(tmp_path, rows)
    rep = kit.assign(engine)
    keys = _keys(engine)
    assert keys[70][0] is None and keys[70][4] is None
    assert keys[10][0] == "bam" and keys[10][1] == "addr_area" and keys[10][4] == "city"
    assert keys[20][0] is None and keys[20][4] is None          # «не визначено» = місто
    assert keys[30][0] == "krykhivtsi" and keys[30][4] == "hromada"
    assert rep.area_conflicts["n"] == 2 + 13
    assert rep.area_conflicts["by_label"] == {"krykhivtsi": 2 + 13}


# --- «Не в ЖК» ----------------------------------------------------------------------------------


def test_not_in_complex_needs_observed_field_no_house_complex_and_no_declined_words(tmp_path):
    on = kit.rules_with_secondary()
    sec = dict(market_type=MarketType.SECONDARY)
    rows = [
        # Будинок, де інші квартири — у ЖК з поля DOM.RIA: «не в ЖК» заборонено.
        (1, 1, "domria", dict(identity={"complex": "ria:7017"}, location="вул. Височана, 18")),
        (2, 2, "domria", dict(identity={"complex": "ria:7017"}, location="вул. Височана, 18")),
        (3, 3, "domria", dict(location="вул. Височана, 18", **sec)),
        # Відмінки «житлового комплексу», «клубному будинку» в описі — ознака ЖК.
        (4, 4, "domria", dict(description="Квартира у житлового комплексу «Сонце»", **sec)),
        (5, 5, "domria", dict(description="Продаж у клубному будинку", **sec)),
        (6, 6, "domria", dict(description="Затишна квартира", **sec)),
        # LUN і OLX — лише якщо джерело справді показувало поле ЖК.
        (7, 7, "lun", dict(location="Хіміків вул., 3, Пасiчна", **sec)),
        (8, 8, "lun", dict(location="Хіміків вул., 4, Пасiчна",
                           place_raw={"lun_geo_checked_at": "2026-10-08"}, **sec)),
        (9, 9, "olx", dict(location="Івано-Франківськ", **sec)),
        (10, 10, "olx", dict(location="Івано-Франківськ",
                             place_raw={"olx_checked_at": "2026-10-08"}, **sec)),
    ]
    engine = _db(tmp_path, rows)
    kit.assign(engine, rules=on)
    keys = _keys(engine)
    assert keys[3][2] is None                       # не «не в ЖК»: будинок у ЖК
    assert keys[4][2] is None and keys[5][2] is None
    assert keys[6][2] == NONE
    assert keys[7][2] is None and keys[8][2] == NONE
    assert keys[9][2] is None and keys[10][2] == NONE


def test_not_in_complex_disabled_in_config_until_owner_sample():
    """Точність «не в ЖК» 59% (рецензія E10) — у config вимкнено до перевіреної вибірки."""
    assert configfiles.load("places/rules").secondary.markets == ()


# --- Похідна місцевість ------------------------------------------------------------------------


def test_area_is_derived_and_follows_directory_without_would_change(tmp_path):
    """Рішення власника про село (КАТОТТГ) — місцевість оголошення, квартири й рядка
    сайту йде за довідником; would_change лише для району/ЖК (рецензія E10)."""
    engine = kit.make_engine(tmp_path)
    kit.build(engine, n=160)
    kit.assign(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM listings WHERE district_key = "
                                 "'krykhivtsi' AND place_area = 'hromada'")).scalar() > 0
    districts = configfiles.load("places/districts")
    d2 = directory.Directory.build(
        replace(districts, district=tuple(replace(x, area="city") if x.key == "krykhivtsi"
                                          else x for x in districts.district)),
        configfiles.load("places/complexes"), configfiles.load("places/rules"))
    rep = kit.assign(engine, d=d2)
    assert rep.would_change == {} and "area" not in rep.would_change
    with engine.connect() as conn:
        bad = conn.execute(text(
            "SELECT count(*) FROM listings l LEFT JOIN properties p ON p.id = l.property_id "
            "WHERE l.district_key = 'krykhivtsi' AND (l.place_area IS NOT 'city' OR "
            "l.row_area IS NOT coalesce(p.place_area, l.place_area))")).scalar()
    assert bad == 0
    assert kit.assign(engine, d=d2).wrote is False


# --- Назви: сегмент location LUN, «ЖК …» у полі району, нормалізація, довідник ----------------------


def _resolve(src, **kw):
    from types import SimpleNamespace

    from realty.places import extract
    from realty.places.resolve import resolve

    base = dict(id=1, source=src, market_type="primary", property_id=None, district=None,
                complex_name=None, location=None, title=None, identity=None, place_raw=None)
    base.update(kw)
    return resolve(extract.view(SimpleNamespace(**base)), directory.load(),
                   configfiles.load("places/rules"))


def test_lun_location_middle_segment_and_complex_in_district_field():
    r = _resolve("lun", location="Слобідська вул., 46, Калинова Слобода, Крихівці")
    assert (r.complex_key, r.complex_how) == ("kalynova-sloboda", "src_name")
    r = _resolve("lun", location="Кераміків вул., 26, Кладовище, Крихівці")
    assert r.complex_key is None and r.unknown == []           # POI — не «нерозпізнана»
    r = _resolve("domria", district="ЖК Винагородний")
    assert r.complex_key == "vynahorodnyi" and r.district_key is None


def test_normalization_gaps_closed():
    d = directory.load()
    assert d.match_district("с. Крихівці") == ("district", "krykhivtsi")
    assert d.match_district("село Крихівці") == ("district", "krykhivtsi")
    assert d.match_complex("ЖК A5") == ("complex", "a5")              # латинська A
    assert d.match_complex("ЖК«Містечко Липки»") == ("complex", "mistechko-lypky")
    assert d.match_complex("Липки")[0] == "unknown"                   # Липки 2? PREMIUM?
    assert d.match_complex("Манхэттен") == ("complex", "manhattan")
    assert d.match_complex("Сентрал Парк") == ("complex", "central-park")
    assert d.match_district("Пасечная") == ("district", "pasichna")


def test_directory_parent_evidence_counts_only_directory_districts():
    """Патріот: Бам 7/8 = 88% (голос «ЖК Винагородний» у полі району не рахується) —
    дитина Бама; Козацький тоді — Бам 40/47 = 85% (рецензія E10)."""
    d = directory.load()
    assert d.districts["patriot"].parent == "bam" and d.label("patriot") == "Бам (Патріот)"
    assert d.complex_district("kozatskyi") == "bam"
    assert d.complex_display("kniahynyn") == "Житловий район Княгинин"     # парасолька
    assert d.complex_display("comfort-park") == "ЖК Comfort Park"


# --- reassign -------------------------------------------------------------------------------------


def _cli_env(tmp_path, db_name, **extra):
    env = {**os.environ, "DB_URL": f"sqlite:///{tmp_path / db_name}",
           "OPS_DB_URL": f"sqlite:///{tmp_path / 'ops_cli.db'}"}
    env.pop(configfiles.ENV_DIR, None)
    env.pop("REALTY_CYCLE_STEP", None)
    return {**env, **extra}


def _cli(env, *args):
    return subprocess.run([sys.executable, "cli.py", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


def test_reassign_fixes_determined_keys_only_on_apply_with_backup(tmp_path):
    engine = kit.make_engine(tmp_path, "ra.db")
    kit.build(engine, n=80)
    kit.assign(engine)
    with engine.begin() as conn:
        wrong = conn.execute(text("SELECT id FROM listings WHERE complex_how = 'src_id' "
                                  "ORDER BY id LIMIT 2")).scalars().all()
        conn.execute(text(f"UPDATE listings SET complex_key = 'skygarden' WHERE id IN "
                          f"({wrong[0]}, {wrong[1]})"))
    before = kit.raw_checksum(engine), _keys(engine)
    # Звичайний крок лише рахує.
    assert kit.assign(engine, dry_run=True).would_change["complex"] == 2
    env = _cli_env(tmp_path, "ra.db")
    r = _cli(env, "places", "reassign")
    assert r.returncode == 0 and "ВИПРАВЛЕНО б (reassign)" in r.stdout, r.stdout + r.stderr
    r = _cli(env, "places", "reassign", "--apply")
    assert r.returncode == 2 and "успішного бекапу немає" in r.stdout, r.stdout + r.stderr
    assert (kit.raw_checksum(engine), _keys(engine)) == before
    ids_file = tmp_path / "changed.txt"
    # Позначка кроку циклу — щоб процес не брав справжній data/cycle.lock репозиторію
    # (замок циклу тоді вважається вже взятим диригентом).
    r = _cli({**env, "REALTY_CYCLE_STEP": "тест"}, "places", "reassign", "--apply",
             "--no-backup-check", "--ids-out", str(ids_file))
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(int(x) for x in ids_file.read_text().split()) == sorted(wrong)
    after = _keys(engine)
    assert kit.raw_checksum(engine) == before[0]
    assert all(after[i][2] != "skygarden" for i in wrong)
    assert {i for i in after if after[i] != before[1][i]} >= set(wrong)
    ops_eng = create_engine(env["OPS_DB_URL"], future=True)
    with ops_eng.connect() as conn:
        kind, detail = conn.execute(text(
            "SELECT kind, would_change_detail FROM places_runs WHERE status = 'ok'")).one()
    assert kind == "reassign" and sorted(json.loads(detail)["changed_ids"]) == sorted(wrong)
    assert kit.assign(engine, dry_run=True).would_change == {}


def test_sample_has_house_evidence_columns_and_hromada_stratum(tmp_path):
    rows = [(i, 10 + i, "domria", dict(district="Бам", complex_name="ЖК Козацький",
                                      location="вул. Фізкультурна, 27")) for i in range(1, 4)]
    rows.append((5, 20, "lun", dict(location="Фізкультурна вул., 27, Бам")))
    rows.append((6, 21, "lun", dict(location="Кераміків вул., 26, Кладовище, Крихівці "
                                              "(Івано-Франківськ)")))
    engine = _db(tmp_path, rows, "sample.db")
    kit.assign(engine)
    out = tmp_path / "sample.csv"
    r = _cli(_cli_env(tmp_path, "sample.db"), "places", "sample", "--out", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    got = list(csv.DictReader(out.open(encoding="utf-8")))
    assert {"ria_district_same_addr", "lun_label_same_addr", "zhk_same_addr"} <= set(got[0])
    lun = next(x for x in got if x["id"] == "5")
    assert lun["ria_district_same_addr"] == "Бам 3" and lun["zhk_same_addr"] == "ЖК Козацький 3"
    ria = next(x for x in got if x["id"] == "1")
    assert ria["lun_label_same_addr"] == "Бам 1" and ria["ria_district_same_addr"] == "Бам 2"
    assert any(x["шар"] == "lun/area/hromada" and x["id"] == "6" for x in got)


# --- Вимикач кроку в циклі -----------------------------------------------------------------


def test_cycle_step_off_until_owner_review(tmp_path, monkeypatch):
    """Вимикач кроку: з `enabled = false` кроку в циклі немає й він нічого не пише.

    Робочий config/cycle.toml після рішення власника 08.10 (варіант (а), коміт
    720d7d1) має `enabled = true`, тож вимкнений стан — на копії конфігів."""
    import re
    import shutil

    from realty import runner

    cfg_dir = tmp_path / "config"
    shutil.copytree(configfiles.config_dir(), cfg_dir)
    cycle = cfg_dir / "cycle.toml"
    cycle.write_text(re.sub(r"(?m)^enabled = true$", "enabled = false", cycle.read_text()))
    monkeypatch.setenv(configfiles.ENV_DIR, str(cfg_dir))
    assert configfiles.load("cycle").places.enabled is False
    assert runner.PLACES_STEP not in [s.name for s in runner.default_steps(sources=["domria"])]
    engine = kit.make_engine(tmp_path, "gate.db")
    kit.build(engine, n=30)
    before = kit.raw_checksum(engine), _keys(engine)
    r = _cli({**_cli_env(tmp_path, "gate.db", REALTY_CYCLE_STEP=runner.PLACES_STEP),
              configfiles.ENV_DIR: str(cfg_dir)},
             "places", "assign")
    assert r.returncode == 0 and "вимкнено до перегляду вибірки" in r.stdout, r.stdout
    assert (kit.raw_checksum(engine), _keys(engine)) == before


def test_dry_run_without_s4_refuses_and_leaves_schema(tmp_path):
    """`places assign --dry-run` до `db migrate` не додає колонок поза міграцією."""
    from sqlalchemy import inspect

    engine = kit.make_engine(tmp_path, "old.db")
    kit.build(engine, n=20)
    with engine.begin() as conn:
        for name in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' "
                                      "AND tbl_name = 'listings' AND sql IS NOT NULL")).scalars().all():
            conn.execute(text(f'DROP INDEX "{name}"'))
        for col in ("district_key", "district_how", "complex_key", "complex_how",
                    "place_area", "place_at", "place_sig", "row_district", "row_complex",
                    "row_area"):
            conn.execute(text(f"ALTER TABLE listings DROP COLUMN {col}"))
        cols_before = [c["name"] for c in inspect(conn).get_columns("listings")]
    env = _cli_env(tmp_path, "old.db")
    r = _cli(env, "places", "assign", "--dry-run")
    assert r.returncode == 2 and "db migrate" in r.stdout, r.stdout + r.stderr
    with engine.connect() as conn:
        assert [c["name"] for c in inspect(conn).get_columns("listings")] == cols_before
    r = _cli(env, "db", "migrate", "--dry-run")
    assert r.returncode == 0 and "ops.db" in r.stdout and "places_runs" in r.stdout, r.stdout


# --- Сторож -------------------------------------------------------------------------------------


def test_watchdog_would_change_once_per_state(tmp_path, monkeypatch):
    from realty import watchdog

    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", eng)
    monkeypatch.setattr(ops, "OpsSession", sessionmaker(bind=eng, expire_on_commit=False,
                                                        future=True))
    ops.init_ops(force=True)
    monkeypatch.setattr(watchdog, "collect", lambda now, state: watchdog.check_places(now))
    monkeypatch.setattr(watchdog, "_header", lambda: "[test]")
    sent = []
    t0 = datetime(2026, 10, 8, 12, 0)
    state = tmp_path / "alerts.json"

    def add(n, at):
        with ops.ops_session() as s:
            s.add(ops.PlacesRun(at=at, status="ok", kind="cycle", would_change=n,
                                directory_ver="v1", rules_hash="r1",
                                would_change_detail=json.dumps({"by_field": {"complex": n}})))

    add(3, t0)
    watchdog.run(t0, send=sent.append, state_path=state)
    for h in (3, 7, 13, 25):                         # той самий стан — мовчки
        add(3, t0 + timedelta(hours=h))
        watchdog.run(t0 + timedelta(hours=h), send=sent.append, state_path=state)
    assert len(sent) == 1 and "reassign" in sent[0]
    add(5, t0 + timedelta(hours=26))                 # новий стан — одне повідомлення
    watchdog.run(t0 + timedelta(hours=26), send=sent.append, state_path=state)
    watchdog.run(t0 + timedelta(hours=40), send=sent.append, state_path=state)
    assert len(sent) == 2 and "5 вже визначених" in sent[1]


# --- flombu: точність точки й населений пункт ----------------------------------------------------

# Локації з живої відповіді flombu (проба Етапу 0, 12 записів): route / routeNumber.
FLOMBU_PROBE = [
    ("Петриків", "", ""), ("Тернопіль", "вулиця Львівська", ""), ("Тернопіль", "", ""),
    ("Тернопіль", "Валова вулиця", ""), ("Тернопіль", "бульвар Данила Галицького", ""),
    ("Байківці", "", ""), ("Байківці", "", ""), ("Тернопіль", "вулиця Київська", "18"),
    ("Тернопіль", "вулиця Митрополита Шептицького", ""), ("Байківці", "", ""),
    ("Байківці", "", ""), ("Байківці", "", ""),
]


def test_flombu_point_precision_from_geocoding_level():
    from realty import identity

    got = [identity.from_flombu({}, {"latitude": 49.55, "longitude": 25.6, "locality": loc,
                                     "route": route, "routeNumber": num})["geo"]
           for loc, route, num in FLOMBU_PROBE]
    assert got.count("building") == 1 and got[7] == "building"
    assert {g for i, g in enumerate(got) if i != 7} <= {"street", "locality"}
    assert sum(g in identity.PRECISE for g in got) == 1
    a = identity.from_flombu({}, {"latitude": 48.92, "longitude": 24.71, "route": "Галицька"})
    b = identity.from_flombu({}, {"latitude": 48.95, "longitude": 24.75, "route": "Галицька"})
    assert identity.distance_m(a, b) is None         # вето відстані зведення не спрацює


def test_flombu_locality_inside_box_must_be_city_or_directory_place():
    from test_places_sources import _flombu_item, _parse

    from realty.sources.flombu import FlombuSource

    src = FlombuSource()
    item, inc = _flombu_item(21, lat=48.90, lon=24.825, locality="Тисмениця",
                             address="вул. Галицька, 1")
    assert _parse(src, item, inc) == {}
    assert src.stats["skipped_locality"] == 1
    item, inc = _flombu_item(22, lat=48.93, lon=24.70, locality="Чукалівка")
    assert _parse(src, item, inc)["district"] == "Чукалівка"
    item, inc = _flombu_item(23, lat=48.92, lon=24.71, locality="Івано-Франківськ")
    assert _parse(src, item, inc)
