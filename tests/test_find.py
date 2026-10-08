"""Пошук за посиланням і «Перевірити зараз» (Блок 5, крок E14, D59).

Вимога власника (промт 11, Блок 5): вставляю посилання на оголошення з будь-якого з 5
джерел — відкривається сторінка цієї квартири з виділеним оголошенням; посилання
нормалізується (мобільні версії, параметри відстеження, якорі, слеш у кінці); приймає
наше посилання на квартиру й id оголошення; не знайдено — зрозуміла причина (не з
Івано-Франківська, ще не зібране, у карантині, знято); «Перевірити зараз» — один запит
до джерела, далі звичайний конвеєр якості, з обмеженням частоти в конфігу.

Усе — на синтетичній базі (slug вигадані, імен людей немає), мережі немає: процес
перевірки отримує підроблений фетчер, а сайт сам у мережу не ходить узагалі.
На коді до E14 ці тести падають: маршрутів /find і /api/find/check немає (404/405),
сторінка квартири не знає ?hl=, поля пошуку в шапці немає, черга не знає виду `link`.
"""
from __future__ import annotations

import logging
import re
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles, ops, runner  # noqa: E402
from realty.analytics import cache, objects, segments  # noqa: E402
from realty.fetcher import ProbeResult  # noqa: E402
from realty.models import (  # noqa: E402
    Base, Condition, Listing, MarketType, PriceEvent, Property, PropertyRedirect,
)
from realty.web import sessions  # noqa: E402
from realty.web.app import app  # noqa: E402

NOW = datetime(2026, 10, 8, 8, 0, 0)
OWNER, OWNER_PW = "vasia", "пароль власника 1"
FRIEND, FRIEND_PW = "druh", "druh-pass-2"
SITE = "https://mojkvartiry.test"
RIA = "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-tsentr-testova-{}.html"
OLX = "https://www.olx.ua/d/uk/obyavlenie/kvartyra-testova-ID{}.html"


def _l(i, pid, **kw):
    base = dict(source="domria", external_id=str(30000000 + i), original_url=RIA.format(30000000 + i),
                price=50_000 + i, currency="USD", price_usd=50_000.0 + 100 * i, rooms=2,
                area_total=50.0, price_per_sqm=1000.0 + i, floor=5, floors_total=9,
                location="вул. Тестова, 1", market_type=MarketType.SECONDARY,
                condition=Condition.RENOVATED, published_at=NOW - timedelta(days=40),
                first_seen=NOW - timedelta(days=30), last_seen=NOW, quality_status="ok",
                property_id=pid, is_active=True)
    base.update(kw)
    return Listing(id=i, **base)


