"""Сильні ознаки квартири й будинку — на збережених реальних сторінках."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty import identity

PROBES = Path(__file__).resolve().parent.parent / "probes"


def test_domria_card_gives_flat_building_complex_and_seller():
    card = {"flat_entity_id": "6a9d39c0", "building_entity_id": "6981afa0", "osm_building_id": 1390405407,
            "newbuild_id": 5556, "user_newbuild_name_uk": "ЖК Manhattan",
            "building_number_str": "34 корпус 9", "latitude": 48.9210883, "longitude": 24.68830618,
            "user_id": 15062930}
    ident = identity.from_domria(card)
    assert ident["flat"] == "ria:6a9d39c0" and ident["building"] == "ria:6981afa0"
    assert ident["complex"] == "ria:5556" and ident["korpus"] == "34 корпус 9"
    assert ident["seller"] == "ria:15062930" and ident["geo"] == "point"


@pytest.mark.skipif(not (PROBES / "_lun_sample.json").exists(), reason="немає зразка LUN")
def test_lun_item_gives_duplicate_group_and_house():
    item = json.loads((PROBES / "_lun_sample.json").read_text())
    ident = identity.from_lun(item)
    assert ident["group"] == "lun:100701609798972958"          # LUN сам зводить дублі
    assert ident["building"] == "lun:10886945" and ident["korpus"] == "Будинок 3"
    assert ident["geo"] == "building" and 48.9 < ident["lat"] < 49.0
    assert "phone" not in json.dumps(ident)                    # контакти не зберігаємо


def test_olx_coordinates_are_marked_approximate():
    html = r'{\"map\":{\"lat\":48.91819,\"lon\":24.71343,\"radius\":2},\"user\":{\"id\":2095211563},\"isBusiness\":true}'
    ident = identity.from_olx_page(html)
    assert ident["geo"] == "approx" and ident["seller"] == "olx:2095211563"
    # Розмита точка OLX ніколи не дає відстані для порівняння корпусів.
    assert identity.distance_m(ident, {"lat": 48.9, "lon": 24.7, "geo": "point"}) is None


@pytest.mark.parametrize("text, code", [
    ("34 корпус 9", "34к9"), ("34/7", "34к7"), ("3 корпус 4", "3к4"), ("18/1", "18к1"),
    ("29А", None), ("34", None), ("Будинок 3", None), ("", None), (None, None),
])
def test_korpus_code(text, code):
    assert identity.korpus_code(text) == code


def test_distance_between_real_buildings_of_one_complex():
    # ЖК Manhattan: корпус 9 і корпус «34/7» за координатами DIM.RIA.
    a = {"lat": 48.9210883, "lon": 24.68830618, "geo": "point"}
    b = {"lat": 48.92043322, "lon": 24.68865294, "geo": "point"}
    assert 70 < identity.distance_m(a, b) < 85
