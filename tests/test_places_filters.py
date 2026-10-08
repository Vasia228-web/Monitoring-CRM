"""Фільтри «Район», «ЖК», «тільки місто» і сторінка «Райони й ЖК» (Блок 4, E10, D57).

Головна вимога: число біля КОЖНОГО варіанта дорівнює кількості рядків, які покаже
список із цим варіантом (за тих самих інших фільтрів, зі згортанням і без, з кімнатами й
без). Перевіряється через публічний шлях — сторінки «/» і «/places», як їх бачить людина.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import places_kit as kit  # noqa: E402
from realty import ops  # noqa: E402
from realty.analytics import cache, objects, segments  # noqa: E402
from realty.web import sessions  # noqa: E402
from realty.web.app import app  # noqa: E402

OWNER, OWNER_PW = "vasia", "пароль власника 1"
FRIEND, FRIEND_PW = "druh", "druh-pass-2"
SITE = "https://mojkvartiry.test"


def _login(c, user, pw, ip):
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"},
               headers={"CF-Connecting-IP": ip, "Accept": "text/html"})
    assert r.status_code == 303, r.text[:200]


@pytest.fixture
def site(tmp_path, monkeypatch):
    engine = kit.make_engine(tmp_path, "site.db")
    kit.build(engine, n=220)
    kit.assign(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    import realty.web.analytics_routes as routes
    import realty.web.app as appmod
    import realty.web.dedup_routes as dr
    import realty.web.places_routes  # noqa: F401
    import realty.web.status as status_mod
    from realty.db import SessionLocal as _real  # noqa: F401
    for mod in (routes, appmod, status_mod):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr("realty.db.SessionLocal", Session)
    monkeypatch.setattr(dr, "session_scope", lambda: kit.scope_for(Session))
    from realty import backup, dedup_audit, dedup_sample  # noqa: F401 — таблиці ops
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.init_ops(force=True)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    monkeypatch.setenv("FRIEND_USER", FRIEND)
    monkeypatch.setenv("FRIEND_PASSWORD", FRIEND_PW)
    for mod in (segments, objects):
        monkeypatch.setattr(mod, "_now", lambda: kit.NOW)
    cache.invalidate()
    owner = TestClient(app, base_url=SITE, client=("127.0.0.1", 50000), follow_redirects=False)
    _login(owner, OWNER, OWNER_PW, "203.0.113.91")
    friend = TestClient(app, base_url=SITE, client=("127.0.0.1", 50001), follow_redirects=False)
    _login(friend, FRIEND, FRIEND_PW, "203.0.113.92")
    yield owner, friend, engine
    cache.invalidate()


_OPTION = re.compile(r'<option value="([^"]*)"[^>]*>([^<]*?)\((\d+)\)</option>')


def _select(html: str, name: str) -> list[tuple[str, int]]:
    m = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', html, re.S)
    assert m, f"немає списку {name}"
    return [(v, int(n)) for v, _label, n in _OPTION.findall(m.group(1))]


def _area(html: str) -> dict[str, int]:
    out = {}
    for value, n in re.findall(r'name="area" value="([^"]*)".*?\((\d+)\)</span>', html, re.S):
        out[value] = int(n)
    return out


def _matched(html: str) -> int:
    m = re.search(r"<b>(\d+)</b> за фільтром", html)
    if m:
        return int(m.group(1))
    return int(re.search(r'<span class="v num">(\d+)</span>', html).group(1))


def _get(c, **params) -> str:
    params = {k: v for k, v in params.items() if v not in (None,)}
    r = c.get("/?" + urlencode(params))
    assert r.status_code == 200, r.text[:300]
    return r.text


@pytest.mark.parametrize("extra", [{}, {"all_ads": "1"}, {"rooms": "1"},
                                   {"rooms": "2", "all_ads": "1"}, {"area": "city"}])
def test_option_counts_equal_result_counts(site, extra):
    owner, _friend, _engine = site
    page = _get(owner, **extra)
    districts = _select(page, "district")
    assert len(districts) >= 8                     # райони міста, села, «не визначено»
    checked = 0
    for value, n in districts:
        html = _get(owner, **{**extra, "district": value})
        assert _matched(html) == n, (extra, "district", value)
        checked += 1
        if not value:
            continue
        # ЖК у вибраному районі (і «не в ЖК», «не визначено»).
        for cvalue, cn in _select(html, "complex"):
            got = _matched(_get(owner, **{**extra, "district": value, "complex": cvalue}))
            assert got == cn, (extra, "complex", value, cvalue)
            checked += 1
        # Перемикач місцевості — з урахуванням району.
        for avalue, an in _area(html).items():
            got = _matched(_get(owner, **{**extra, "district": value, "area": avalue}))
            assert got == an, (extra, "area", value, avalue)
    assert checked > 20


def test_district_narrows_complex_list(site):
    owner, _f, _e = site
    keys = {v for v, _ in _select(_get(owner, district="pasichna"), "complex")}
    assert "comfort-park" in keys                    # ЖК Пасічни
    assert "skygarden" not in keys and "shepit" not in keys
    assert {"", "_none", "_unknown"} <= keys
    # Без району — список ЖК не показується (лише «усі», «не в ЖК», «не визначено»).
    assert {v for v, _ in _select(_get(owner), "complex")} == {"", "_none", "_unknown"}
    # ЖК, вибраний посиланням без району, у списку є (і фільтр діє).
    keys = {v for v, _ in _select(_get(owner, complex="skygarden"), "complex")}
    assert "skygarden" in keys


def test_area_switch(site):
    owner, _f, engine = site
    page = _get(owner)
    area = _area(page)
    assert area[""] == _matched(page)
    city = _get(owner, area="city")
    assert _matched(city) == area["city"] < area[""]
    # Села громади й «поза громадою» не видно в «тільки місто», невідома місцевість — видно.
    hromada = [v for v, _ in _select(page, "district") if v in ("krykhivtsi", "lysets")]
    assert hromada
    for v in hromada:
        assert _matched(_get(owner, district=v, area="city")) == 0
    assert _matched(_get(owner, district="_unknown", area="city")) == \
        _matched(_get(owner, district="_unknown"))
    # Сума груп «Місто / Громада / Поза громадою / Не визначено» = повна видача.
    total = sum(n for v, n in _select(page, "district") if v not in ("", ))
    # Батьківський район уже включає дітей — дітей не додаємо вдруге.
    children = {"nimetska-koloniia"}
    total -= sum(n for v, n in _select(page, "district") if v in children)
    assert total == _matched(page)


def test_unknown_key_warns_and_is_not_applied(site):
    owner, _f, _e = site
    full = _matched(_get(owner))
    html = _get(owner, district="nemaie", complex="nemaie-zhk", area="village")
    assert "Район не знайдено в довіднику — фільтр не застосовано." in html
    assert "ЖК не знайдено в довіднику" in html
    assert _matched(html) == full


def test_state_in_url_friend_and_pagination(site):
    owner, friend, _e = site
    from realty.web.navstate import carry

    state = {"district": "pasichna", "complex": "_none", "area": "city", "rooms": "2",
             "sort": "price_asc", "page": "2"}
    assert carry("/processing", state).startswith("/processing?rooms=2&district=pasichna&"
                                                  "complex=_none&area=city")
    assert carry("/places", state) == "/places?rooms=2&district=pasichna&complex=_none&area=city"
    # Друг: фільтри й /places працюють, панель статусу — ні.
    assert friend.get("/?district=pasichna").status_code == 200
    assert friend.get("/places?area=city").status_code == 200
    assert friend.get("/api/status/places").status_code == 403
    assert owner.get("/api/status/places").status_code == 200
    # Посилання вкладок несуть стан.
    html = _get(owner, district="pasichna", area="city")
    assert 'href="/places?district=pasichna&amp;area=city"' in html
    assert 'href="/processing?district=pasichna&amp;area=city' in html
    # Сторінки списку з фільтром не перетинаються, а разом — уся вибірка.
    from realty.web.pagination import PAGE_SIZES

    size = min(PAGE_SIZES)
    total = _matched(_get(owner, area="city", all_ads="1"))
    assert total > size
    ids = []
    for page in range(1, -(-total // size) + 1):
        r = owner.get(f"/?area=city&all_ads=1&per_page={size}&page={page}")
        ids += re.findall(r'data-id="(\d+)"\s+data-taken', r.text)
    assert len(ids) == len(set(ids)) == total


def test_places_page_counts_equal_the_list(site):
    """Кожне число на «Райони й ЖК» — рівно стільки рядків відкриває посилання."""
    owner, _f, _e = site
    for extra in ({}, {"area": "city"}, {"rooms": "3"}):
        html = owner.get("/places?" + urlencode(extra)).text
        links = re.findall(r'<a href="(/\?[^"]+)">[^<]*</a>\s*<span class="n">(\d+)</span>', html)
        assert len(links) > 5
        for href, n in links:
            r = owner.get(href.replace("&amp;", "&"))
            assert _matched(r.text) == int(n), (extra, href)


def test_table_shows_normalized_place_not_poi(site):
    owner, _f, _e = site
    html = _get(owner, district="pasichna", per_page="200")
    assert "Пасічна" in html
    assert 'ТЦ "Арсен"' not in html and "ТЦ &#34;Арсен&#34;" not in html
    html = _get(owner, district="krykhivtsi", per_page="200")
    assert "Крихівці · <span class=\"tag\">громада</span>" in html


@pytest.fixture
def plain_site(tmp_path, monkeypatch):
    """Той самий сайт, але row_* заповнені прямо в базі, без кроку «райони й ЖК» — так
    тест іде лише публічним шляхом (GET «/») і на коді до E10 падає на поведінці
    (параметр district там ігнорується), а не на імпорті нового модуля."""
    engine = kit.make_engine(tmp_path, "plain.db")
    kit.build(engine, n=120)
    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        have = {c["name"] for c in inspect(conn).get_columns("listings")}
        for col in ("row_district", "row_complex", "row_area"):
            if col not in have:
                conn.execute(text(f"ALTER TABLE listings ADD COLUMN {col} VARCHAR(48)"))
        conn.execute(text("UPDATE listings SET row_district = 'pasichna', row_area = 'city' "
                          "WHERE coalesce(property_id, id) % 3 = 0"))
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    import realty.web.app as appmod
    monkeypatch.setattr(appmod, "SessionLocal", Session)
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.init_ops(force=True)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    for var in ("FRIEND_USER", "FRIEND_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    c = TestClient(app, base_url=SITE, client=("127.0.0.1", 50002), follow_redirects=False)
    _login(c, OWNER, OWNER_PW, "203.0.113.93")
    return c


def test_district_parameter_filters_the_list_public_path(plain_site):
    c = plain_site
    full = _matched(_get(c))
    html = _get(c, district="pasichna")
    got = _matched(html)
    assert 0 < got < full                                 # параметр діє, а не ігнорується
    options = dict(_select(html, "district"))
    assert options["pasichna"] == got
    assert options["_unknown"] == full - got
    assert _matched(_get(c, district="_unknown")) == full - got


# --- Рецензія E10: покоління, холодний шлях, API, без JS, підписи ------------------------------


def test_counts_stay_equal_when_generation_changes_mid_request(site, monkeypatch):
    """Між списком id і лічильниками фоновий потік побачив нове покоління «lists»
    (крок циклу записав зміни): лічильники зі старого списку не мають лягти під нове
    покоління — наступний перегляд показує числа, рівні видачі (рецензія E10, race.py)."""
    from sqlalchemy import text

    from realty import webcache
    from realty.web import speedcache

    owner, _f, engine = site
    webcache.GENERATIONS.poll()
    _get(owner)
    speedcache.BACKGROUND.join(30)
    real = speedcache.facets
    fired = []

    def racing(session, key, compute, **kw):
        if not fired:
            fired.append(1)
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE listings SET is_active = 0, manual_active = NULL WHERE id IN "
                    "(SELECT id FROM listings WHERE row_district IS NOT NULL AND rooms = 2 "
                    "AND is_active = 1 LIMIT 25)"))
            webcache.bump("lists", "тест: крок циклу")
            webcache.GENERATIONS.poll()
        return real(session, key, compute, **kw)

    monkeypatch.setattr(speedcache, "facets", racing)
    _get(owner, rooms="2")
    monkeypatch.setattr(speedcache, "facets", real)
    assert fired
    speedcache.BACKGROUND.join(30)
    page = _get(owner, rooms="2")
    for value, n in _select(page, "district"):
        assert _matched(_get(owner, rooms="2", district=value)) == n, value


def _capture_sql(engine):
    import threading

    from sqlalchemy import event

    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append((threading.current_thread().name, " ".join(statement.split())))

    event.listen(engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(engine, "before_cursor_execute", before)


def _map_scans(seen) -> list:
    """Повні проходи «id → row_*» усієї таблиці (карта лічильників) поза фоновим
    потоком. Список id із row_* поруч (ORDER BY — порядок показу) — не карта."""
    out = []
    for thread, sql in seen:
        if thread == "realty-speedcache" or "GROUP BY" in sql or "ORDER BY" in sql:
            continue
        if sql.startswith(("SELECT id, row_district", "SELECT listings.id, listings.row_district")):
            out.append(sql)
    return out


def test_cold_request_never_builds_place_map_on_request_path(site):
    """Перший перегляд після нового покоління: карту id → row_* будує лише фон, запит —
    один GROUP BY (рецензія E10: холодний «/» +13–17 мс на M4)."""
    from realty.web import speedcache

    owner, _f, engine = site
    speedcache.owner_changed("тест: холодний кеш")
    speedcache.BACKGROUND.join(30)
    for memo in (speedcache.LIST_IDS, speedcache.FACETS, speedcache.FACET_OPTIONS,
                 speedcache.PLACE_MAP):
        memo.clear()
    seen, stop = _capture_sql(engine)
    try:
        page = _get(owner, rooms="1")
    finally:
        stop()
    assert not _map_scans(seen)
    # Лічильники — з того самого проходу, що й список id (row_* поруч з id).
    assert any(s.startswith("SELECT listings.id, listings.row_district") and "ORDER BY" in s
               for _t, s in seen)
    assert not any("GROUP BY listings.row_district" in s for _t, s in seen)
    for value, n in _select(page, "district")[:6]:
        assert _matched(_get(owner, rooms="1", district=value)) == n
    # Карту будує фон — із затримкою, після відповіді.
    import time

    deadline = time.monotonic() + 10
    while not len(speedcache.PLACE_MAP) and time.monotonic() < deadline:
        time.sleep(0.05)
    speedcache.BACKGROUND.join(30)
    assert len(speedcache.PLACE_MAP) == 1


def test_place_map_only_clean_active_rows_interned_one_key(site):
    from sqlalchemy import text

    from realty.web import speedcache

    owner, _f, engine = site
    speedcache.owner_changed("тест: прогрів")
    speedcache.BACKGROUND.join(30)
    _get(owner)
    speedcache.BACKGROUND.join(30)
    assert len(speedcache.PLACE_MAP) == 1
    (m,) = [v for _t, v in speedcache.PLACE_MAP._data.values()]
    from realty.models import CLEAN_STATUSES

    with engine.connect() as conn:
        clean = set(conn.execute(text(
            "SELECT id FROM listings WHERE coalesce(manual_active, is_active) = 1 AND "
            f"quality_status IN ({', '.join(repr(s) for s in CLEAN_STATUSES)})")).scalars())
        everything = conn.execute(text("SELECT count(*) FROM listings")).scalar()
    assert set(m) == clean and len(m) < everything
    assert len({id(v) for v in m.values()}) == len(set(m.values()))      # інтерновані


def test_memo_disabled_counts_use_group_by_not_full_map(site, monkeypatch):
    from realty.web import speedcache

    owner, _f, engine = site
    monkeypatch.setattr(speedcache, "_memo_settings", lambda: (False, 1, 0.0))
    seen, stop = _capture_sql(engine)
    try:
        page = _get(owner)
    finally:
        stop()
    assert not _map_scans(seen)
    for value, n in _select(page, "district")[:5]:
        assert _matched(_get(owner, district=value)) == n


def test_api_listings_unknown_place_is_400(site):
    owner, friend, _e = site
    r = owner.get("/api/listings?district=nemaie")
    assert r.status_code == 400 and "Район не знайдено" in r.json()["error"]
    assert friend.get("/api/listings?complex=nemaie-zhk").status_code == 400
    ok = owner.get("/api/listings?district=pasichna&limit=5")
    assert ok.status_code == 200 and all(x["district_key"] == "pasichna" for x in ok.json())


def test_without_js_complex_outside_district_is_dropped(site):
    """Без JS зміна району надсилає і район, і старий ЖК: ЖК, якого в районі немає,
    скидається — і число біля району дорівнює видачі."""
    owner, _f, _e = site
    page = _get(owner)
    n = dict(_select(page, "district"))["pasichna"]
    html = _get(owner, district="pasichna", complex="skygarden")
    assert "ЖК скинуто — його немає у вибраному районі." in html
    assert _matched(html) == n
    assert '<option value="skygarden" selected' not in html


def test_group_titles_and_umbrella_labels_not_repeated(site):
    owner, _f, _e = site
    page = _get(owner)
    assert '<optgroup label="Громада (села)">' in page and "— громада" not in page
    assert "— поза громадою" not in page
    places = owner.get("/places").text
    assert "Громада (села)<span" in places and "Громада (села) — " not in places
    html = _get(owner, district="kniahynyn", all_ads="1", per_page="200")
    assert "ЖК Житловий район" not in html and "Житловий район Княгинин" in html


def test_before_first_assign_site_looks_like_before_e10(site):
    """Крок «райони й ЖК» у циклі вимкнено до перегляду вибірки (рецензія E10): поки він
    нічого не визначив, сайт показує сирий район (як до E10), без фільтрів місця, а не
    «район не визначено» на кожному рядку."""
    from sqlalchemy import text

    from realty.web import speedcache

    owner, friend, engine = site
    with engine.begin() as conn:
        conn.execute(text("UPDATE listings SET row_district = NULL, row_complex = NULL, "
                          "row_area = NULL"))
    speedcache.owner_changed("тест: до першого кроку")
    html = _get(owner, per_page="200")
    assert '<select name="district"' not in html and "район не визначено" not in html
    assert '<span class="sub">Центр</span>' in html               # сире поле DOM.RIA
    assert "Райони й ЖК ще не визначено" in _get(owner, district="pasichna")
    assert "чекає перегляду" in friend.get("/places").text
