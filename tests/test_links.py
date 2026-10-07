"""Розбір посилань і ключ «сайт:id» (крок E6, D51; realty/links.py, config/links.toml).

Що стереже:
  * усі 154 кейси прототипу Етапу 0 (tests/fixtures/links_cases.json — справжні
    id, синтетичні slug) і кожне сімейство адрес із копії бази
    (links_db_families.json: джерело|хост, 12 сегментів місця rieltor, префікси
    slug DOM.RIA) — ключ, канонічна адреса й адреса перевірки;
  * нормалізацію: m./www., http, порт, query й #фрагмент, кінцевий слеш,
    DOM.RIA /ru/ → /uk/, flombu публічна ↔ збережена, rieltor без www;
  * id OLX: base62 з переставленими v/w — 191 жива пара Етапу 0 (зі звичайним
    алфавітом частина не сходиться) і чутливість до регістру;
  * site_key — лише шість сімейств оголошень і лише адреси (не голі id);
  * сирий вставлений текст (піддомен агенції, токен чату) не потрапляє в журнал.
"""
from __future__ import annotations

import json
import logging
import string
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles, links  # noqa: E402
from realty.configfiles import ConfigError  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
CASES = json.loads((FIX / "links_cases.json").read_text(encoding="utf-8"))["cases"]
FAMILIES = json.loads((FIX / "links_db_families.json").read_text(encoding="utf-8"))["cases"]
OLX_LIVE = json.loads((FIX / "olx_numeric_live.json").read_text(encoding="utf-8"))["pairs"]


@pytest.fixture(autouse=True)
def _own_domain(monkeypatch):
    """Кейси прототипу містять наш постійний домен — він приходить із .env (D27)."""
    monkeypatch.setenv("PUBLIC_DOMAIN", "mojkvartiry.link")


def _check(case: dict, r) -> None:
    e = case["expect"]
    where = f"{case['origin']}: {case['note']}"
    if "key" in e:
        assert r.ok, (where, r)
        assert r.key == e["key"], where
    if "candidates_include" in e:
        assert r.ok, (where, r)
        assert set(e["candidates_include"]) <= set(r.candidates), (where, r.candidates)
    if "reason" in e:
        assert not r.ok and r.reason == e["reason"], (where, r)
    if "case_lost_id" in e:
        assert r.ok and r.case_lost and r.id == e["case_lost_id"] and r.key is None, (where, r)
    if "canonical_url" in e:
        assert r.canonical_url == e["canonical_url"], where
    if "fetch_url" in e:
        assert r.fetch_url == e["fetch_url"], where


def test_all_154_stage0_cases_are_ported():
    assert len(CASES) == 154
    kinds = {c["origin"].split(":")[1] for c in CASES}
    assert {"real", "synthetic", "own", "bare", "negative"} <= kinds


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['origin']}|{c['note'][:40]}")
def test_stage0_prototype_case(case):
    _check(case, links.parse(case["input"]))


@pytest.mark.parametrize("case", FAMILIES, ids=lambda c: c["origin"])
def test_every_url_family_of_the_db_copy(case):
    r = links.parse(case["input"])
    _check(case, r)
    # Збережена адреса дає той самий ключ, що й site_key (ним заповнюється база).
    assert links.site_key(case["input"]) == case["expect"]["key"]


def test_db_families_cover_all_sources_hosts_and_segments():
    origins = {c["origin"] for c in FAMILIES}
    assert {o.split("|")[0] + "|" + o.split("|")[1] for o in origins} == {
        "db:blago|blagodeveloper.com", "db:domria|dom.ria.com", "db:flombu|flombu.com",
        "db:lun|dom.ria.com", "db:lun|lun.ua", "db:lun|rieltor.ua", "db:lun|www.olx.ua",
        "db:olx|www.olx.ua"}
    assert sum(1 for o in origins if o.startswith("db:lun|rieltor.ua|")) == 12


# --- Нормалізація --------------------------------------------------------------------------

DOM = "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-test-ulitsa-34616500.html"
OLX = "https://www.olx.ua/d/uk/obyavlenie/kvartyra-test-ID10BkYC.html"
RIELTOR = "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/"


