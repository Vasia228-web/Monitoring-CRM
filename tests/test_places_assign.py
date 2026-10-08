"""Крок циклу «райони й ЖК» (`cli.py places assign`; Блок 4, E10, D57).

Межі: сирі поля не змінюються ніколи (контрольна сума до/після); ключі — лише туди, де
порожньо або «не визначено»; зміна вже визначеного лише рахується (would_change); row_*
— значення квартири; крок — перед «дублі» зі стелею з config/cycle.toml.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import places_kit as kit  # noqa: E402
from realty import configfiles, ops  # noqa: E402
from realty.places import directory  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine = kit.make_engine(tmp_path)
    kit.build(engine)
    return engine


@pytest.fixture
def ops_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", eng)
    monkeypatch.setattr(ops, "OpsSession", sessionmaker(bind=eng, expire_on_commit=False,
                                                        future=True))
    ops.init_ops(force=True)
    return eng


def _keys(engine):
    with engine.connect() as conn:
        return {r[0]: tuple(r[1:]) for r in conn.execute(text(
            "SELECT id, district_key, district_how, complex_key, complex_how, place_area, "
            "row_district, row_complex, row_area, property_id FROM listings"))}


def test_raw_columns_untouched_and_keys_filled(db):
    before = kit.raw_checksum(db)
    rep = kit.assign(db)
    assert kit.raw_checksum(db) == before                     # сирі поля й last_seen ті самі
    assert rep.filled["district"] > 0 and rep.filled["complex"] > 0
    assert rep.would_change == {} and rep.stopped is None
    keys = _keys(db)
    # Рядок без квартири — власні ключі; з квартирою — ключі квартири (одні на всю квартиру).
    with db.connect() as conn:
        props = {r[0]: tuple(r[1:]) for r in conn.execute(text(
            "SELECT id, district_key, complex_key, place_area FROM properties"))}
    for lid, (dk, dh, ck, ch, area, rd, rc, ra, pid) in keys.items():
        if pid is None:
            assert (rd, rc, ra) == (dk, ck, area)
        else:
            assert (rd, rc, ra) == props[pid]
    # Повторний прогін нічого не пише.
    again = kit.assign(db)
    assert not again.wrote and again.filled == {} and kit.raw_checksum(db) == before


def test_fill_only_counts_would_change_and_never_overwrites(db, tmp_path, monkeypatch):
    """Довідник змінено так, що визначений ЖК став би іншим: крок рахує would_change і НЕ
    пише; «не в ЖК» — уточнюється конкретним ЖК (дозволено)."""
    on = kit.rules_with_secondary()
    kit.assign(db, rules=on)
    with db.begin() as conn:
        # Рядок «не в ЖК» отримує ознаку ЖК у полі → має уточнитися.
        lid = conn.execute(text("SELECT id FROM listings WHERE complex_key = '_none' "
                                "AND source = 'domria' LIMIT 1")).scalar()
        assert lid is not None
        conn.execute(text("UPDATE listings SET identity = :i WHERE id = :id"),
                     {"i": json.dumps({"complex": "ria:5902"}), "id": lid})
    before_keys = _keys(db)
    d = directory.load()
    # «Перенести» Comfort Park (ria 5902) на інший ЖК: копія довідника, де id належить Silver.
    comfort, silver = d.complexes["comfort-park"], d.complexes["silver"]
    d2 = directory.Directory.build(
        configfiles.load("places/districts"),
        replace(configfiles.load("places/complexes"), complex=tuple(
            replace(c, ria_ids=()) if c.key == "comfort-park" else
            replace(c, ria_ids=(*c.ria_ids, 5902)) if c.key == "silver" else c
            for c in configfiles.load("places/complexes").complex)),
        configfiles.load("places/rules"))
    assert comfort.ria_ids == (5902,) and d2.complex_by_ria(5902) == "silver"
    rep = kit.assign(db, d=d2, rules=on)
    after = _keys(db)
    assert rep.would_change["complex"] > 0
    # Жоден непорожній конкретний ЖК не змінився …
    for i, (b, a) in enumerate(zip(before_keys.values(), after.values())):
        if b[2] not in (None, "_none"):
            assert a[2] == b[2]
    # … а «не в ЖК» уточнено (заповнення, не зміна).
    assert after[lid][2] == "silver" and before_keys[lid][2] == "_none"
    assert silver.key == "silver"


def test_unknown_names_are_reported_and_shown_to_owner_only(db, ops_db, monkeypatch):
    from realty.places import assign as step
    from realty.places import commands

    rep = kit.assign(db)
    names = {e["name"]: e for e in rep.unknown}
    assert "ЖК Невідомий Двір" in names and names["ЖК Невідомий Двір"]["field"] == \
        "domria.complex_name"
    assert "Зовсім Новий ЖК" in names                        # OLX «Назва ЖК»
    # POI й орієнтири — не «нерозпізнані».
    assert not {"ТЦ \"Арсен\"", "Міське озеро", "ТЦ Панорама PLAZA"} & set(names)
    step.record(rep, d=directory.load(), rules_hash="t", kind="manual", status="ok")
    last = commands.last_run()
    assert last["status"] == "ok" and any(e["name"] == "ЖК Невідомий Двір"
                                          for e in last["unknown"])


def test_precision_gate_disables_weak_tier(db, monkeypatch):
    """Координати ввімкнено, але самоперевірка нижча за поріг — нічого «за координатами»."""
    rules = configfiles.load("places/rules")
    on = replace(rules, tiers=replace(rules.tiers, coords_enabled=True),
                 coords=replace(rules.coords, min_precision_district=1.01,
                                min_precision_complex=1.01))
    rep = kit.assign(db, rules=on)
    assert rep.precision["coords_district"]["enabled"] is False
    assert not any(k.endswith(":coords") for k in rep.tiers)
    assert all(v[1] != "coords" for v in _keys(db).values())


def test_dry_run_writes_nothing(db):
    before = kit.raw_checksum(db), _keys(db)
    rep = kit.assign(db, dry_run=True)
    assert rep.filled["district"] > 0
    assert (kit.raw_checksum(db), _keys(db)) == before


def test_batch_with_foreign_change_is_rolled_back(db, monkeypatch):
    """Відбиток інших колонок пакета змінився під час запису — ROLLBACK і стоп."""
    from realty.places import assign as step

    real = step._apply_listing_plans

    def sneaky(conn, chunk, plans, rep=None):
        real(conn, chunk, plans, rep)
        conn.execute(text("UPDATE listings SET title = 'зіпсовано' WHERE id = :id"),
                     {"id": chunk[0]})

    monkeypatch.setattr(step, "_apply_listing_plans", sneaky)
    before = kit.raw_checksum(db)
    with pytest.raises(step._Mismatch):
        kit.assign(db)
    assert kit.raw_checksum(db) == before


def _cycle_enabled(monkeypatch, enabled: bool):
    real = configfiles.load
    cfg = real("cycle")
    on = replace(cfg, places=replace(cfg.places, enabled=enabled))
    monkeypatch.setattr(configfiles, "load", lambda name: on if name == "cycle" else real(name))
    return on


def test_places_step_runs_before_dedup_with_ceiling_from_cycle_toml(monkeypatch):
    from realty import runner

    _cycle_enabled(monkeypatch, True)
    names = [s.name for s in runner.default_steps(sources=["domria"])]
    i = names.index("райони й ЖК")
    assert names[i - 1] == "перевірка актуальності" and names[i + 1] == "дублі"
    step = runner.default_steps(sources=["domria"])[i]
    assert step.argv[-2:] == ["places", "assign"]
    assert step.timeout == configfiles.load("cycle").places.timeout_min * 60
    assert "райони й ЖК" in runner.ANALYTICS_STEPS


def test_broken_cycle_toml_skips_only_the_places_step(tmp_path, monkeypatch):
    from realty import runner

    cfg = tmp_path / "config"
    for src in (ROOT / "config").rglob("*.toml"):
        p = cfg / src.relative_to(ROOT / "config")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (cfg / "cycle.toml").write_text("[places]\ntimeout_min = \"десять\"\n", encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(cfg))
    names = [s.name for s in runner.default_steps(sources=["domria"])]
    assert "райони й ЖК" not in names and "дублі" in names and "перевірка актуальності" in names


def test_cli_assign_in_a_copy_records_run_and_refuses_broken_directory(tmp_path):
    """Справжній `cli.py places assign` окремим процесом на копії: код 0, рядок прогону;
    зламаний довідник — відмова з кодом 1, база без змін."""
    import os
    import subprocess

    engine = kit.make_engine(tmp_path, "cli.db")
    kit.build(engine, n=40)
    before = kit.raw_checksum(engine)
    cfg = tmp_path / "config"
    for src in (ROOT / "config").rglob("*.toml"):
        p = cfg / src.relative_to(ROOT / "config")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    cycle = cfg / "cycle.toml"
    cycle.write_text(cycle.read_text(encoding="utf-8").replace("enabled = false",
                                                              "enabled = true"),
                     encoding="utf-8")
    env = {**os.environ, "DB_URL": f"sqlite:///{tmp_path / 'cli.db'}",
           "OPS_DB_URL": f"sqlite:///{tmp_path / 'cli_ops.db'}", "REALTY_CYCLE_STEP": "тест",
           configfiles.ENV_DIR: str(cfg)}
    r = subprocess.run([sys.executable, "cli.py", "places", "assign"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "РАЙОНИ Й ЖК" in r.stdout and kit.raw_checksum(engine) == before
    ops_eng = create_engine(env["OPS_DB_URL"], future=True)
    with ops_eng.connect() as conn:
        status, kind = conn.execute(text("SELECT status, kind FROM places_runs")).one()
        gens = dict(conn.execute(text("SELECT name, gen FROM web_generations")).all())
    assert (status, kind) == ("ok", "cycle")
    # Квартири отримали район і ЖК — знімок «Аналітики» й список сайту застаріли.
    assert gens.get("analytics", 0) >= 1 and gens.get("lists", 0) >= 1
    (cfg / "places" / "rules.toml").write_text("зламано = [", encoding="utf-8")
    r = subprocess.run([sys.executable, "cli.py", "places", "assign"], cwd=ROOT,
                       env={**env, configfiles.ENV_DIR: str(cfg)}, capture_output=True,
                       text=True, timeout=180)
    assert r.returncode == 1 and "ВІДМОВА" in r.stdout
