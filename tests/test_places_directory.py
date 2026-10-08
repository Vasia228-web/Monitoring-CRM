"""Довідник районів і ЖК config/places/ (Блок 4, E10, D57): узгоджений, без колізій."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles  # noqa: E402
from realty.places.directory import DirectoryError, load  # noqa: E402

CONFIG = ROOT / "config"


def test_directory_is_consistent():
    d = load()
    assert len(d.districts) >= 27 and len(d.complexes) >= 150
    # Ієрархія: «Центр» включає «Німецька колонія» (доказ ≥80% на Етапі 0), підпис — з батьком.
    assert "nimetska-koloniia" in d.family("tsentr")
    assert d.label("nimetska-koloniia") == "Центр (Німецька колонія)"
    # Села громади й «поза громадою» (рішення власника 4, D46).
    assert d.area("krykhivtsi") == "hromada" and d.area("lysets") == "outside"
    assert d.area("pasichna") == "city"
    # Міське озеро — орієнтир, не район (D57); POI LUN — теж не район.
    assert d.match_district("Міське озеро ") == ("ignore", "landmark")
    assert d.match_district('ТЦ "Арсен"') == ("ignore", "poi")
    assert d.match_district("Пасiчна") == ("district", "pasichna")
    # ЖК: id DOM.RIA, «за назвою» для id з кількома назвами, парасолька.
    assert d.complex_by_ria(5902) == "comfort-park"
    assert d.complex_by_ria(6420) is None                    # SHEPIT/Стожари — за назвою
    assert d.match_complex("ЖК SHEPIT") == ("complex", "shepit")
    assert d.complexes["kniahynyn-center"].within == "kniahynyn"
    assert d.related("kniahynyn", "kniahynyn-center") == "umbrella"
    assert d.related("family-plaza", "family-plaza-2") == "phase"
    assert d.related("shepit", "skygarden") == "different"
    # Прив'язка ЖК → район лише за доказом: Comfort Park (Пасічна 99%) — так; U One — ні.
    assert d.complex_district("comfort-park") == "pasichna"
    assert d.complex_district("u-one") is None


@pytest.fixture
def cfg_copy(tmp_path, monkeypatch):
    dst = tmp_path / "config"
    for src in CONFIG.rglob("*.toml"):
        p = dst / src.relative_to(CONFIG)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(dst))
    return dst


def _complexes_without_check():
    """complexes.toml, прочитаний схемою без перевірки довідника цілком."""
    import tomllib

    data = tomllib.loads(configfiles.path_for("places/complexes").read_text(encoding="utf-8"))
    errors: list[str] = []
    cls = configfiles.PlacesComplexesConfig
    original = cls._directory_problems
    cls._directory_problems = lambda self: []
    try:
        value = configfiles._build(cls, data, "", errors)
    finally:
        cls._directory_problems = original
    assert not errors, errors
    return value


def test_alias_collision_is_refused(cfg_copy):
    """Штучна колізія («Senat» як псевдонім Sonata) — DirectoryError і `config check` ≠ 0."""
    p = cfg_copy / "places" / "complexes.toml"
    text = p.read_text(encoding="utf-8")
    marker = 'name = "Sonata"\n'
    assert text.count(marker) == 1
    start = text.index(marker)
    alias_line = text.index("aliases = [", start)
    end = text.index("\n", alias_line)
    p.write_text(text[:alias_line] + 'aliases = ["Senat"]' + text[end:], encoding="utf-8")
    # Схема complexes.toml сама збирає довідник: колізія — ConfigError уже при читанні.
    with pytest.raises(configfiles.ConfigError, match="уже веде до"):
        load()
    with pytest.raises(DirectoryError) as e:
        from realty.places.directory import Directory
        Directory.build(configfiles.load("places/districts"),
                        _complexes_without_check(), configfiles.load("places/rules"))
    assert any("уже веде до" in x for x in e.value.problems)
    env = {**os.environ, configfiles.ENV_DIR: str(cfg_copy)}
    r = subprocess.run([sys.executable, "cli.py", "config", "check", "--allow-override"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode != 0 and "довідник" in r.stdout, r.stdout + r.stderr


def test_unknown_parent_and_district_are_refused(cfg_copy):
    p = cfg_copy / "places" / "districts.toml"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace('parent = "tsentr"', 'parent = "nemaie"', 1), encoding="utf-8")
    with pytest.raises(configfiles.ConfigError, match="такого району немає"):
        configfiles.load("places/districts")


def test_every_source_district_in_db_copy_is_known():
    """Кожне значення поля району DOM.RIA і кожна мітка LUN з копії бази (conftest)
    розпізнається як район або як відома не-районна назва (POI, орієнтир, вулиця)."""
    from sqlalchemy import text

    from realty.db import engine
    from realty.places.extract import lun_label

    d = load()
    unknown = {}
    with engine.connect() as conn:
        for src, district, location in conn.execute(text(
                "SELECT source, district, location FROM listings WHERE source IN ('domria', 'lun')")):
            value = district if src == "domria" else lun_label(location)
            if not value or not str(value).strip():
                continue
            kind, _ = d.match_district(value)
            if kind == "unknown":
                unknown[value] = unknown.get(value, 0) + 1
    assert not unknown, f"нерозпізнані назви району (додати в config/places/districts.toml): {unknown}"