@pytest.mark.parametrize("variant,canonical", [
    # DOM.RIA: /ru/ → /uk/, m./www., http, порт, трекери, якір.
    (DOM.replace("/uk/", "/ru/"), DOM),
    (DOM.replace("https://dom.", "https://m.dom."), DOM),
    (DOM.replace("https://dom.", "http://www.dom."), DOM),
    (DOM.replace("dom.ria.com", "DOM.RIA.com:443"), DOM),
    (DOM + "?utm_source=share&fbclid=IwAR0#photo-3", DOM),
    (DOM.replace("/uk/", "/"), DOM),
    # OLX: m., без www, ru, посилання застосунку, трекери, якір.
    (OLX.replace("www.olx.ua", "m.olx.ua"), OLX),
    (OLX.replace("www.olx.ua", "olx.ua"), OLX),
    (OLX.replace("/d/uk/", "/d/"), OLX),
    (OLX.replace("https://www.olx.ua/d/uk/", "ios-app://663217552/app-olxua/https://www.olx.ua/uk/"), OLX),
    (OLX + "?search_reason=search%7Corganic&reason=observed_ad#a1;promoted", OLX),
    # rieltor: www → без www (www дає 404 живим), без кінцевого слеша, ru, трекери.
    (RIELTOR.replace("https://", "https://www."), RIELTOR),
    (RIELTOR.rstrip("/"), RIELTOR),
    (RIELTOR.replace("rieltor.ua/", "rieltor.ua/ru/"), RIELTOR),
    (RIELTOR + "?utm_source=lun.ua#gallery", RIELTOR),
    # flombu: публічна форма ↔ збережена — один ключ і одна канонічна адреса.
    ("https://www.flombu.com/uk/prodazh/kvartyra-116037", "https://flombu.com/uk/estate_deal_sales/116037"),
    ("https://www.flombu.com/ru/prodazha/kvartira-116037?utm_source=fb",
     "https://flombu.com/uk/estate_deal_sales/116037"),
    ("https://flombu.com/uk/estate_deal_sales/116037.json", "https://flombu.com/uk/estate_deal_sales/116037"),
    # LUN і Благо: мова, слеш, www, трекери.
    ("https://lun.ua/ru/realty/4720682454?utm_source=telegram", "https://lun.ua/uk/realty/4720682454"),
    ("https://www.blagodeveloper.com/plannings/41685", "https://blagodeveloper.com/plannings/41685/"),
])
def test_normalization_to_one_canonical_url(variant, canonical):
    assert links.canonical_url(variant) == canonical
    assert links.site_key(variant) == links.site_key(canonical) is not None


def test_canonical_fetch_urls_follow_the_live_findings():
    # DOM.RIA — картка API (сторінка без мовного префікса дає 404 живим).
    assert links.canonical_fetch_url(DOM) == "https://dom.ria.com/realty/data/34616500?lang_id=4"
    # rieltor — без www (www дає 404 навіть живим оголошенням).
    assert links.canonical_fetch_url(RIELTOR.replace("https://", "https://www.")) == RIELTOR
    # flombu — одразу на www (без www — 301).
    assert links.canonical_fetch_url("https://flombu.com/uk/estate_deal_sales/116037") == \
        "https://www.flombu.com/uk/estate_deal_sales/116037"
    # Благо не перевіряється (рішення власника 3, D46).
    assert links.canonical_fetch_url("https://blagodeveloper.com/plannings/41685/") is None
    assert links.canonical_fetch_url("34616500") is None                  # голий id — не адреса


@pytest.mark.parametrize("text,key", [
    # id лише в query — єдині параметри, що читаються (план Блоку 5).
    ("https://dom.ria.com/uk/verified/?realtyId=34616500&fromId=1", "domria:34616500"),
    ("https://blagodeveloper.com/plannings/?planning_id=41685", "blago:41685"),
    # Обгортки переадресацій і поширення, подвійне кодування.
    ("https://www.google.com/url?q=" + "https%253A%252F%252Flun.ua%252Fuk%252Frealty%252F4720682454",
     "lun:4720682454"),
    ("https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.olx.ua%2Fd%2Fuk%2Fobyavlenie%2Fx-ID10BkYC.html",
     "olx:10BkYC"),
    # Текст «Поділитися» з адресою без схеми і з розділовим знаком у кінці.
    ("Дивись: m.olx.ua/d/uk/obyavlenie/x-ID10BkYC.html.", "olx:10BkYC"),
    # %-закодований шлях (регістр id зберігається).
    ("https://www.olx.ua/d/uk/obyavlenie/%D0%BA%D0%B2-ID10LNlw.html", "olx:10LNlw"),
])
def test_more_link_forms(text, key):
    assert links.parse(text).key == key


