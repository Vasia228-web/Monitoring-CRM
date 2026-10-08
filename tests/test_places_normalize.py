"""Нормалізація назв районів і ЖК (Блок 4, E10, D57): точний ключ, без вгадування."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles  # noqa: E402
from realty.places.normalize import normalizer, suggest  # noqa: E402


def _n():
    return normalizer(configfiles.load("places/rules"))


def test_latin_lookalike_letters_fold_to_cyrillic():
    """Етап 0: «Пасiчна» (латинська i) — 463 оголошення LUN, окремий кошик у сегментах."""
    n = _n()
    assert n.key("Пасiчна") == n.key("Пасічна") == n.key("мкр. «Пасічна»") == "пасічна"
    assert n.key("Опришівцi") == n.key("Опришівці")
    assert n.key("Рiнь") == n.key("Рінь")
    # Чисто латинська назва лишається латиницею; змішане слово — до письма більшості.
    assert n.key("Manhattan") == "manhattan"
    assert n.key("Comfогt House", "complex") == n.key("Comfort House", "complex")
    assert n.key("Сity", "complex") == n.key("City", "complex")       # кирилична «С»
    assert n.key("KNIAHYNYN-СENTER", "complex") == n.key("Kniahynyn-Center", "complex")


def test_prefixes_suffixes_quotes_and_dashes():
    n = _n()
    assert n.key("Крихівці (Івано-Франківськ)") == n.key("Крихівці") == "крихівці"
    assert n.key("Коновальця-Чорновола") == n.key("Коновальця Чорновола")
    assert n.key("ЖК «Паркова Алея»", "complex") == n.key("Паркова Алея", "complex")
    assert n.key("ж/к Senat", "complex") == n.key("Senat", "complex")
    assert n.key("ЖК Квартал №5", "complex") == n.key("Квартал 5", "complex")
    assert n.key("Дем'янів Лаз", "complex") == n.key("Демʼянів Лаз", "complex")
    # «Житловий район» — не префікс ЖК: парасолька окремо від ЖК Manhattan (D57).
    assert n.key("Житловий район Manhattan", "complex") != n.key("ЖК Manhattan", "complex")
    assert n.key("ЖК", "complex") == ""


def test_no_fuzzy_matching_only_suggestions():
    """Senat і Sonata — різні ЖК (колізія скелета на Етапі 0): ключі різні, підказка —
    лише підказка."""
    n = _n()
    assert n.key("Senat", "complex") != n.key("Sonata", "complex")
    assert suggest("сенатт", {"сенат": "Сенат"}) == "Сенат"
    assert suggest("щось зовсім інше", {"сенат": "Сенат"}) is None
