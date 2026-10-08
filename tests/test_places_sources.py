"""Докази місця від збирачів (Блок 4, E10, D57): нових запитів немає, сирі поля ті самі.

flombu — виправлений зв'язок `location` (координати й населений пункт) і місто за
координатами (BBOX): до E10 зв'язок шукався за ключем 'estateRecordLocation', координат
не було в жодному з 27 оголошень, а місто визначав лише текст адреси.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty.sources.flombu import FlombuSource  # noqa: E402
from realty.sources.lun import LunSource  # noqa: E402


def _flombu_item(i: int, *, lat, lon, locality, address="м. Івано-Франківськ",
                 key="location"):
    item = {"id": i, "type": "estateDealSale",
            "attributes": {"type2HumanVal": "Квартира", "price": 50000 + i,
                           "priceCurrency": "USD", "title": "2-кімнатна квартира",
                           "addressToStreet": address, "estateSizeHumanVal": "50 м²",
                           "tileEstateAccentAttrs": ["2 кімнати"],
                           "publishedAtHumanVal": "сьогодні", "ownerType": "agent"},
            "relationships": {key: {"data": {"type": "estateRecordLocation", "id": str(i)}}}}
    inc = {"type": "estateRecordLocation", "id": str(i),
           "attributes": {"latitude": lat, "longitude": lon, "locality": locality,
                          "sublocality1": "", "originalAddress": "",
                          "route": "Тестова вулиця"}}
    return item, inc


def _parse(src, item, inc):
    geo = {inc["id"]: inc["attributes"]}
    return src._parse(item, geo)


def test_flombu_location_relationship_gives_coordinates_and_locality():
    src = FlombuSource()
    item, inc = _flombu_item(7, lat=48.92, lon=24.71, locality="Чукалівка")
    rec = _parse(src, item, inc)
    assert rec["identity"]["lat"] == 48.92 and rec["identity"]["lon"] == 24.71
    assert rec["identity"]["geo"] == "street"        # вулиця без номера — не точна точка
    assert rec["place_raw"] == {"flombu_locality": "Чукалівка"}
    assert rec["district"] == "Чукалівка"            # село — як мітка села LUN
    item, inc = _flombu_item(8, lat=48.92, lon=24.71, locality="Івано-Франківськ")
    rec = _parse(src, item, inc)
    assert rec["district"] is None                   # саме місто районом не є


def test_flombu_city_by_coordinates_not_by_address_text():
    """Адреса без назви міста, але точка в межах BBOX — у місті (до E10 — відкинуто);
    точка поза BBOX — ні, хоч би що писав текст."""
    src = FlombuSource()
    item, inc = _flombu_item(9, lat=48.93, lon=24.70, locality="Вовчинець",
                             address="вул. Галицька, 1")
    assert _parse(src, item, inc)                    # у прямокутнику міста
    item, inc = _flombu_item(10, lat=49.80, lon=24.03, locality="Львів",
                             address="м. Івано-Франківськ")
    assert _parse(src, item, inc) == {}
    assert src.stats["skipped_geo"] == 1


def test_flombu_old_relationship_key_still_works():
    src = FlombuSource()
    item, inc = _flombu_item(11, lat=48.92, lon=24.71, locality="Івано-Франківськ",
                             key="estateRecordLocation")
    assert _parse(src, item, inc)["identity"]["lat"] == 48.92


def test_lun_item_geo_entities_are_captured_and_district_unchanged():
    d = {"id": 4721489034, "urlRaw": "https://rieltor.ua/ivano-frankovsk/flats-sale/view/1/",
         "price": 84000, "currency": "usd", "roomCount": 1, "areaTotal": 43,
         "geo": "Героїв Миколаєва вул., Будинок 3, Пасiчна, Івано-Франківськ, "
                "Івано-Франківська область",
         "location": [24.7486, 48.9169], "text": "Квартира",
         "poi": {"name": 'ТЦ "Арсен"'},
         "geoEntities": [
             {"geoId": 10886945, "type": "house", "name": "Будинок 3"},
             {"geoId": 10027096, "type": "microdistrict", "name": "Пасiчна"},
             {"geoId": 10869613, "type": "residential_complex", "name": "ЖК Альпійський"},
             {"geoId": 10008717, "type": "city", "name": "Івано-Франківськ"}]}
    rec = LunSource()._parse(d)
    assert rec["district"] == 'ТЦ "Арсен"'           # сире поле — як було (POI)
    raw = dict(rec["place_raw"])
    assert raw.pop("lun_geo_checked_at")             # прохід із geoEntities був
    assert raw == {"lun_geo": [
        {"type": "microdistrict", "id": 10027096, "name": "Пасiчна"},
        {"type": "residential_complex", "id": 10869613, "name": "ЖК Альпійський"},
        {"type": "city", "id": 10008717, "name": "Івано-Франківськ"}]}


def test_olx_detail_reads_zhk_param():
    from realty.sources.olx import parse_detail

    html = ('<div data-testid="ad-parameters-container"><p>Поверх: 3</p>'
            '<p>Назва ЖК: Comfort Park</p><p>Загальна площа: 50 м²</p></div>')
    out = parse_detail(html)
    assert out["place_raw"]["olx_zhk"] == "Comfort Park"
    assert "olx_checked_at" in out["place_raw"]


def test_rieltor_complex_block():
    from realty.sources.rieltor import parse_detail

    html = ('<div class="offer-view-address-newhouse"><div class="ldb__complex">'
            '<a class="ldb__complex-name">ЖК Паркова Алея</a>'
            '<span class="ldb__complex-address">Івано-Франківськ, Героїв Миколаєва вул.</span>'
            '</div></div>')
    assert parse_detail(html)["place_raw"]["rieltor_zhk"] == "ЖК Паркова Алея"


def test_domria_card_keeps_district_id_and_newbuild():
    from realty.sources.domria import DomRiaSource

    card = {"city_id": 15, "realty_id": 1, "beautiful_url": "x-1.html", "price": 50000,
            "district_name_uk": "Набережна", "district_id": 12345, "newbuild_id": 5556,
            "user_newbuild_name_uk": "ЖК Manhattan", "street_name_uk": "Ленкавського",
            "building_number_str": "34"}
    rec = DomRiaSource()._parse(card)
    assert rec["district"] == "Набережна" and rec["complex_name"] == "ЖК Manhattan"
    assert rec["place_raw"] == {"ria_district_id": 12345, "ria_district": "Набережна",
                                "ria_newbuild_id": 5556}


@pytest.mark.parametrize("raw,stored,expect", [
    ({"lun_geo": [{"type": "microdistrict", "id": 1, "name": "Центр"}]},
     {"lun_geo": [{"type": "microdistrict", "id": 2, "name": "Пасічна"}]},
     {"lun_geo": [{"type": "microdistrict", "id": 2, "name": "Пасічна"}]}),
    ({"olx_zhk": "Senat"}, {"olx_checked_at": "2026-10-01"},
     {"olx_checked_at": "2026-10-01", "olx_zhk": "Senat"}),
])
def test_place_raw_is_fill_only(raw, stored, expect):
    """Наявний ключ place_raw не перезаписується, нові — додаються (FILL_ONLY_JSON)."""
    from types import SimpleNamespace

    from realty import pipeline

    existing = SimpleNamespace(place_raw=stored, seller_evidence=None, seller_profile=None)
    pipeline._fill_only(existing, {"place_raw": raw})
    assert existing.place_raw == expect