@pytest.mark.parametrize("text,reason", [
    ("https://chat.ria.com/2/34616500?secure=SECRETTOKEN", "chat_link"),
    ("https://olx.page.link/AbCdEf", "short_link"),
    ("https://bit.ly/3xyz", "short_link"),
    ("https://www.flombu.com/uk/prodazh/budynok-116037", "not_flat"),
    ("https://rieltor.ua/ivano-frankovsk/houses-sale/view/12345678/", "not_flat"),
    ("https://lun.ua/uk/sale/if/flats", "not_listing"),
    ("https://example.com/realty-123-34616500.html", "unsupported_host"),
    ("https://mojkvartiry.link/?rooms=2", "own_not_property"),
    ("", "empty"),
    ("   ", "empty"),
    ("просто текст без посилання", "no_url"),
])
def test_refusals_have_a_reason(text, reason):
    r = links.parse(text)
    assert not r.ok and r.reason == reason
    assert links.site_key(text) is None


@pytest.mark.parametrize("text", [
    "https://[olx.ua/d/uk/obyavlenie/x-ID10BkYC.html",              # недописаний IPv6
    "https://olx.ua\uff03/d/uk/obyavlenie/x-ID10BkYC.html",         # «＃» з месенджера (NFKC)
    "https://www.google.com/url?q=https%3A%2F%2F%5Bolx.ua%2Fd%2Fuk%2Fobyavlenie%2Fx-ID10BkYC.html",
    "Дивись: https://[olx.ua/d/uk/obyavlenie/x-ID10BkYC.html",
])
def test_malformed_urls_are_refused_not_raised(text):
    """Рев'ю E6 (D51): urlsplit кидав ValueError — розбір обіцяє Link або NotALink."""
    r = links.parse(text)
    assert not r.ok and r.reason in ("unrecognized", "unsupported_host")
    assert links.site_key(text) is None
    assert links.canonical_url(text) is None and links.canonical_fetch_url(text) is None
    assert links.family_of(text) is None


@pytest.mark.parametrize("text,key,canonical", [
    ("olx:10BkYC", "olx:10BkYC", "https://www.olx.ua/d/uk/obyavlenie/ID10BkYC.html"),
    ("domria:34616500", "domria:34616500", "https://dom.ria.com/realty/data/34616500?lang_id=4"),
    ("ria:34616500", "domria:34616500", "https://dom.ria.com/realty/data/34616500?lang_id=4"),
    ("lun:4720682454", "lun:4720682454", "https://lun.ua/uk/realty/4720682454"),
    ("rieltor:13017427", "rieltor:13017427", "https://rieltor.ua/flats-sale/view/13017427/"),
    ("flombu:116037", "flombu:116037", "https://flombu.com/uk/estate_deal_sales/116037"),
    ("blago:41685", "blago:41685", "https://blagodeveloper.com/plannings/41685/"),
    (" olx:IDs3H ", "olx:IDs3H", "https://www.olx.ua/d/uk/obyavlenie/IDIDs3H.html"),
])
def test_explicit_family_key_input(text, key, canonical):
    """Рев'ю E6 (D51), план Блоку 5: «сімейство:id» — явна форма (і вигляд ?hl=<ключ>)."""
    r = links.parse(text)
    assert r.ok and r.key == key and r.via == "family_key" and r.candidates == (key,)
    assert r.canonical_url == canonical
    assert links.site_key(text) is None                       # у site_key — лише адреси


@pytest.mark.parametrize("text", ["OLX:10BkYC", "foo:123", "domria:abc", "olx:ab", "lun:",
                                  "olx:10BkYC-x", "ria:3461650012345"])
def test_unknown_or_malformed_family_key_is_unrecognized(text):
    r = links.parse(text)
    assert not r.ok and r.reason == "unrecognized"


def test_family_key_does_not_shadow_the_olx_footer_id():
    assert links.parse("id:931767753").key == "olx:113Bm9"


