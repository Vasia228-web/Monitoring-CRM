"""Ступені визначення району й ЖК і значення квартири (Блок 4, E10, D57)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles  # noqa: E402
from realty.places import extract  # noqa: E402
from realty.places.directory import NONE, load  # noqa: E402
from realty.places.resolve import property_place, resolve  # noqa: E402


def row(source, **kw):
    base = dict(id=1, source=source, market_type="primary", property_id=None, district=None,
                complex_name=None, location=None, title=None, identity=None, place_raw=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _res(r, *, rules=None, **kw):
    v = extract.view(r, zhk_words=kw.pop("zhk", None))
    return resolve(v, load(), rules or configfiles.load("places/rules"), **kw)


def _secondary_rules():
    from dataclasses import replace

    rules = configfiles.load("places/rules")
    return replace(rules, secondary=replace(rules.secondary, markets=("secondary",)))


def test_poi_never_becomes_district():
    """LUN пише в district найближчий POI (Етап 0: 5 059 квартир із POI замість району)."""
    r = _res(row("lun", district="ТЦ Панорама PLAZA", location="Івано-Франківськ"))
    assert r.district_key is None and r.unknown == []
    r = _res(row("lun", district='ТЦ "Арсен"', location="Мазепи вул., 1, Міське озеро"))
    assert r.district_key is None and r.unknown == []        # орієнтир — не район


def test_tier_priority_and_provenance():
    # (а) район ЖК сильніший за район агента: Comfort Park → Пасічна, «за ЖК».
    r = _res(row("domria", district="Центр", complex_name="ЖК Comfort Park",
                 identity={"complex": "ria:5902"}))
    assert (r.complex_key, r.complex_how) == ("comfort-park", "src_id")
    assert (r.district_key, r.district_how, r.area) == ("pasichna", "complex_src", "city")
    assert r.district_src == "tsentr"
    # (б) без ЖК — район джерела.
    r = _res(row("domria", district="Центр"))
    assert (r.district_key, r.district_how) == ("tsentr", "src")
    # (в) ЖК з довідника без прив'язки (U One): поле агента там збігається з будинком лише
    # в 70,7% (рецензія E10) — району немає (лише «за будинком», якщо є докази).
    r = _res(row("domria", district="Софіївка", identity={"complex": "ria:9316"}))
    assert (r.complex_key, r.district_key, r.district_how) == ("u-one", None, None)
    assert r.district_src == "sofiivka"
    # (г) мітка LUN з латинською «i», село з суфіксом, «поза громадою».
    assert _res(row("lun", location="Хіміків вул., 28, Пасiчна")).district_key == "pasichna"
    r = _res(row("lun", location="Кераміків вул., 26, Кладовище, Крихівці (Івано-Франківськ)"))
    assert (r.district_key, r.area) == ("krykhivtsi", "hromada")
    assert _res(row("lun", location="Лисець")).area == "outside"
    # (д) мікрорайон з geoEntities LUN — сильніший за мітку; ЖК з geoEntities — за назвою.
    r = _res(row("lun", location="Галицька вул., 5, Центр",
                 place_raw={"lun_geo": [{"type": "microdistrict", "id": 1, "name": "Пасiчна"},
                                        {"type": "residential_complex", "id": 2,
                                         "name": "ЖК Comfort Park"}]}))
    assert (r.district_key, r.complex_key, r.complex_how) == ("pasichna", "comfort-park", "src_name")
    # (е) OLX «Назва ЖК» і Благо — назва з поля; flombu — населений пункт.
    assert _res(row("olx", place_raw={"olx_zhk": "Comfort Park"})).district_key == "pasichna"
    assert _res(row("blago", complex_name="SKYGARDEN")).complex_key == "skygarden"
    assert _res(row("flombu", place_raw={"flombu_locality": "Чукалівка"})).district_key == "chukalivka"
    assert _res(row("flombu", place_raw={"flombu_locality": "Івано-Франківськ"})).district_key is None


def test_ria_id_with_several_names_resolves_by_name_and_unknown_is_reported():
    r = _res(row("domria", complex_name="ЖК Стожари", identity={"complex": "ria:6420"}))
    assert r.complex_key is None and ("domria.complex_name", "ЖК Стожари") in r.unknown
    r = _res(row("domria", complex_name="ЖК Невідомий Двір", identity={"complex": "ria:1"}))
    assert r.complex_key is None and r.unknown == [("domria.complex_name", "ЖК Невідомий Двір")]


def test_not_in_complex_only_for_secondary_without_signals():
    on = _secondary_rules()
    assert _res(row("domria", market_type="secondary", district="Центр"),
                zhk=False, rules=on).complex_key == NONE
    # Слово «ЖК» у тексті — не «не в ЖК», а «не визначено».
    assert _res(row("domria", market_type="secondary", district="Центр"),
                zhk=True, rules=on).complex_key is None
    # Первинка без ЖК — «не визначено», а не «не в ЖК».
    assert _res(row("olx", market_type="primary"), zhk=False, rules=on).complex_key is None
    # Вторинка з ЖК у полі — ЖК.
    r = _res(row("domria", market_type="secondary", identity={"complex": "ria:5902"}),
             zhk=False, rules=on)
    assert r.complex_key == "comfort-park"


def test_weak_tiers_off_by_default():
    rules = configfiles.load("places/rules")
    assert not rules.tiers.coords_enabled and not rules.tiers.text_enabled
    r = _res(row("lun", title="Продаж у ЖК Senat", location="Івано-Франківськ"))
    assert r.complex_key is None                         # заголовок — лише після E11


def test_property_place_majority_tie_umbrella_and_parent():
    d = load()
    # Сильніший ступінь перемагає слабший; у ньому — більшість.
    assert property_place([("pasichna", "src", None, None), ("pasichna", "src", None, None),
                           ("kaskad", "coords", None, None)], d)[:3] == ("pasichna", None, "city")
    # Нічия — None і запис у place_conflict.
    dk, ck, area, conflict = property_place([("pasichna", "src", None, None),
                                             ("kaskad", "src", None, None)], d)
    assert dk is None and area is None and conflict == {"district": ["kaskad", "pasichna"]}
    # Батько й дитина — не протиріччя: конкретніший.
    assert property_place([("tsentr", "src", None, None),
                           ("nimetska-koloniia", "src", None, None)], d)[0] == "nimetska-koloniia"
    # Парасолька поступається ЖК усередині себе; «не в ЖК» — конкретному ЖК.
    assert property_place([(None, None, "kniahynyn", "src_id"),
                           (None, None, "kniahynyn-center", "src_id")], d)[1] == "kniahynyn-center"
    assert property_place([(None, None, NONE, "secondary"),
                           (None, None, "comfort-park", "src_name")], d)[1] == "comfort-park"
    assert property_place([(None, None, NONE, "secondary")], d)[1] == NONE