ROWS = [
    # квартира 1: DIM.RIA і копія OLX, яку ми бачили лише через LUN
    lambda: _l(1, 1, external_id="34616500", original_url=RIA.format(34616500)),
    lambda: _l(2, 1, source="lun", external_id="4720682454", original_url=OLX.format("abc12")),
    # квартира 2: оголошення OLX знято 03.10
    lambda: _l(3, 2, source="olx", external_id="10BkYC", original_url=OLX.format("10BkYC"),
               is_active=False, delisted_at=datetime(2026, 10, 3, 9, 0)),
    # квартира 3: у карантині якості
    lambda: _l(4, 3, quality_status="review", quality_reason="ціна за м² поза межами сегмента"),
    # квартири 4 і 5: один ключ DIM.RIA у двох квартирах (пропущене зведення)
    lambda: _l(5, 4),
    lambda: _l(6, 5, source="lun", external_id="5000000001", original_url=RIA.format(30000005)),
    # квартира 6: Благо (актуальність не підтверджується)
    lambda: _l(7, 6, source="blago", external_id="41685",
               original_url="https://blagodeveloper.com/plannings/41685/"),
    # квартира 7: rieltor через LUN
    lambda: _l(8, 7, source="lun", external_id="5000000002",
               original_url="https://rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/"),
    # квартира 8: flombu
    lambda: _l(9, 8, source="flombu", external_id="116037",
               original_url="https://flombu.com/uk/estate_deal_sales/116037"),
    # квартири 9 і 10: id OLX, однакові без урахування регістру
    lambda: _l(10, 9, source="olx", external_id="10LNlw", original_url=OLX.format("10LNlw")),
    lambda: _l(11, 10, source="olx", external_id="10LNLW", original_url=OLX.format("10LNLW")),
    # ще не зведене в квартиру (щойно зібране, крок «дублі» попереду)
    lambda: _l(12, None),
    # квартира 11: позначене неактуальним вручну
    lambda: _l(13, 11, manual_active=False),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Синтетична база, своя ops.db, два входи (власник і друг), процес перевірки без мережі."""
    engine = create_engine(f"sqlite:///{tmp_path / 'find.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        for pid in [*range(1, 12), 41685]:
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=50.0,
                           price_usd_min=50_000.0, price_per_sqm=1000.0, street="Тестова",
                           house="1", first_seen=NOW - timedelta(days=30), last_seen=NOW))
        s.flush()
        s.add_all([make() for make in ROWS])
        s.add(PropertyRedirect(old_id=100, new_id=2))
        s.commit()

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    import realty.db as dbmod
    import realty.web.analytics_routes as routes
    import realty.web.app as appmod
    import realty.web.dedup_routes as dr
    import realty.web.find_routes as fr
    import realty.web.status as status_mod
    for mod in (routes, appmod, status_mod, fr, dbmod):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(dr, "session_scope", scope)
    from realty import backup, dedup_audit, dedup_sample  # noqa: F401 — таблиці ops
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(ops_engine)
    ops.init_ops(force=True)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    monkeypatch.setenv("FRIEND_USER", FRIEND)
    monkeypatch.setenv("FRIEND_PASSWORD", FRIEND_PW)
    monkeypatch.setenv("PUBLIC_DOMAIN", "mojkvartiry.link")
    for mod in (segments, objects):
        monkeypatch.setattr(mod, "_now", lambda: NOW)
    cache.invalidate()
    from realty.lookup import link, opened
    monkeypatch.setattr(opened, "CYCLE_LOCK", tmp_path / "cycle.lock")
    monkeypatch.setattr(opened, "DRAIN_LOCK", tmp_path / "lookup.lock")
    monkeypatch.setattr(opened, "DISABLED_FLAG", tmp_path / "COLLECTOR_OFF")
    net = FakeNet()
    monkeypatch.setattr(link, "_make_fetcher", lambda: net)
    owner = _login(OWNER, OWNER_PW, "203.0.113.71")
    friend = _login(FRIEND, FRIEND_PW, "203.0.113.72")
    yield SimpleNamespace(owner=owner, friend=friend, Session=Session, engine=engine, net=net,
                          opened=opened, link=link, tmp=tmp_path)
    cache.invalidate()


def _login(user, pw, ip) -> TestClient:
    c = TestClient(app, base_url=SITE, client=("127.0.0.1", 50000), follow_redirects=False)
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"},
               headers={"CF-Connecting-IP": ip, "Accept": "text/html"})
    assert r.status_code == 303, r.text[:200]
    return c


class FakeNet:
    """Замість мережі: відповідь за частиною адреси (типово 200 без тіла). Рахує запити."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.answers: dict[str, tuple[int, str | None]] = {}

    def check(self, url, method="HEAD", delay=None, max_bytes=0):
        self.calls.append((method, url))
        for part, (code, body) in self.answers.items():
            if part in url:
                return ProbeResult(code=code, method=method, url=url, final_url=url,
                                   body=body if method == "GET" else None)
        return ProbeResult(code=200, method=method, url=url, final_url=url)

    def close(self):
        pass


def card(rid: int, **kw) -> str:
    """Картка DOM.RIA (realty/data/<id>?lang_id=4) — синтетична, у форматі збирача."""
    import json

    d = {"realty_id": rid, "city_id": 15, "city_name_uk": "Івано-Франківськ",
         "realty_type_id": 2, "advert_type_id": 1,
         "beautiful_url": f"realty-prodaja-kvartira-ivano-frankovsk-tsentr-testova-{rid}.html",
         "price_total": 61000, "currency_type_id": 1, "rooms_count": 2,
         "total_square_meters": 54.5, "street_name_uk": "Тестова", "building_number_str": "10",
         "district_name_uk": "Центр", "floor": 4, "floors_count": 9,
         "publishing_date": "2026-10-01 10:00:00", "priceItemArr": {"1": 1119},
         "description_uk": "Продаж квартири, ремонт. Дзвоніть +380 67 000 00 01"}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def _find(c, text, **headers):
    return c.post("/find", data={"q": text}, headers={"Origin": SITE, "Accept": "text/html",
                                                      **headers})


def _check(c, q):
    return c.post("/api/find/check", json={"q": q}, headers={"Origin": SITE})


def _drain(job_id):
    from realty.lookup import opened

    cfg = configfiles.load("speed").open_check
    return opened.run(job_id, budget_s=cfg.drain_budget_s, timeout_s=cfg.job_timeout_s)


def _lookup_cfg(monkeypatch, **check):
    """config/lookup.toml з іншими значеннями [check] — ліміти не зашиті в код."""
    real = configfiles.get

    def fake(name):
        value = real(name)
        if name != "lookup":
            return value
        new = {k: MappingProxyType(v) if isinstance(v, dict) else v for k, v in check.items()}
        return replace(value, check=replace(value.check, **new))
    monkeypatch.setattr(configfiles, "get", fake)


# --- Розбір: усі 5 джерел, мобільні версії, параметри, якорі, слеш, наші посилання ----------

PARSE_CASES = [
    # DIM.RIA: мобільна, www, /ru/, без схеми, utm, якір, текст «Поділитися», обгортка google,
    # картка API, службова сторінка з id у query
    ("https://m.dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html", "domria:34616500"),
    ("https://www.dom.ria.com/ru/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html?utm_source=fb&fbclid=AbC#photo-3", "domria:34616500"),
    ("dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html/", "domria:34616500"),
    ("Глянь квартиру https://dom.ria.com/uk/realty-prodaja-kvartira-x-34616500.html, гарна", "domria:34616500"),
    ("https://www.google.com/url?q=https%3A%2F%2Fdom.ria.com%2Fuk%2Frealty-x-34616500.html&sa=D", "domria:34616500"),
    ("https://dom.ria.com/realty/data/34616500?lang_id=4", "domria:34616500"),
    ("https://dom.ria.com/uk/verified/?realtyId=34616500", "domria:34616500"),
    # OLX: мобільна, без www, ru, /uk/obyavlenie, reason+utm, якір, застосунки
    ("https://m.olx.ua/d/uk/obyavlenie/kvartyra-ID10BkYC.html", "olx:10BkYC"),
    ("olx.ua/d/obyavlenie/kvartyra-ID10BkYC.html?reason=hp%7Cpromoted&utm_medium=x", "olx:10BkYC"),
    ("https://www.olx.ua/uk/obyavlenie/insha-nazva-ID10BkYC.html#3;promoted", "olx:10BkYC"),
    ("ios-app://1234567890/olx/https://www.olx.ua/d/uk/obyavlenie/kvartyra-ID10BkYC.html", "olx:10BkYC"),
    ("android-app://ua.slando/https/www.olx.ua/d/uk/obyavlenie/kvartyra-ID10BkYC.html", "olx:10BkYC"),
    # LUN: власний id, ru, utm
    ("https://lun.ua/ru/realty/4720682454?utm_source=telegram", "lun:4720682454"),
    ("https://m.lun.ua/uk/realty/4720682454/", "lun:4720682454"),
    # rieltor: www (сайт дає 404 — ключ той самий), піддомен агенції, без слеша, utm і якір
    ("https://www.rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/", "rieltor:13017427"),
    ("https://agentsiya-test.rieltor.ua/ru/ivano-frankovsk/flats-sale/view/13017427?utm_source=x#gallery", "rieltor:13017427"),
    # flombu: публічна й збережена форми, ru, utm, .json
    ("https://www.flombu.com/uk/prodazh/kvartyra-116037?utm_source=fb", "flombu:116037"),
    ("https://flombu.com/ru/prodazha/kvartira-116037/", "flombu:116037"),
    ("https://flombu.com/uk/estate_deal_sales/116037.json", "flombu:116037"),
    # Благо: www, без слеша, fbclid, форма з query, обгортка facebook
    ("https://www.blagodeveloper.com/plannings/41685?fbclid=IwAR0", "blago:41685"),
    ("https://blagodeveloper.com/plannings/?planning_id=41685", "blago:41685"),
    ("https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fblagodeveloper.com%2Fplannings%2F41685%2F", "blago:41685"),
    # наш сайт: домен (PUBLIC_DOMAIN), відносне посилання, з якорем і параметрами, «№»
    ("https://mojkvartiry.link/property/123", "own:123"),
    ("https://www.mojkvartiry.link/property/123/?verify=0#prices", "own:123"),
    ("/property/123", "own:123"),
    ("/property/123?hl=olx:10BkYC#found", "own:123"),
    ("№123", "own:123"),
    # явний ключ і id оголошення
    ("olx:10BkYC", "olx:10BkYC"),
    ("ID: 931767753", "olx:113Bm9"),
]


@pytest.mark.parametrize("text,key", PARSE_CASES, ids=[k + ":" + str(i) for i, (_, k)
                                                       in enumerate(PARSE_CASES)])
def test_parse_input_all_sources(env, text, key):
    from realty.web.find_routes import parse_input

    got = parse_input(text, None)
    assert getattr(got, "key", None) == key, got


def test_own_site_host_is_ours_even_without_public_domain(env, monkeypatch):
    """Адреса під тим самим хостом, під яким відкрито сайт (тунель), — наш сайт."""
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    r = _find(env.owner, "https://mojkvartiry.test/property/2")
    assert r.status_code == 303 and r.headers["location"] == "/property/2"


# --- Пошук: одна квартира, кілька, переадресація, причини -------------------------------------


@pytest.mark.parametrize("text,pid,key", [
    ("https://m.dom.ria.com/ru/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html?utm_source=x#a", 1, "domria:34616500"),
    ("https://m.olx.ua/d/obyavlenie/insha-nazva-IDabc12.html?reason=x", 1, "olx:abc12"),  # лише через LUN
    ("34616500", 1, "domria:34616500"),                      # id оголошення
    ("https://lun.ua/uk/realty/4720682454", 1, "lun:4720682454"),   # власний id LUN
    ("https://olx.ua/d/uk/obyavlenie/x-ID10BkYC.html", 2, "olx:10BkYC"),   # знято
    ("https://www.rieltor.ua/ivano-frankovsk/flats-sale/view/13017427", 7, "rieltor:13017427"),
    ("https://www.flombu.com/uk/prodazh/kvartyra-116037?utm_source=fb", 8, "flombu:116037"),
    ("https://blagodeveloper.com/plannings/?planning_id=41685", 6, "blago:41685"),
])
def test_post_find_redirects_to_the_property_with_highlight(env, text, pid, key):
    r = _find(env.owner, text)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert re.fullmatch(rf"/property/{pid}\?hl=[a-z]+:[0-9A-Za-z]+#found", loc), loc
    assert unquote(loc.split("hl=")[1].split("#")[0]) == key


def test_own_links_and_merged_property_redirect(env):
    assert _find(env.owner, "https://mojkvartiry.link/property/5").headers["location"] == "/property/5"
    assert _find(env.owner, "/property/5").headers["location"] == "/property/5"
    # Злита квартира 100 → 2 (property_redirects): посилання не ламається.
    assert _find(env.owner, "№100").headers["location"] == "/property/2"
    loc = _find(env.owner, "https://mojkvartiry.link/property/100?verify=0").headers["location"]
    assert loc == "/property/2"


def test_key_in_two_properties_shows_a_choice(env):
    r = _find(env.owner, RIA.format(30000005) + "?utm_source=x")
    assert r.status_code == 303 and r.headers["location"].startswith("/find?q=")
    page = env.owner.get(r.headers["location"])
    assert page.status_code == 200
    assert 'href="/property/4?hl=domria%3A30000005#found"' in page.text
    assert 'href="/property/5?hl=domria%3A30000005#found"' in page.text
    assert "веде в кілька квартир" in page.text
    assert "Це одна квартира" in page.text                       # підказка лише власнику
    assert "Це одна квартира" not in env.friend.get(r.headers["location"]).text


def test_bare_number_in_two_id_spaces_shows_a_choice(env):
    """41685 — і номер квартири на нашому сайті, і id планування Благо: не вгадуємо."""
    page = env.owner.get(_find(env.owner, "41685").headers["location"])
    assert 'href="/property/41685"' in page.text
    assert 'href="/property/6?hl=blago%3A41685#found"' in page.text
    # «№41685» — лише номер квартири.
    assert _find(env.owner, "№41685").headers["location"] == "/property/41685"


def test_lower_cased_olx_link_offers_both_case_collisions(env):
    page = env.owner.get(_find(env.owner, "https://www.olx.ua/d/uk/obyavlenie/x-id10lnlw.html")
                         .headers["location"])
    assert 'href="/property/9?hl=olx%3A10LNlw#found"' in page.text
    assert 'href="/property/10?hl=olx%3A10LNLW#found"' in page.text
    # Точне посилання — рівно одна квартира.
    assert _find(env.owner, OLX.format("10LNLW")).headers["location"].startswith("/property/10?")


@pytest.mark.parametrize("text,needle", [
    ("", "Вставте посилання"),
    ("просто текст без посилання", "Це не схоже на посилання"),
    ("https://example.com/kvartyra/1", "Цей сайт ми не збираємо"),
    ("https://olx.page.link/AbCdE", "коротке посилання"),
    ("https://chat.ria.com/2/1?secure=SECRETTOKEN", "посилання на чат"),
    ("https://www.olx.ua/d/uk/nedvizhimost/kvartiry/prodazha-kvartir/", "не сторінка оголошення"),
    ("https://rieltor.ua/ivano-frankovsk/houses-sale/view/12345678/", "не про продаж квартири"),
    ("https://mojkvartiry.link/analytics?rooms=2", "не на сторінку квартири"),
    ("https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-39999999.html", "ще немає"),
    ("№999999", "Квартири з таким номером"),
    ("123456789012", "За цим числом нічого"),
    (RIA.format(30000012), "ще не зведене в квартиру"),
])
def test_not_found_has_a_clear_reason(env, text, needle):
    r = _find(env.owner, text)
    assert r.status_code == 303 and r.headers["location"].startswith("/find?")
    page = env.owner.get(r.headers["location"])
    assert page.status_code == 200 and needle in page.text, page.text[-3000:]


def test_not_in_db_offers_check_now_only_where_enabled(env):
    def page(text):
        return env.owner.get(_find(env.owner, text).headers["location"]).text

    assert "Перевірити зараз" in page(RIA.format(39999999))
    assert "Перевірити зараз" in page("https://m.olx.ua/d/uk/obyavlenie/nova-ID9zZzZ.html?utm_x=1")
    flombu = page("https://www.flombu.com/uk/prodazh/kvartyra-999999")
    assert "Перевірити зараз" not in flombu and "поки немає" in flombu
    lun_only = page("https://lun.ua/uk/realty/9999999999")
    assert "Перевірити зараз" not in lun_only
    blago = page("https://blagodeveloper.com/plannings/99999/")
    assert "Перевірити зараз" not in blago and "Благо не перевіряється" in blago
    # Голий токен OLX без назви — адреси для запиту немає: просимо повне посилання.
    assert "повне посилання" in page("olx:9zZzZ")


# --- Сторінка квартири: плашка й підсвічування, XSS -------------------------------------------


def test_property_page_highlights_every_row_of_the_key(env):
    loc = _find(env.owner, "https://m.olx.ua/d/uk/obyavlenie/x-IDabc12.html").headers["location"]
    html = env.owner.get(loc.split("#")[0] + "&verify=0").text
    assert 'id="found"' in html and "Знайдено за посиланням" in html
    assert re.search(r'<li class="hl">\s*<a href="https://www\.olx\.ua/[^"]*IDabc12\.html"', html)
    assert html.count('<li class="hl">') == 1                  # DIM.RIA тієї ж квартири — ні
    assert "ЛУН — активне" in html
    owner_tbl = re.findall(r'<tr class="hl">', html)
    assert len(owner_tbl) == 1                                   # таблиця виправлення зведення


@pytest.mark.parametrize("pid,key,needles", [
    (2, "olx:10BkYC", ["знято з продажу 03.10.2026", "Перевірити зараз"]),
    (3, "domria:30000004", ["у карантині", "ціна за м² поза межами сегмента"]),
    (6, "blago:41685", ["актуальність не підтверджена"]),
    (11, "domria:30000013", ["позначено неактуальним вручну"]),
    (1, "domria:34616500", ["DIM.RIA — активне"]),
])
def test_banner_states_the_status_in_plain_words(env, pid, key, needles):
    html = env.friend.get(f"/property/{pid}?verify=0&hl={key}").text
    for n in needles:
        assert n in html, (n, html[:4000])


def test_bogus_or_hostile_hl_is_ignored(env):
    plain = env.owner.get("/property/1?verify=0").text
    for hl in ['"><script>alert(1)</script>', "olx:10BkYC<b>", "own:1", "x" * 500,
               "olx:nosuchkey", "javascript:alert(1)"]:
        r = env.owner.get("/property/1", params={"verify": "0", "hl": hl})
        assert r.status_code == 200
        assert "<script>alert(1)" not in r.text and "javascript:alert" not in r.text
        assert 'id="found"' not in r.text and '<li class="hl">' not in r.text
    assert 'id="found"' not in plain


def test_find_page_never_reflects_raw_markup(env):
    r = env.owner.get("/find", params={"q": '<img src=x onerror=alert(1)>'})
    assert r.status_code == 200 and "<img src=x" not in r.text
    r = env.owner.get("/find", params={"err": '"><script>alert(1)</script>'})
    assert r.status_code == 200 and "<script>alert(1)" not in r.text


# --- Поле пошуку на кожній сторінці, без запитів до бази ---------------------------------------


@pytest.mark.parametrize("who", ["owner", "friend"])
def test_search_box_on_every_page_for_both_roles(env, who):
    c = getattr(env, who)
    pages = ["/", "/processing", "/analytics", "/places", "/property/1?verify=0", "/find"]
    if who == "owner":
        pages.append("/status")
    for path in pages:
        html = c.get(path, headers={"Accept": "text/html"}).text
        form = re.search(r'<form class="find" method="post" action="/find"[^>]*>(.*?)</form>',
                         html, re.S)
        assert form, path
        assert 'inputmode="url"' in form.group(1) and 'autocapitalize="none"' in form.group(1)
        assert 'name="q"' in form.group(1)
    if who == "friend":
        assert c.get("/status", headers={"Accept": "text/html"}).status_code == 403


def test_search_box_adds_no_sql_to_page_renders(env, monkeypatch):
    from realty.web import find_routes
    from realty.web.app import templates

    @contextmanager
    def statements():
        seen: list[str] = []

        def before(conn, cursor, statement, params, context, executemany):
            seen.append(" ".join(statement.split()))
        event.listen(env.engine, "before_cursor_execute", before)
        try:
            yield seen
        finally:
            event.remove(env.engine, "before_cursor_execute", before)

    with statements() as seen:
        html = templates.get_template("_find_box.html").render(find_ui=find_routes.ui_config)
    assert 'action="/find"' in html and seen == []
    env.owner.get("/")                                   # прогріти кеші списку
    with statements() as with_box:
        a = env.owner.get("/").text
    monkeypatch.setitem(templates.env.globals, "find_ui", lambda: None)
    with statements() as without_box:
        b = env.owner.get("/").text
    assert 'class="find"' in a and 'class="find"' not in b
    assert with_box == without_box


# --- Безпека запитів: same-origin, ролі, журнал ------------------------------------------------


def test_post_find_from_a_foreign_site_is_refused(env):
    r = env.owner.post("/find", data={"q": "34616500"},
                       headers={"Origin": "https://evil.example", "Accept": "text/html"})
    assert r.status_code == 403
    r = env.friend.post("/api/find/check", json={"q": RIA.format(39999999)},
                        headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_raw_input_never_reaches_logs_or_location(env, caplog):
    caplog.set_level(logging.DEBUG)
    secrets = ["utm_source", "fbclid", "SECRETTOKEN", "380670000001"]
    for text in ["https://380670000001.rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/"
                 "?utm_source=x&fbclid=y",
                 "https://chat.ria.com/2/1?secure=SECRETTOKEN",
                 RIA.format(39999999) + "?utm_source=x&fbclid=y",
                 "https://m.olx.ua/d/uk/obyavlenie/x-ID9zZzZ.html?utm_source=x&fbclid=y"]:
        r = _find(env.owner, text)
        loc = r.headers["location"]
        follow = env.owner.get(loc)
        for s in secrets:
            assert s not in loc and s not in caplog.text
            assert s not in follow.text
        q = r.headers["location"].split("q=", 1)[-1]
        if "q=" in r.headers["location"]:
            _check(env.owner, unquote(q))
    with ops.ops_session() as s:
        stored = " ".join(f"{j.key} {j.target} {j.message} {j.result}"
                          for j in s.scalars(select(ops.LookupCheck)))
    for s_ in secrets:
        assert s_ not in stored


# --- «Перевірити зараз»: черга, ліміт, повтор, цикл -------------------------------------------


def test_check_now_inserts_domria_through_the_quality_gate(env, monkeypatch):
    import realty.pipeline as pipeline

    def no_llm(*a, **k):
        raise AssertionError("LLM у перевірці за посиланням не викликається")
    monkeypatch.setattr(pipeline, "LLMExtractor", no_llm)
    env.net.answers["realty/data/39999999"] = (200, card(39999999))
    with env.Session() as s:
        high = max(s.scalars(select(Property.id)))
    r = _check(env.friend, RIA.format(39999999))
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert r.json()["state"] == "queued"
    assert env.net.calls == []                               # сайт у мережу не ходить
    done = _drain(job)
    assert done == [(job, "done")]
    assert len(env.net.calls) == 1 and env.net.calls[0][0] == "GET"
    assert "realty/data/39999999" in env.net.calls[0][1]
    with env.Session() as s:
        row = s.scalar(select(Listing).where(Listing.source == "domria",
                                             Listing.external_id == "39999999"))
        assert row is not None and row.site_key == "domria:39999999"
        assert row.quality_status in ("ok", "review") and row.quality_checked_at is not None
        assert "[телефон]" in row.description and "000 00 01" not in row.description
        assert row.property_id is not None and row.property_id > high
        assert s.scalar(select(func.count()).select_from(PriceEvent)
                        .where(PriceEvent.listing_id == row.id)) == 1
        pid = row.property_id
    st = env.friend.get(f"/api/find/check/{job}").json()
    assert st["final"] and st["outcome"] in ("check_added", "check_added_quarantine")
    assert st["property_url"] == f"/property/{pid}?hl=domria:39999999#found"
    page = env.friend.get(st["property_url"].split("#")[0] + "&verify=0")
    assert page.status_code == 200 and 'id="found"' in page.text
    # Тепер посилання знаходить нову квартиру.
    assert _find(env.owner, RIA.format(39999999)).headers["location"].startswith(f"/property/{pid}?")


@pytest.mark.parametrize("answer,outcome,text", [
    ((200, card(39999998, city_id=10, city_name_uk="Львів")), "check_not_city", "Львів"),
    ((200, card(39999998, is_delete=1)), "check_removed", "знято з продажу"),
    ((200, card(39999998, realty_type_id=1)), "check_not_flat", "не про продаж квартири"),
    ((404, None), "check_not_found_on_source", "не знайшов"),
    ((403, None), "check_blocked", "спробуйте пізніше"),
    ((200, "not json"), "check_unknown", "спробуйте пізніше"),
])
def test_domria_card_outcomes_write_nothing(env, answer, outcome, text):
    env.net.answers["realty/data/39999998"] = answer
    with env.Session() as s:
        before = s.scalar(select(func.count()).select_from(Listing))
    job = _check(env.owner, "39999998" if False else RIA.format(39999998)).json()["job"]
    _drain(job)
    st = env.owner.get(f"/api/find/check/{job}").json()
    assert st["outcome"] == outcome and text in st["text"]
    assert len(env.net.calls) == 1
    with env.Session() as s:
        assert s.scalar(select(func.count()).select_from(Listing)) == before


@pytest.mark.parametrize("code,outcome", [(200, "check_alive_not_added"), (410, "check_removed"),
                                          (404, "check_not_found_on_source"),
                                          (429, "check_blocked")])
def test_olx_and_rieltor_check_liveness_only(env, code, outcome):
    env.net.answers["ID9zZzZ"] = (code, None)
    env.net.answers["view/13999999"] = (code, None)
    with env.Session() as s:
        before = s.scalar(select(func.count()).select_from(Listing))
    for q in ["https://m.olx.ua/d/uk/obyavlenie/nova-kvartyra-ID9zZzZ.html?reason=x",
              "https://www.rieltor.ua/ivano-frankovsk/flats-sale/view/13999999/"]:
        job = _check(env.owner, q).json()["job"]
        _drain(job)
        assert env.owner.get(f"/api/find/check/{job}").json()["outcome"] == outcome
    # Рівно по одному запиту, без www у rieltor (www дає 404 живим) і без query.
    assert [m for m, _ in env.net.calls] == ["HEAD", "HEAD"]
    assert env.net.calls[1][1] == "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13999999/"
    assert all("?" not in u for _, u in env.net.calls)
    with env.Session() as s:
        assert s.scalar(select(func.count()).select_from(Listing)) == before


def test_check_of_a_known_key_uses_the_liveness_path(env):
    """Оголошення вже є в базі — перевірка актуальності правилами Блоку 1 (410 → знято),
    поля оголошення не переписуються, вердикт — і на рядок LUN того самого ключа."""
    env.net.answers["IDabc12"] = (410, None)
    with env.Session() as s:
        before = s.get(Listing, 2)
        snapshot = (before.price_usd, before.description, before.original_url, before.last_seen)
    job = _check(env.owner, "https://www.olx.ua/d/uk/obyavlenie/x-IDabc12.html").json()["job"]
    _drain(job)
    st = env.owner.get(f"/api/find/check/{job}").json()
    assert st["outcome"] == "check_delisted", st
    assert st["property_url"].startswith("/property/1?hl=olx:abc12")
    assert len(env.net.calls) == 1
    with env.Session() as s:
        row = s.get(Listing, 2)
        assert row.is_active is False
        assert (row.price_usd, row.description, row.original_url, row.last_seen) == snapshot


def test_disabled_sources_and_blago_refuse_check(env):
    assert _check(env.owner, "https://www.flombu.com/uk/prodazh/kvartyra-999999").json()["code"] \
        == "check_disabled"
    assert _check(env.owner, "https://lun.ua/uk/realty/9999999999").json()["code"] \
        == "check_disabled"
    assert _check(env.owner, "https://blagodeveloper.com/plannings/41685/").json()["code"] \
        == "blago_unverifiable"
    assert _check(env.owner, "olx:9zZzZ").json()["code"] == "need_full_link"
    assert _check(env.owner, "41685").json()["code"] == "need_full_link"
    with ops.ops_session() as s:
        assert s.scalar(select(func.count()).select_from(ops.LookupCheck)) == 0


def test_rate_limit_per_role_from_config(env, monkeypatch):
    _lookup_cfg(monkeypatch, per_role_per_hour={"owner": 30, "friend": 2})
    ids = [_check(env.friend, RIA.format(39999990 + i)) for i in range(2)]
    assert [r.status_code for r in ids] == [202, 202]
    third = _check(env.friend, RIA.format(39999993))
    assert third.status_code == 429
    body = third.json()
    assert body["code"] == "check_rate_limited" and "ліміт" in body["text"]
    assert 1 <= body["retry_after_s"] <= 3601 and third.headers["Retry-After"]
    # Повтор уже поставленого — не нова перевірка: той самий номер, ліміт не витрачається.
    again = _check(env.friend, RIA.format(39999990) + "?utm_source=x")
    assert again.status_code == 202 and again.json()["job"] == ids[0].json()["job"]
    assert again.json()["reused"] is True
    # Власник — свій ліміт.
    assert _check(env.owner, RIA.format(39999993)).status_code == 202
    # 0 — кнопки для ролі немає.
    _lookup_cfg(monkeypatch, per_role_per_hour={"owner": 30, "friend": 0})
    assert _check(env.friend, RIA.format(39999994)).status_code == 403
    page = env.friend.get(_find(env.friend, RIA.format(39999995)).headers["location"]).text
    assert "Перевірити зараз" not in page and "вимкнена" in page


def test_repeated_submits_reuse_one_job_and_one_request(env):
    env.net.answers["realty/data/39999997"] = (200, card(39999997, city_id=10))
    first = _check(env.friend, RIA.format(39999997)).json()["job"]
    # Власник бачить усі завдання — повтор того самого оголошення бере завдання друга.
    second = _check(env.owner, "https://m.dom.ria.com/ru/realty-x-39999997.html#gal").json()
    assert second["job"] == first and second["reused"]
    _drain(first)
    third = _check(env.friend, RIA.format(39999997)).json()
    assert third["job"] == first and third["final"] and third["outcome"] == "check_not_city"
    assert len(env.net.calls) == 1


def test_check_status_of_another_role_is_404_for_the_friend(env):
    """Рецензія E14 (08.10): стан «Перевірити зараз» читався будь-якою роллю за номером.
    Друг не бачить завдань власника (404, як неіснуюче) і не отримує їх повтором; власник
    бачить усі."""
    env.net.answers["realty/data/39999986"] = (200, card(39999986, city_id=10))
    owner_job = _check(env.owner, RIA.format(39999986)).json()["job"]
    assert env.friend.get(f"/api/find/check/{owner_job}").status_code == 404
    assert env.owner.get(f"/api/find/check/{owner_job}").status_code == 200
    mine = _check(env.friend, RIA.format(39999986)).json()
    assert mine["job"] != owner_job and not mine["reused"]
    assert env.friend.get(f"/api/find/check/{mine['job']}").status_code == 200
    assert env.owner.get(f"/api/find/check/{mine['job']}").status_code == 200


def test_check_waits_for_the_cycle_and_never_takes_its_lock(env, monkeypatch):
    env.net.answers["realty/data/39999996"] = (200, card(39999996))
    taken: list[Path] = []
    real_acquire = runner.CycleLock.acquire

    def spy(self):
        taken.append(self.path)
        return real_acquire(self)

    cycle = runner.CycleLock(env.opened.CYCLE_LOCK)
    assert cycle.acquire()                                  # «іде цикл»
    monkeypatch.setattr(runner.CycleLock, "acquire", spy)
    try:
        r = _check(env.owner, RIA.format(39999996)).json()
        assert r["state"] == "deferred" and "цикл збору" in r["text"]
        assert _drain(r["job"]) == [(r["job"], "deferred")]
        assert env.net.calls == []                           # ні запиту, ні запису
        with env.Session() as s:
            assert s.scalar(select(Listing.id).where(Listing.external_id == "39999996")) is None
    finally:
        cycle.release()
    assert env.opened.CYCLE_LOCK not in taken                # замок циклу лише дивились
    assert _drain(r["job"]) == [(r["job"], "done")]
    assert _drain(r["job"]) == [(r["job"], "taken")]
    with env.Session() as s:
        assert s.scalar(select(func.count()).select_from(Listing)
                        .where(Listing.external_id == "39999996")) == 1
    assert len(env.net.calls) == 1


def test_cycle_started_during_the_request_defers_the_write(env, monkeypatch):
    """Цикл почався, поки йшов запит: відповідь відкидається, запис — після циклу."""
    env.net.answers["realty/data/39999995"] = (200, card(39999995))
    job = _check(env.owner, RIA.format(39999995)).json()["job"]
    cycle = runner.CycleLock(env.opened.CYCLE_LOCK)
    real = env.net.check

    def check_then_cycle(*a, **k):
        res = real(*a, **k)
        assert cycle.acquire()
        return res
    monkeypatch.setattr(env.net, "check", check_then_cycle)
    try:
        assert _drain(job) == [(job, "deferred")]
        with env.Session() as s:
            assert s.scalar(select(Listing.id).where(Listing.external_id == "39999995")) is None
    finally:
        cycle.release()
    monkeypatch.setattr(env.net, "check", real)
    assert _drain(job) == [(job, "done")]
    with env.Session() as s:
        assert s.scalar(select(Listing.id).where(Listing.external_id == "39999995")) is not None


def test_check_now_is_queued_for_the_worker_and_launches_it(env):
    from realty.web import livecheck

    import conftest
    conftest.LAUNCHED.clear()
    job = _check(env.owner, RIA.format(39999989)).json()["job"]
    livecheck.LIVE.tick()
    assert conftest.LAUNCHED == [job]


def test_collector_off_closes_the_job_without_network(env):
    env.opened.DISABLED_FLAG.write_text("")
    job = _check(env.owner, RIA.format(39999988)).json()["job"]
    assert _drain(job) == [(job, "skipped")]
    assert env.owner.get(f"/api/find/check/{job}").json()["outcome"] == "check_collector_off"
    assert env.net.calls == []


def test_unknown_job_status_is_404(env):
    assert env.friend.get("/api/find/check/999").status_code == 404


def test_new_singleton_property_survives_the_next_rebuild(env):
    """Квартира з перевірки — окремий id; перебудова «дублі» лишає його або переадресовує."""
    from realty import dedup

    env.net.answers["realty/data/39999987"] = (200, card(39999987))
    job = _check(env.owner, RIA.format(39999987)).json()["job"]
    _drain(job)
    pid = env.owner.get(f"/api/find/check/{job}").json()["property_url"].split("?")[0]
    with env.Session() as s:
        dedup.rebuild(s)
        s.commit()
    r = env.owner.get(pid + "?verify=0")
    assert r.status_code in (200, 302)


# --- Рецензія E14 (08.10) ---------------------------------------------------------------------


@pytest.mark.parametrize("text,city", [
    ("https://dom.ria.com/uk/realty-prodaja-kvartira-kiev-pecherskiy-lesi-ukrainki-39999970.html",
     "kiev"),
    ("https://rieltor.ua/kiev/flats-sale/view/13999970/", "kiev"),
    ("https://www.rieltor.ua/lvov-2105/flats-sale/view/13999971/?utm_source=x", "lvov"),
])
def test_other_city_from_the_url_is_told_without_a_request(env, text, city):
    """Рецензія E14 (08.10): місто з адреси (slug DIM.RIA, сегмент rieltor) — не наше:
    «схоже, не з Івано-Франківська» одразу, без запиту; кнопка лишається підтвердженням."""
    loc = _find(env.owner, text).headers["location"]
    page = env.owner.get(loc).text
    assert "схоже, не з Івано-Франківська" in page and f"«{city}»" in page
    assert "ще немає" not in page and "Перевірити зараз" in page
    assert env.net.calls == []


@pytest.mark.parametrize("text", [
    "https://dom.ria.com/uk/realty-prodaja-kvartira-krihovtsy-x-39999972.html",
    "https://rieltor.ua/nikitintsy-637/flats-sale/view/13999973/",
    "https://rieltor.ua/flats-sale/view/13999974/",                  # без сегмента — не знаємо
    "https://dom.ria.com/realty/data/39999975?lang_id=4",            # картка API — не знаємо
])
def test_own_area_or_unknown_place_stays_not_in_db(env, text):
    page = env.owner.get(_find(env.owner, text).headers["location"]).text
    assert "ще немає" in page and "схоже, не з" not in page


def test_unplaced_quarantined_row_shows_the_quarantine_reason(env):
    """Рецензія E14 (08.10): незведене оголошення в карантині не стане квартирою після
    «дублі» — пишемо карантин і причину, а не «ще не зведене»."""
    with env.Session() as s:
        s.add(_l(14, None, external_id="30000014", original_url=RIA.format(30000014),
                 quality_status="review", quality_reason="площа менша за мінімум"))
        s.commit()
    page = env.owner.get(_find(env.owner, RIA.format(30000014)).headers["location"]).text
    assert "в карантині якості" in page and "площа менша за мінімум" in page
    assert "ще не зведене" not in page


def test_trailing_slash_inside_share_text_is_stripped(env):
    """Рецензія E14 (08.10): «Глянь https://…-ID10BkYC.html/ гарна» — not_listing, бо
    повтор без слеша робився лише для тексту без пробілів."""
    from realty.web.find_routes import parse_input

    for text in ("Глянь https://www.olx.ua/d/uk/obyavlenie/kvartyra-ID10BkYC.html/ гарна",
                 "Глянь https://www.olx.ua/d/uk/obyavlenie/kvartyra-ID10BkYC.html/, гарна",
                 "dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html/ ось"):
        got = parse_input(text)
        assert getattr(got, "key", None) in ("olx:10BkYC", "domria:34616500"), (text, got)
    r = _find(env.owner, "Глянь https://www.olx.ua/d/uk/obyavlenie/kvartyra-ID10BkYC.html/ гарна")
    assert r.headers["location"].startswith("/property/2?hl=olx:10BkYC")


def test_host_pauses_apply_between_link_jobs_of_one_run(env, monkeypatch):
    """Рецензія E14 (08.10): свіжий фетчер на кожне завдання починав паузи хостів з нуля —
    два завдання до одного сайту в одному прогоні йшли без паузи. Тепер обмежувач спільний
    на прогін: другий запит до olx.ua бачить перший."""
    from urllib.parse import urlsplit

    from realty import fetcher as fetcher_mod

    seen: list[tuple[str, bool]] = []

    class Recording(fetcher_mod.RateLimiter):
        def wait(self, url, delay=None):
            host = urlsplit(url).netloc
            seen.append((host, host in self._last))           # чи пам'ятає попередній
            self._last[host] = 0.0

    class Paced(FakeNet):
        def __init__(self):
            super().__init__()
            self.limiter = Recording(1.0)

        def check(self, url, method="HEAD", delay=None, max_bytes=0):
            self.limiter.wait(url, delay)
            return super().check(url, method, delay, max_bytes)

    monkeypatch.setattr(fetcher_mod, "RateLimiter", Recording)
    monkeypatch.setattr(env.link, "_make_fetcher", Paced)
    jobs = [_check(env.owner, f"https://www.olx.ua/d/uk/obyavlenie/nova-ID9zZz{c}.html").json()["job"]
            for c in "AB"]
    done = _drain(jobs[0])
    assert [st for _j, st in done] == ["done", "done"]
    assert [h for h, _ in seen] == ["www.olx.ua", "www.olx.ua"]
    assert seen[1][1] is True                                  # пауза хоста діє між завданнями


def test_cycle_started_during_a_known_key_check_writes_nothing(env, monkeypatch):
    """Рецензія E14 (08.10): шлях наявного ключа (Блок 1) застосовував вердикт, навіть якщо
    цикл почався, поки йшов запит. Тепер — перевірка замка перед записом, відкладення."""
    env.net.answers["IDabc12"] = (410, None)
    job = _check(env.owner, "https://www.olx.ua/d/uk/obyavlenie/x-IDabc12.html").json()["job"]
    cycle = runner.CycleLock(env.opened.CYCLE_LOCK)
    real = env.net.check

    def check_then_cycle(*a, **k):
        res = real(*a, **k)
        if not cycle.path.exists() or runner.lock_busy(cycle.path) is None:
            assert cycle.acquire()
        return res
    monkeypatch.setattr(env.net, "check", check_then_cycle)
    try:
        assert _drain(job) == [(job, "deferred")]
        with env.Session() as s:
            assert s.get(Listing, 2).is_active is True               # нічого не записано
    finally:
        cycle.release()
    monkeypatch.setattr(env.net, "check", real)
    assert _drain(job) == [(job, "done")]
    assert env.owner.get(f"/api/find/check/{job}").json()["outcome"] == "check_delisted"
    with env.Session() as s:
        assert s.get(Listing, 2).is_active is False


def test_fuse_held_source_gets_a_plain_message(env):
    """Рецензія E14 (08.10): ключ сайту під запобіжником — «сайт не дав однозначної
    відповіді» було неправдою; тепер — check_held простими словами."""
    from realty.liveness import fuse

    fuse.trip([fuse.Trip(source="lun", reason="share", checked=20, removed=10, share=0.5)],
              run_id=None, mode="literal")
    env.net.answers["IDabc12"] = (410, None)
    job = _check(env.owner, "https://www.olx.ua/d/uk/obyavlenie/x-IDabc12.html").json()["job"]
    _drain(job)
    st = env.owner.get(f"/api/find/check/{job}").json()
    assert st["outcome"] == "check_held" and "запобіжник" in st["text"]
    with env.Session() as s:
        assert s.get(Listing, 2).is_active is True


def test_expired_and_stale_jobs_forget_the_target_url(env):
    """Рецензія E14 (08.10): target (адреса запиту) стирався лише в finish — у відкладених,
    що застаріли, і в покинутих у черзі він лишався в ops.db назавжди."""
    from realty.lookup import queue

    old = NOW - timedelta(days=2)
    with ops.ops_session() as s:
        for state in ("deferred", "queued", "running"):
            s.add(ops.LookupCheck(kind=queue.KIND_LINK, key=f"link:olx:9zZz{state[:2]}",
                                  state=state, role="owner", created_at=old, started_at=old,
                                  target="https://www.olx.ua/d/uk/obyavlenie/x-ID9zZzZ.html"))
    fresh = _check(env.owner, "https://m.olx.ua/d/uk/obyavlenie/nova-ID9zZzY.html").json()["job"]
    _drain(fresh)
    with ops.ops_session() as s:
        rows = s.scalars(select(ops.LookupCheck)).all()
        assert all(r.target is None for r in rows), [(r.state, r.target) for r in rows]
        states = sorted(r.state for r in rows if r.id != fresh)
    assert states == ["failed", "skipped", "skipped"]