@pytest.mark.parametrize("text,key", [
    # Рев'ю E6 (D51): телефон пише з великої, адреса без схеми — знаходимо;
    # шлях (і регістр id OLX у ньому) — як був.
    ("Olx.ua/d/uk/obyavlenie/x-ID10BkYC.html", "olx:10BkYC"),
    ("Дивись Dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html",
     "domria:34616500"),
    ("HTTPS://WWW.OLX.UA/d/uk/obyavlenie/x-ID10LNlw.html", "olx:10LNlw"),
    ("Https://Lun.ua/uk/realty/4720682454", "lun:4720682454"),
])
def test_share_text_with_capitalised_host(text, key):
    assert links.parse(text).key == key
    assert links.site_key(text) == key


def test_own_domain_without_scheme():
    """PUBLIC_DOMAIN без схеми («mojkvartiry.link/property/18») — наш сайт (D51 п. 10)."""
    assert links.parse("mojkvartiry.link/property/18").key == "own:18"
    assert links.parse("Глянь www.mojkvartiry.link/property/18, ок").key == "own:18"
    assert links.parse("notmojkvartiry.link/property/18").reason == "no_url"


def test_path_parts_are_re_encoded_in_built_urls():
    """Рев'ю E6 (D51): частини шляху після %-декодування йшли в адресу сирими —
    «a%3Fb» давав «?» посеред шляху (хибне 404 перевірки)."""
    from urllib.parse import urlsplit

    r = links.parse("https://www.olx.ua/d/uk/obyavlenie/a%3Fb%23c%20d-ID10BkYC.html")
    assert r.key == "olx:10BkYC"
    for url in (r.fetch_url, r.canonical_url):
        p = urlsplit(url)
        assert p.path.endswith("-ID10BkYC.html") and not p.query and not p.fragment, url
    r = links.parse("https://www.olx.ua/d/uk/obyavlenie/%D0%BA%D0%B2-ID10LNlw.html")
    assert r.fetch_url == "https://www.olx.ua/d/uk/obyavlenie/%D0%BA%D0%B2-ID10LNlw.html"
    assert r.fetch_url.isascii()


def test_flombu_rental_section_is_not_a_sale_key():
    """Рев'ю E6 (D51): розділ flombu — лише продаж; оренда могла б мати свій простір id."""
    for url in ("https://www.flombu.com/uk/orenda/kvartyra-116037",
                "https://www.flombu.com/uk/anything/kvartyra-116037"):
        r = links.parse(url)
        assert not r.ok and r.reason == "not_flat" and links.site_key(url) is None
    assert links.site_key("https://www.flombu.com/uk/prodazh/kvartyra-116037") == "flombu:116037"


def test_agency_subdomain_is_dropped_everywhere():
    """Піддомен агенції rieltor буває номером телефону — у результаті його немає."""
    sub = "380000000000"
    r = links.parse(f"https://{sub}.rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/")
    assert r.key == "rieltor:13017427" and r.canonical_url == RIELTOR
    assert sub not in repr(r)


def test_raw_input_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    secrets = ["https://380000000000.rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/",
               "https://chat.ria.com/2/1?secure=SECRETTOKEN",
               OLX + "?utm_source=x&fbclid=y"]
    for s in secrets:
        links.parse(s)
        links.site_key(s)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for needle in ("380000000000", "SECRETTOKEN", "utm_source", "fbclid"):
        assert needle not in logged


# --- id OLX ---------------------------------------------------------------------------------


def test_olx_numeric_conversion_matches_191_live_pages():
    assert len(OLX_LIVE) == 191
    for token, numeric in OLX_LIVE.items():
        assert links.olx_token_to_numeric(token) == numeric, token
        assert links.numeric_to_olx_token(numeric) == token, numeric
    assert links.olx_token_to_numeric("113Bm9") == 931767753
    assert links.numeric_to_olx_token(931767753) == "113Bm9"


def test_standard_base62_would_be_wrong_for_v_and_w():
    """Звичайний алфавіт 0-9a-zA-Z помиляється саме на токенах із v/w (48 із 191)."""
    std = string.digits + string.ascii_lowercase + string.ascii_uppercase

    def dec(s):
        n = 0
        for ch in s:
            n = n * 62 + std.index(ch)
        return n

    wrong = [t for t, n in OLX_LIVE.items() if dec(t) != n]
    assert wrong and all(set(t) & set("vwVW") for t in wrong)
    assert len(wrong) == 48


