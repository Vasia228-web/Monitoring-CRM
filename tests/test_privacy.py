"""Телефони → «[телефон]» (рішення власника 5, D46; крок E6, D51; realty/privacy.py).

Номери в тестах — синтетичні (0XX 000 00 0N із чинним кодом оператора чи
району): форма справжня, абонентська частина — нулі. Справжніх номерів і
імен у тестах немає.

Принцип (config/privacy.toml): краще пропустити екзотичний номер, ніж зіпсувати
ціну, площу чи дату. Тому поруч із переліком форм, які МАЮТЬ замінюватись, —
більший перелік того, що лишається байт у байт.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles, privacy  # noqa: E402
from realty.configfiles import ConfigError  # noqa: E402

PH = "[телефон]"

# Номер → повністю замінюється (план Блоку 3 + форми, знайдені на копії бази).
REDACTED = [
    "+38 (067) 000-00-01", "+380670000001", "380 67 000 0001", "8 067 000 00 01",
    "067.000.00.01", "(067)0000001", "067 0000001", "0670000001", "0 67 000 00 01",
    "067 00 00 001", "+380 670000001", "0.6.7.0.0.0.0.0.0.1", "(0342) 75-00-01",
    "0342 750001", "06******01", "0XX XXX XX XX", "067-***-**-**", "0X******XX",
    "+380 67 *** ** **", "0 67 XXX XX XX", "06********",
    "viber://chat?number=%2B380670000001", "viber://add?number=380670000001",
    "wa.me/380670000001", "https://wa.me/380670000001",
    "https://api.whatsapp.com/send?phone=380670000001", "t.me/+380670000001",
    "tel:+380670000001", "tel:0670000001",
    "+380 (67) 000-00-01", "(+380) 67 000 00 01", "+38 0670000001", "80670000001",
    "+38(067)000-00-01", "067 – 000 – 00 – 01", "067 000 00 01",
    "050-000-00-02", "093 000 0003", "(0342)750001", "063.000.00.04",
    "06********01", "+38 06********01", "+38 0X********",
    "+380 99 000 00 05", "077 000 00 06", "075 000 00 07",
    # Рев'ю E6 (D51): роздільники з месенджерів і редакторів, «_» (2 рядки копії
    # бази), «+» усередині +380 (1 рядок), зайвий 0 після +380 (без «+380 »,
    # що висить перед заміною), довгі маски OLX (13–14 позицій, 44 рядки),
    # +380 з 3 масками (2 рядки), %2B, цифри інших письмен, «0. 6.7…».
    "0_6_7_0_0_0_0_0_0_1", "067_000_00_01", "067\u2009000\u200900\u200901",
    "067\u2007000\u200700\u200701", "067\u200b000\u200b00\u200b01", "067\u00ad000\u00ad00\u00ad01",
    "067\u2010000\u201000\u201001", "067\u2011000\u201100\u201101", "067\u2012000\u201200\u201201",
    "067\u2212000\u221200\u221201", "+38067-000+00-01", "+380 067 000 00 01",
    "+380 (0 67) 000 00 01", "+3800670000001", "0X*********XX", "0X**********XX",
    "+38 (0X**********XX", "+38 0X*********XX", "+3806700***01", "%2B380670000001",
    "viber://chat/?number=%2B380670000001", "\uff10\uff16\uff17 \uff10\uff10\uff10 \uff10\uff10 \uff10\uff11",
    "0. 6.7.0.0.0.0.0.0.1",
]


@pytest.mark.parametrize("number", REDACTED)
def test_every_realistic_form_is_redacted(number):
    assert privacy.redact_phones(number) == PH
    # І всередині тексту — з розділовими знаками й словами довкола.
    text = f"Двокімнатна, 45 000 $. Дзвоніть: {number}, власник."
    out = privacy.redact_phones(text)
    assert out == f"Двокімнатна, 45 000 $. Дзвоніть: {PH}, власник."


@pytest.mark.parametrize("text,expected", [
    # Номер одразу після ціни: ціна лишається (пошук усередині ряду цифр).
    ("45 000 $ 067 000 00 01", f"45 000 $ {PH}"),
    ("ціна 45 000 067 000 00 01", f"ціна 45 000 {PH}"),
    ("1 250 000 грн 0670000001", f"1 250 000 грн {PH}"),
    # Два номери в одному ряду й через кому.
    ("067 000 00 01 050 000 00 02", f"{PH} {PH}"),
    ("0670000001, 0500000002", f"{PH}, {PH}"),
    # Короткий міський — лише одразу після слова-ключа.
    ("тел. 75-00-01", f"тел. {PH}"),
    ("Тел.: 75 00 01", f"Тел.: {PH}"),
    ("моб. 50-12-34", f"моб. {PH}"),
    ("дзвоніть 75-00-01", f"дзвоніть {PH}"),
    # Дужки лишаються збалансованими.
    ("(067 000 00 01 власник)", f"({PH} власник)"),
    ("(06******** агент)", f"({PH} агент)"),
    # Кирилиця впритул (часта одруківка) — номер; латиниця впритул — ні (див. нижче).
    ("тел0670000001", f"тел{PH}"),
    ("Тел:0670000001", f"Тел:{PH}"),
    # Посилання-контакти — цілком.
    ("пишіть у viber://chat?number=%2B380670000001 або", f"пишіть у {PH} або"),
    # …але лише один номер: ціна чи кімнати одразу після посилання лишаються (рев'ю E6).
    ("tel:0670000001 45 000 $", f"{PH} 45 000 $"),
    ("tel: 0670000001 2 кімнати", f"{PH} 2 кімнати"),
    ("tel:+38 (067) 000-00-01, 65 000 $", f"{PH}, 65 000 $"),
    ("viber://chat?number=380670000001 2 кімн.", f"{PH} 2 кімн."),
    ("wa.me/380670000001 3 поверх", f"{PH} 3 поверх"),
    # +380 у ряду після ціни — за формою груп; «+380 0…» — разом із кодом країни.
    ("ціна 45 000 380 67 000 00 01", f"ціна 45 000 {PH}"),
    ("тел. +380 067 000 00 01.", f"тел. {PH}."),
])
def test_number_is_cut_and_the_rest_stays(text, expected):
    assert privacy.redact_phones(text) == expected


# Те, що МАЄ лишитись байт у байт: ціни, площі, роки, дати, час, id, кадастр,
# поверхи, діапазони, рахунки, картки, адреси з довгими числами.
UNCHANGED = [
    "65 000 $", "45 000 $", "1 250 000 грн", "(80) 3 600 000.00 грн", "2 050 000 грн 2025 року",
    "ціна 65000$ торг", "1 050 $/м²", "12 500 000 грн", "125 000 000 грн", "80 000 $",
    "45.3 м²", "65.5 м²", "120,4 кв.м", "50-60 м²", "S=45,3 м2", "3x4 м", "4х кімнатна",
    "2023", "2024", "1985-1990", "будинок 2021-2025", "XX століття",
    "12.10.2026", "05.10.2024", "05.10.2024 10:00", "01.10.2024 10:00",
    "12.10.2026 10:00-18:00", "з 10:00 до 18:00", "Дзвоніть до 12.10.24", "12-05-24",
    "2610100000:01:002:0123", "ЄДРПОУ 12345678", "UA213223130000026007233566001",
    "4149 4393 0000 0001", "5168 0670 0000 0001",
    "ID1143bm", "ID: 931767753", "34375047", "оголошення 34616500", "LUN 4720682454",
    "5/9 поверх", "9/10 поверх", "кв. 5, поверх 3/9", "1/2 частки", "вул. Мазепи 168Б, кв. 12",
    "код 0342", "0 800 500 500", "гаряча лінія 0800500500",
    "https://lun.ua/uk/realty/4720682454",
    "https://www.olx.ua/d/uk/obyavlenie/x-ID10BkYC.html",
    "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html",
    "https://t.me/agency_channel", "t.me/username",
    # Свідомі пропуски (див. докстрінг realty/privacy.py): 9 цифр без 0,
    # склеєне з латиницею, групи через «/», «О» замість нуля, форми поза білим
    # списком, маска довша за 14 позицій.
    "67 000 00 01", "0670000001abc", "067/000/00/01", "Viber0670000001", "О67 000 00 01",
    "067 0000 001", "06 70 00 00 01", "0 670 000 001", "код 06***********01 ok",
    # Рев'ю E6 (D51): дата поруч із маскою; картка й IBAN з «380» усередині;
    # «Т.» — ініціал; десяткові дроби; кадастровий номер, що починається з 05;
    # «2+1»; «+» в інших рядах; дата після часу.
    "Огляд до 05.10.2024 ****", "Здача 05.2025 ****", "05-06-2024 хххх",
    "4149 3805 1234 5678", "UA21 3223 1300 3805 1234 5678 9", "вул. Т. 12-05-24",
    "0.5 0.6 0.7 0.8 0.9", "0510100000:01:002:0123", "2+1 кімнати", "45 000+1 500 $",
    "кв. 0+1, 067+000", "з 10:00 до 18:00 05.10.2024",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_no_false_positives(text):
    assert privacy.redact_phones(text) == text


def test_masked_number_is_cut_whole_not_leaving_a_tail():
    """Маска довша за 10 позицій («06********01») — заміна цілим шматком, без
    хвоста «[телефон]01»; цифри, склеєні з літерами кирилиці, — теж номер."""
    for text in ("огляд: 06********01 (агент)", "+38 06********01 або +38 0X********"):
        out = privacy.redact_phones(text)
        assert not re.search(r"\[телефон\][\d*xхXХ•]", out), out
        assert not re.search(r"\d", out.replace("(агент)", "")), out
    assert privacy.redact_phones("0670000001Харків") == f"{PH}Харків"
    # OLX маскує й 13–14 позиціями — цілим шматком (рев'ю E6: 44 рядки копії бази).
    assert privacy.redact_phones("код 06**********01 ok") == f"код {PH} ok"
    assert privacy.redact_phones("+38 (0X**********XX, торг") == f"{PH}, торг"
    # Довша за max_mask_national_positions маска лишається ЦІЛОЮ, а не
    # розрізаною на «[телефон]» і хвіст.
    assert privacy.redact_phones("код 06***********01 ok") == "код 06***********01 ok"
    # +380 рівно з 12 позицій — досить 3 масок; коротша маска інших форм — ні.
    assert privacy.redact_phones("тел +3806700***01") == f"тел {PH}"
    assert privacy.redact_phones("тел 06700***01") == "тел 06700***01"


def test_realistic_description_keeps_every_figure():
    text = ("Продаж 2-кімнатної квартири 65,3 м² у ЖК «Тест», 5/9 поверх, будинок 2021 року. "
            "Ціна 65 000 $ (2 600 000 грн), торг. Кадастровий 2610100000:01:002:0123. "
            "Огляд 12.10.2026 з 10:00 до 18:00. Тел. 067 000 00 01, viber +380500000002.")
    out = privacy.redact_phones(text)
    assert out.count(PH) == 2
    assert out == text.replace("067 000 00 01", PH).replace("+380500000002", PH)


@pytest.mark.parametrize("text", REDACTED + UNCHANGED + [
    "тел. 75-00-01 і 067 000 00 01", "(067 000 00 01 власник)"])
def test_idempotent(text):
    once = privacy.redact_phones(text)
    assert privacy.redact_phones(once) == once
    assert not re.search(r"\d", configfiles.load("privacy").phone.replacement)


def test_patterns_never_carry_the_subscriber_digits():
    """Форма для перегляду замін — лише префікс 380/80/0, решта цифр — X."""
    for number in REDACTED + ["тел. 75-00-01"]:
        _, hits = privacy.find(f"x {number} y")
        assert hits, number
        for h in hits:
            digits = re.sub(r"\D", "", h.pattern)
            assert digits in ("", "0", "80", "380"), (number, h.pattern)
    assert privacy.pattern_of("+38 (067) 000-00-01") == "+38 (0XX) XXX-XX-XX"
    assert privacy.pattern_of("+380670000001") == "+380XXXXXXXXX"


def test_near_misses_are_reported_as_shapes_only():
    """Відкинуті «майже номери» — лише довжини груп і причина, без самих цифр."""
    misses: list = []
    text = "Огляд 01.10.2024 10 год, ціна (80) 3 600 000.00 грн, 067 0000 001"
    assert privacy.find(text, misses=misses)[0] == text
    assert {(m.shape, m.reason) for m in misses} == {
        ("2-2-4-2", "code"), ("1-1-3-3-2", "shape"), ("3-4-3", "shape")}
    # Дата з часом — не ряд із 10 цифр («10:00» ряд не продовжує): не «майже номер».
    misses = []
    privacy.find("Огляд 01.10.2024 10:00", misses=misses)
    assert misses == []


def test_numbers_split_by_other_characters_are_reported_not_redacted():
    """Рев'ю E6 (D51): форми, які суворий розбір не бачить рядом («/», «⏎», «;»,
    «+» поза +380), — у звіті «майже номерів» з формою без цифр (reason=loose),
    щоб нові форми знаходились; самі тексти не змінюються."""
    for text, pattern in [("тел. 067/000/00/01", "0XX/XXX/XX/XX"),
                          ("тел.\n067\n000 00 01", "0XX\nXXX XX XX"),
                          ("тел 067; 000; 00; 01", "0XX; XXX; XX; XX"),
                          ("моб 067+000+00+01", "0XX+XXX+XX+XX")]:
        misses: list = []
        assert privacy.find(text, misses=misses)[0] == text
        assert [(m.shape, m.reason) for m in misses] == [(pattern, "loose")], text
    # Те, що суворий розбір бачить цілим рядом, тут не дублюється; замінене — теж.
    for text in ("Огляд 05.10.2024 10 год", "тел 067 000 00 01", "45 000 $ / 1 250 000 грн"):
        misses = []
        privacy.find(text, misses=misses)
        assert not [m for m in misses if m.reason == "loose"], text


def test_plus_inside_a_number_only_for_380():
    """«+» між групами — номер лише всередині +380; вимкнено в конфігу — лише звіт."""
    import dataclasses

    assert privacy.redact_phones("тел +38067-000+00-01 ok") == f"тел {PH} ok"
    assert privacy.redact_phones("тел 067-000+00-01 ok") == "тел 067-000+00-01 ok"
    cfg = privacy.config()
    off = dataclasses.replace(cfg, phone=dataclasses.replace(cfg.phone,
                                                            plus_inside_international=False))
    misses: list = []
    assert privacy.find("тел +38067-000+00-01 ok", cfg=off, misses=misses)[0] == \
        "тел +38067-000+00-01 ok"
    assert [m.reason for m in misses] == ["loose"]


def test_long_digit_runs_are_fast():
    """Ряди цифр без номера (IBAN, картки, таблиці цін) — без експоненційного перебору."""
    import time

    text = ("1234 5678 " * 400) + "abc" + ("9" * 3000) + "x" + ("0/1 " * 2000) + ("1_" * 3000)
    t0 = time.perf_counter()
    assert privacy.redact_phones(text) == text
    misses: list = []
    assert privacy.find(text, misses=misses)[0] == text          # і зі звітом промахів
    assert time.perf_counter() - t0 < 2.0


def test_disabled_means_no_replacement(tmp_path, monkeypatch):
    d = tmp_path / "config"
    for src in (ROOT / "config").rglob("*.toml"):
        (d / src.relative_to(ROOT / "config")).parent.mkdir(parents=True, exist_ok=True)
        (d / src.relative_to(ROOT / "config")).write_text(src.read_text(encoding="utf-8"),
                                                          encoding="utf-8")
    p = d / "privacy.toml"
    p.write_text(p.read_text(encoding="utf-8").replace("enabled = true", "enabled = false"),
                 encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(d))
    cfg = configfiles.load("privacy")
    assert privacy.redact_phones("067 000 00 01", cfg=cfg) == "067 000 00 01"
    assert privacy.find("067 000 00 01", cfg=cfg)[0] == PH          # scan бачить і так


@pytest.mark.parametrize("old,new,needle", [
    ('replacement = "[телефон]"', 'replacement = "[тел 1]"', "phone.replacement: у заміні не може бути цифр"),
    ('"3-3-2-2", "3-3-4"', '"3-3-2-2", "3-3-5"', "разом має бути 10"),
    ('mobile_codes = ["39"', 'mobile_codes = ["039"', "дві цифри"),
    ("link_regex.tel = '(?i)(?<![\\w])tel:", "link_regex.tel = '(?i)(?<![\\w]tel:",
     "не компілюється"),
    ("batch_rows = 200", "batch_rows = 500", "більше за допустимий максимум 200"),
    ('_()"\nmax_sep_run', '_()+"\nmax_sep_run', "«+» — лише через plus_inside_international"),
    ("min_mask_chars_full_international = 3", "min_mask_chars_full_international = 5",
     "не більше за min_mask_chars"),
    ("max_mask_national_positions = 14", "max_mask_national_positions = 20",
     "більше за допустимий максимум 16"),
    ('fields = ["description", "title"]', 'fields = ["description", "location"]',
     "не з переліку"),
])
def test_broken_privacy_config_is_refused(tmp_path, monkeypatch, old, new, needle):
    d = tmp_path / "config"
    d.mkdir()
    text = (ROOT / "config" / "privacy.toml").read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    (d / "privacy.toml").write_text(text.replace(old, new), encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(d))
    with pytest.raises(ConfigError) as e:
        configfiles.load("privacy")
    assert needle in str(e.value)
