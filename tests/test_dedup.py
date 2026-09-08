"""Міжплатформна дедуплікація: нормалізація адрес і кластеризація."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty.dedup import (
    MERGE_THRESHOLD, Shape, cluster, conflicts, match_score, normalize_address,
    street_from_text, street_overlap,
)


def _sh(i, source, rooms=2, area=60.0, floor=5, street=None, house=None,
        district=None, price=70000.0, url=None):
    houses = frozenset([house] if isinstance(house, str) else (house or ()))
    return Shape(i, source, url, rooms, area, floor, street, houses, district, price)


def test_address_normalisation_is_order_independent():
    """DIM.RIA і LUN пишуть ту саму адресу по-різному."""
    a, ha = normalize_address("вул. Гетьмана Івана Мазепи, 168")
    b, hb = normalize_address("Гетьмана Івана Мазепи вул., Будинок 168, Бам")
    assert ha == hb == {"168"}
    assert street_overlap(a, b) == 1.0          # район у LUN не заважає


def test_city_only_address_yields_nothing():
    for text in ("Івано-Франківськ", "м. Івано-Франківськ", None):
        street, houses = normalize_address(text)
        assert street is None and not houses


def test_different_streets_do_not_overlap():
    a, _ = normalize_address("вул. Мазепи, 168")
    b, _ = normalize_address("вул. Вовчинецька, 168")
    assert street_overlap(a, b) == 0.0


def test_street_from_text_needs_house_number():
    assert street_from_text("розташована на вул. Височана, 18 у ЖК")[0]
    # Без номера будинку сигнал заслабкий, щоб на нього спиратись.
    assert street_from_text("гарний вид на вул. Хіміків")[0] is None


def test_different_floors_never_merge():
    a = _sh(1, "domria", floor=3, street="галицька", house="64")
    b = _sh(2, "olx", floor=7, street="галицька", house="64")
    assert match_score(a, b) < MERGE_THRESHOLD


def test_different_streets_never_merge():
    """Регресія: бонуси за поверх, площу й ціну дотягували різні вулиці до порога."""
    a = _sh(1, "domria", street="гарбарська")
    b = _sh(2, "domria", street="мудрого софрона", house="27", district="центр")
    assert match_score(a, b) < MERGE_THRESHOLD


def test_similar_units_in_one_building_stay_apart():
    """Забудовник виставляє десятки схожих квартир — 42 і 43 м² не одне й те саме."""
    a = _sh(1, "domria", area=42.0, floor=1, street="національної гвардії", house="16")
    b = _sh(2, "domria", area=43.0, floor=1, street="національної гвардії", house="16",
            price=72000.0)
    assert match_score(a, b) < MERGE_THRESHOLD


def test_same_building_with_different_numbering_merges():
    """Регресія: «Княгинин, 44 корпус 13» і «Будинок 13» — той самий будинок.

    Поки з адреси брався лише перший номер, ці записи вважались різними
    будинками й не зливались.
    """
    a_street, a_house = normalize_address("вул. Княгинин, 44 корпус 13")
    b_street, b_house = normalize_address("Княгинин вул., Будинок 13, Княгинин")
    assert a_house & b_house                      # перетин номерів непорожній

    a = _sh(1, "domria", area=74.0, floor=8, street=a_street, house=a_house, price=135000.0)
    b = _sh(2, "lun", area=74.0, floor=8, street=b_street, house=b_house, price=135000.0)
    assert match_score(a, b) >= MERGE_THRESHOLD


def test_truly_different_houses_do_not_merge():
    a = _sh(1, "domria", street="галицька", house="64")
    b = _sh(2, "lun", street="галицька", house="12")
    assert match_score(a, b) < MERGE_THRESHOLD


def test_addressless_listing_cannot_bridge_two_buildings():
    """Регресія: оголошення OLX без адреси склеїло Івасюка 86 з Височана 18.

    Безадресне оголошення однаково схоже на обидві квартири, і наївне
    об'єднання пар транзитивно зливало два різні будинки в одну групу.
    """
    a = _sh(1, "lun", area=73.0, floor=12, street="івасюка володимира", house="86",
            price=96000.0)
    b = _sh(2, "lun", area=73.0, floor=12, street="височана", house="18", price=97000.0)
    bridge = _sh(3, "olx", area=73.0, floor=12, price=96500.0)

    groups = cluster([a, b, bridge])
    roots = {i: n for n, g in enumerate(groups) for i in g}
    assert roots[1] != roots[2], "різні будинки опинились в одній групі"
    assert not any(conflicts([a, b, bridge][j - 1] for j in g) for g in groups)


def test_addressless_joins_the_single_best_anchor():
    anchor = _sh(1, "domria", area=55.0, floor=9, street="отця блавацького", house="8",
                 price=46600.0)
    same = _sh(2, "olx", area=55.0, floor=9, price=46800.0)
    groups = cluster([anchor, same])
    assert len(groups) == 1 and set(groups[0]) == {1, 2}


def test_identical_url_always_merges():
    """Регресія: LUN агрегує OLX, тож те саме посилання приходить від обох.

    Через це воно ще й впиралось у UNIQUE(original_url) і валило прогін —
    URL не є ключем ідентичності між джерелами.
    """
    url = "https://www.olx.ua/d/uk/obyavlenie/x-ID10aSAP.html"
    a = _sh(1, "olx", area=60.0, floor=9, url=url)
    b = _sh(2, "lun", area=60.0, floor=9, url=url + "?from=lun")
    groups = cluster([a, b])
    assert len(groups) == 1, "однакове посилання має давати один об'єкт"


def test_district_words_do_not_leak_into_street_key():
    """Регресія: LUN дописує район, і на коротких назвах він перетягував збіг.

    «Лемківська вул., Будинок, Міське озеро» і «Романа Левицького вул., 4,
    Міське озеро» мали 0.67 перетину через слова району — і зливались.
    """
    a, _ = normalize_address("Лемківська вул., Будинок, Міське озеро")
    b, _ = normalize_address("Романа Левицького вул., 4, Міське озеро")
    assert a == "лемківська" and "озеро" not in a
    assert street_overlap(a, b) == 0.0


def test_renamed_street_variants_still_match():
    """«вул. Незалежності, 148А» і «Незалежності вул., 148а, Майзлі»."""
    a, ha = normalize_address("вул. Незалежності, 148А")
    b, hb = normalize_address("Незалежності вул., 148а, Майзлі")
    assert street_overlap(a, b) == 1.0 and ha == hb


def test_cluster_never_holds_two_addresses():
    """Інваріант: в одному об'єкті — лише одна адреса.

    Регресія: запис, уже приєднаний за спільним URL, у фазі приєднання
    безадресних підтягував свій корінь до чужого якоря — і два будинки
    опинялись разом. Тепер кластер із конфліктом розділяється наприкінці.
    """
    url = "https://www.olx.ua/d/uk/obyavlenie/x-ID1.html"
    lun = _sh(1, "lun", area=48.1, floor=9, street="івасюка володимира", house="86",
              price=55000.0, url=url)
    olx = _sh(2, "olx", area=48.1, floor=9, price=55000.0, url=url)
    dom = _sh(3, "domria", area=48.0, floor=9, street="незалежності", house="233б",
              price=55000.0)

    groups = cluster([lun, olx, dom])
    shapes = {1: lun, 2: olx, 3: dom}
    for g in groups:
        assert not conflicts([shapes[i] for i in g])
    roots = {i: n for n, g in enumerate(groups) for i in g}
    assert roots[1] != roots[3], "різні вулиці лишились в одному об'єкті"
    assert roots[1] == roots[2], "спільне посилання має тримати записи разом"


def test_doubled_price_is_not_the_same_flat():
    """«вул. Миру, 100» за $89 000 і «вул. Миру» за $38 000 — різні квартири."""
    a = _sh(1, "domria", area=39.0, floor=9, street="миру", house="100", price=89000.0)
    b = _sh(2, "domria", area=39.0, floor=9, street="миру", price=38000.0)
    assert match_score(a, b) < MERGE_THRESHOLD


def test_same_street_different_houses_never_merge():
    """Регресія: аудит знайшов об'єкт із 23 оголошень на вул. Хіміків —
    будинки 2, 24, 28 і 92 склеїлись через збіг поверху, площі й ціни."""
    a = _sh(1, "domria", area=39.0, floor=7, street="хіміків", house="92", price=31200.0)
    b = _sh(2, "domria", area=39.0, floor=7, street="хіміків", house="2", price=31500.0)
    assert match_score(a, b) < MERGE_THRESHOLD

    # А той самий будинок із різною нумерацією джерел — усе ще одна квартира.
    c = _sh(3, "lun", area=39.0, floor=7, street="хіміків",
            house=frozenset({"2", "20"}), price=31500.0)
    assert match_score(a if False else b, c) >= MERGE_THRESHOLD


def test_house_numbers_are_linked_transitively():
    """«Княгинин, 44 корпус 13» доводить, що 44 і 13 — той самий будинок.

    Без транзитивного зв'язку одна квартира за $135 000, яку джерела
    описують то як «13», то як «44», виглядала б як конфлікт.
    """
    both = _sh(1, "domria", street="княгинин", house=frozenset({"13", "44"}))
    only13 = _sh(2, "lun", street="княгинин", house="13")
    only44 = _sh(3, "domria", street="княгинин", house="44")
    assert not conflicts([both, only13, only44])

    # А без сполучної ланки різні номери лишаються конфліктом.
    assert conflicts([only13, only44])