def test_bare_olx_ids_and_numbers():
    r = links.parse("ID: 931767753")
    assert r.key == "olx:113Bm9" and r.family == "olx"
    r = links.parse("IDs3H")                                  # справжній id починається з «ID»
    assert set(r.candidates) == {"olx:s3H", "olx:IDs3H"}
    r = links.parse("925031606")                             # 9 цифр: ще й числовий id OLX
    assert "olx:10BkYC" in r.candidates and "lun:925031606" in r.candidates
    assert links.parse("№123").candidates == ("own:123",)
    assert links.parse("кв. 123").candidates == ("own:123",)
    for bare in ("925031606", "ID: 931767753", "IDs3H", "№123"):
        assert links.site_key(bare) is None                  # ключ — лише з адреси
    with pytest.raises(ValueError):
        links.olx_token_to_numeric("abc-")


def test_olx_key_is_case_sensitive():
    a = links.site_key("https://www.olx.ua/d/uk/obyavlenie/a-ID10LNlw.html")
    b = links.site_key("https://www.olx.ua/d/uk/obyavlenie/b-ID10LNLW.html")
    assert a == "olx:10LNlw" and b == "olx:10LNLW" and a != b
    low = links.parse("https://www.olx.ua/d/uk/obyavlenie/a-id10lnlw.html")
    assert low.case_lost and low.key is None
    assert links.site_key("https://www.olx.ua/d/uk/obyavlenie/a-id10lnlw.html") is None
    # id із самих цифр регістру не має — адреса в нижньому регістрі однозначна.
    assert links.site_key("https://www.olx.ua/d/uk/obyavlenie/a-id105037.html") == "olx:105037"


def test_site_key_families_are_exactly_the_six_sources():
    assert set(links.config().listing_families) == {"domria", "olx", "rieltor", "lun", "flombu",
                                                    "blago"}
    assert links.site_key("https://mojkvartiry.link/property/107") is None   # наш сайт — не оголошення
    assert links.parse("https://mojkvartiry.link/property/107").key == "own:107"


# --- Конфіг ---------------------------------------------------------------------------------


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    for src in (ROOT / "config").rglob("*.toml"):
        dst = d / src.relative_to(ROOT / "config")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(d))
    return d


def _edit(d: Path, old: str, new: str) -> None:
    p = d / "links.toml"
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    p.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize("old,new,needle", [
    ("id_regex.api_card = '^/(?:(?:uk|ru)/)?realty/data/(?P<id>\\d{5,10})/?$'",
     "id_regex.api_card = '^/(?:(?:uk|ru)/)?realty/data/(?P<id>\\d{5,10}/?$'",
     "не компілюється"),
    ("id_regex.realty = '^/(?:(?:uk|ru)/)?realty/(?P<id>\\d{6,12})/?$'",
     "id_regex.realty = '^/(?:(?:uk|ru)/)?realty/(\\d{6,12})/?$'", "немає групи (?P<id>"),
    ('alphabet = "0123456789abcdefghijklmnopqrstuwvxyzABCDEFGHIJKLMNOPQRSTUWVXYZ"',
     'alphabet = "0123456789abcdefghijklmnopqrstuvvxyzABCDEFGHIJKLMNOPQRSTUWVXYZ"',
     "62 різні"),
    ('"lun.ua" = "lun"', '"lun.ua" = "lunn"', "сімейства «lunn» немає"),
    ('canonical_url = ["https://lun.ua/uk/realty/{id}"]', 'canonical_url = ["https://lun.ua/uk/realty/"]',
     "без {id}"),
    ('family_alias = { ria = "domria" }', 'family_alias = { ria = "riaa" }', "сімейства «riaa» немає"),
    ("family_key = '^(?P<fam>[a-z]+):(?P<id>[0-9A-Za-z]{1,12})$'",
     "family_key = '^(?P<fam>[a-z]+):([0-9A-Za-z]{1,12})$'", "немає групи (?P<id>"),
    ("batch_rows = 200", "batch_rows = 500", "більше за допустимий максимум 200"),
])
def test_broken_links_config_is_refused(cfg_dir, old, new, needle):
    _edit(cfg_dir, old, new)
    with pytest.raises(ConfigError) as e:
        configfiles.load("links")
    assert needle in str(e.value)
