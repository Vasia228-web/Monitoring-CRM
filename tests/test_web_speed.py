"""Шлях запиту сайту без зайвої роботи (Блок 2, крок E5, D50).

Кеш списку й зведення за поколінням даних, знімок «Аналітики» з фоновою
перебудовою, GET без записів у базу, перевірка при відкритті квартири — чергою
й окремим процесом (сайт сам у мережу не ходить), перевірка входу поза event
loop, стиснення, опитування /status із конфігу.

Швидкість тут перевіряється не секундоміром (на Fedora N3540 і під
навантаженням пороги часу ненадійні), а лічильниками запитів SQL, подіями й
бар'єрами: «другий запит не виконав запиту списку», «сторінка відповіла, поки
інше з'єднання тримає блокування запису», «запит пройшов, поки перевірка
входу іншого запиту стоїть на бар'єрі».
"""
from __future__ import annotations

import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import ops  # noqa: E402
from realty.analytics import cache, objects, segments  # noqa: E402
from realty.models import Base, Condition, Listing, MarketType, Property  # noqa: E402
from realty.web import sessions  # noqa: E402
from realty.web.app import app  # noqa: E402

NOW = datetime(2026, 10, 7, 8, 0, 0)
OWNER, OWNER_PW = "vasia", "пароль власника 1"
SITE = "https://mojkvartiry.test"


def _mod(name):
    import importlib
    return importlib.import_module(name)


def _l(i, pid, **kw):
    base = dict(source="domria", external_id=str(i), original_url=f"https://dom.ria.com/uk/x-{i}.html",
                price=50_000 + i, currency="USD", price_usd=50_000.0 + 100 * i, rooms=2,
                area_total=50.0, price_per_sqm=1000.0 + i, floor=5, floors_total=9,
                location="вул. Стуса, 30", market_type=MarketType.SECONDARY,
                condition=Condition.RENOVATED, published_at=NOW - timedelta(days=40),
                first_seen=NOW - timedelta(days=30), last_seen=NOW, quality_status="ok",
                property_id=pid, is_active=True)
    base.update(kw)
    return Listing(id=i, **base)


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Синтетична база з індексами моделі, своя ops.db, сайт із входом власника."""
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        for pid in range(1, 61):
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=50.0,
                           price_usd_min=50_000.0, price_per_sqm=1000.0,
                           first_seen=NOW - timedelta(days=30), last_seen=NOW))
        s.flush()
        s.add_all([_l(1, 1), _l(2, 1, source="olx",
                                original_url="https://www.olx.ua/d/uk/obyavlenie/x-IDabc.html"),
                   _l(3, 1, source="lun")]
                  + [_l(10 + pid, pid) for pid in range(2, 61)])
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

    import realty.web.analytics_routes as routes
    import realty.web.app as appmod
    import realty.web.dedup_routes as dr
    import realty.web.status as status_mod
    for mod in (routes, appmod, status_mod):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(dr, "session_scope", scope)
    from realty import backup, dedup_audit, dedup_sample  # noqa: F401 — таблиці ops
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=ops_engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(ops_engine)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    for var in ("FRIEND_USER", "FRIEND_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    for mod in (segments, objects):
        monkeypatch.setattr(mod, "_now", lambda: NOW)
    cache.invalidate()
    try:
        _mod("realty.webcache").GENERATIONS.poll()
    except ImportError:
        pass
    c = TestClient(app, base_url=SITE, client=("127.0.0.1", 50000), follow_redirects=False)
    r = c.post("/login", data={"username": OWNER, "password": OWNER_PW, "next": "/"},
               headers={"CF-Connecting-IP": "203.0.113.91", "Accept": "text/html"})
    assert r.status_code == 303
    yield c, Session, engine
    cache.invalidate()


@contextmanager
def statements(engine):
    """Усі SQL, виконані на рушії, поки діє контекст."""
    seen: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _post(c, url, payload=None):
    r = c.post(url, json=payload or {}, headers={"Origin": SITE})
    assert r.status_code == 200, (url, r.status_code, r.text[:200])
    return r.json()


def _matched(html: str) -> int:
    """Лічильник шапки: «N за фільтром» або, без фільтра, «N оголошень»."""
    m = re.search(r"<b>(\d+)</b> за фільтром", html)
    if m:
        return int(m.group(1))
    return int(re.search(r'<span class="v num">(\d+)</span>', html).group(1))


def _rows(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'data-id="(\d+)"', html)]


def _run_jobs(job_id: int):
    """Те, що робить процес realty-lookup@ (`cli.py lookup check --job N`), з
    лімітами з config/speed.toml, як у cmd_lookup."""
    from realty import configfiles

    cfg = configfiles.load("speed").open_check
    return _mod("realty.lookup.opened").run(job_id, budget_s=cfg.drain_budget_s,
                                            timeout_s=cfg.job_timeout_s)


def _seen_marks(Session) -> tuple:
    """Охоронець D43: «коли бачили» змінює лише справжнє побачення в стрічці,
    а не будь-який запис у рядок (інтеграційний план, перелік регресій)."""
    with Session() as s:
        return tuple(s.execute(text(
            "SELECT max(last_seen), total(julianday(last_seen)), count(*) FROM listings")).one())


# --- Список і зведення: раз на покоління ------------------------------------------------------


def test_list_and_summary_are_computed_once_per_generation(site):
    """Другий перегляд «/» не рахує ні списку id, ні зведення; нове покоління — рахує.

    На старому коді кожен перегляд робив запит-лічильник і п'ять проходів
    таблиці для зведення.
    """
    c, _Session, engine = site
    first = c.get("/").text
    with statements(engine) as seen:
        second = c.get("/").text
    assert second == first
    assert not [q for q in seen if q.startswith("SELECT count(*)") and "FROM (SELECT" in q]
    assert not [q for q in seen if "GROUP BY listings.quality_status" in q], seen
    assert not [q for q in seen if q.startswith("SELECT listings.id FROM listings")], seen
    # Нове покоління «lists» (крок циклу) — і список, і зведення рахуються знову.
    webcache = _mod("realty.webcache")
    webcache.bump("lists", "тест")
    webcache.GENERATIONS.poll()
    with statements(engine) as seen:
        third = c.get("/").text
    assert third == first
    assert [q for q in seen if q.startswith("SELECT listings.id FROM listings")], seen
    assert [q for q in seen if "GROUP BY listings.quality_status" in q], seen


def test_list_key_covers_every_filter(monkeypatch):
    """Новий фільтр у navstate.LIST_KEYS потрапляє в ключ кешу або валить запит.

    Інакше дві різні вибірки ділили б один запис кешу (інтеграція: ключ —
    з LIST_KEYS, Блоки 3/4 додаватимуть туди фільтри).
    """
    speedcache = _mod("realty.web.speedcache")
    state = {"condition": "", "market": "", "source": "", "rooms": "2", "price_min": None,
             "price_max": None, "sort": "price_desc", "all_ads": ""}
    a = speedcache.list_key(state, in_progress=None, collapse=True)
    b = speedcache.list_key({**state, "rooms": "3"}, in_progress=None, collapse=True)
    assert a != b
    # Нумерація сторінки списку id не змінює — і в ключ не входить.
    assert "page" not in dict(a[0]) and "per_page" not in dict(a[0])
    monkeypatch.setattr(speedcache, "LIST_KEYS", (*speedcache.LIST_KEYS, "district"))
    with pytest.raises(KeyError):
        speedcache.list_key(state, in_progress=None, collapse=True)


def test_list_with_cache_equals_live_query_on_every_page(site):
    """Список із кешу — ті самі рядки й лічильник, що й живий запит, на кожній сторінці."""
    c, Session, _engine = site
    queries = _mod("realty.web.queries")
    for params in ({}, {"sort": "price_asc"}, {"all_ads": "1"}, {"per_page": "50", "page": "2"}):
        html = c.get("/", params=params).text
        collapse = params.get("all_ads") != "1"
        stmt = queries.listing_query(sort=params.get("sort", "price_desc"), collapse=collapse)
        with Session() as s:
            live = [r.id for r in s.scalars(stmt)]
        page = int(params.get("page", "1"))
        assert _rows(html) == live[(page - 1) * 50: page * 50], params
        assert _matched(html) == len(live) or len(live) == 62


def test_memo_computes_once_for_concurrent_requests():
    """Два одночасні запити з тим самим ключем — один розрахунок (бар'єр, без часу)."""
    speedcache = _mod("realty.web.speedcache")
    memo = speedcache.Memo("test")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def compute():
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return "значення"

    out = []
    t1 = threading.Thread(target=lambda: out.append(memo.get("k", compute, max_keys=4,
                                                             max_age_s=60)))
    t1.start()
    assert entered.wait(10)
    t2 = threading.Thread(target=lambda: out.append(memo.get("k", compute, max_keys=4,
                                                             max_age_s=60)))
    t2.start()
    release.set()
    t1.join(10)
    t2.join(10)
    assert out == ["значення", "значення"] and len(calls) == 1


# --- Дії власника — одразу, при теплому кеші --------------------------------------------------


def test_owner_actions_beat_a_warm_cache(site):
    """Кеш уже заповнено (влучання є) — і все одно дія власника видна на наступному GET."""
    c, _Session, _engine = site
    speedcache = _mod("realty.web.speedcache")
    c.get("/?per_page=200")
    hits = speedcache.LIST_IDS.hits
    before = c.get("/?per_page=200").text
    assert speedcache.LIST_IDS.hits > hits, "кеш списку не працює — тест нічого б не довів"
    assert 25 in _rows(before)
    total = _matched(before)

    _post(c, "/api/listings/25/status", {"active": False})
    after = c.get("/?per_page=200").text
    assert 25 not in _rows(after) and _matched(after) == total - 1
    _post(c, "/api/listings/25/status", {"active": None})
    assert 25 in _rows(c.get("/?per_page=200").text)

    # «Взяти в обробку» — і список «В обробці», і значок у навігації.
    c.get("/processing")
    _post(c, "/api/properties/7/processing", {"in_progress": True})
    proc = c.get("/processing").text
    assert _rows(proc) == [17]
    assert 'В обробці<span class="count">1</span>' in proc


def test_split_shows_the_new_flat_without_waiting_for_a_rebuild(site, monkeypatch):
    """«Розділити» при наявному знімку: нова квартира — 200 одразу, точковим оновленням.

    Знімок «Аналітики» не будується заново в запиті: тут повну побудову
    заборонено — сторінка нової квартири однаково відкривається.
    """
    c, _Session, _engine = site
    assert c.get("/property/1?verify=0").status_code == 200
    holder = cache.HOLDER
    builds = holder.builds
    new = _post(c, "/api/dedup/split", {"property_id": 1, "listing_ids": [3]})["new_property_id"]
    assert holder.patches >= 1
    assert holder.wait_rebuild(30)                  # фонове перерахування зведень
    page = c.get(f"/property/{new}?verify=0")
    assert page.status_code == 200
    assert holder.builds == builds, "знімок перебудовано в запиті"
    assert "x-3.html" in page.text and "x-1.html" not in page.text


def test_universe_patch_equals_rebuild(site):
    """Точково оновлений знімок (після «розділити» і «злити») = повна перебудова."""
    _c, Session, _engine = site
    dedup = _mod("realty.dedup")
    with Session() as s:
        snap = cache.get(s, force=True)
        new_pid = dedup.split_off(s, 1, [2, 3])
        s.commit()
        assert cache.patch_properties(s, [1, new_pid])
        patched = cache.HOLDER.current().universe
        rebuilt = segments.build_universe(s)
    assert [vars(i) for i in patched.items] == [vars(i) for i in rebuilt.items]
    assert patched.by_id[new_pid].property_id == new_pid
    assert snap.universe is not patched                 # новий об'єкт, а не правка на місці
    assert cache.HOLDER.wait_rebuild(30)


def test_split_during_a_background_rebuild_is_not_lost(site, monkeypatch):
    """«Розділити», поки фонова перебудова вже прочитала базу: нова квартира не губиться.

    Перебудова читала базу ДО поділу; без повторного точкового оновлення її
    знімок підмінив би оновлений, і нова квартира знову стала б «невідомою».
    """
    c, Session, _engine = site
    assert c.get("/property/1?verify=0").status_code == 200
    webcache = _mod("realty.webcache")
    read, release = threading.Event(), threading.Event()
    real_build = cache._build

    def build_then_wait(session, generation):
        snap = real_build(session, generation)       # база прочитана ДО поділу
        read.set()
        assert release.wait(30)
        return snap

    monkeypatch.setattr(cache, "_build", build_then_wait)
    webcache.bump("analytics", "дублі")
    webcache.GENERATIONS.poll()
    assert read.wait(10)
    new = _post(c, "/api/dedup/split", {"property_id": 1, "listing_ids": [3]})["new_property_id"]
    release.set()
    assert cache.HOLDER.wait_rebuild(30)
    current = cache.HOLDER.current().universe
    assert new in current.by_id
    with Session() as s:
        rebuilt = segments.build_universe(s)
    assert [vars(i) for i in current.items] == [vars(i) for i in rebuilt.items]


# --- GET нічого не пише ------------------------------------------------------------------------


def test_property_page_does_not_write_and_ignores_a_write_lock(site):
    """Сторінка квартири відповідає, поки інше з'єднання тримає блокування запису.

    На старому коді GET робив UPDATE переглядів і чекав блокування (на Fedora
    12 с під 12-секундним блокуванням, 500 під 40-секундним). Тут — жодного
    DML у запиті; перегляди пишуться фоном, коли блокування знято.
    """
    c, Session, engine = site
    c.get("/property/5?verify=0")                       # знімок «Аналітики» вже є
    perf = _mod("realty.web.perf")
    perf.WRITER.flush_views()
    locker = engine.connect()
    locker.exec_driver_sql("BEGIN IMMEDIATE")
    done = threading.Event()
    result = {}

    def get():
        with statements(engine) as seen:
            result["status"] = c.get("/property/5?verify=0").status_code
        result["dml"] = [q for q in seen if q.split()[0] in ("INSERT", "UPDATE", "DELETE")]
        done.set()

    t = threading.Thread(target=get)
    t.start()
    finished = done.wait(15)                            # детектор зависання, не поріг швидкості
    locker.exec_driver_sql("ROLLBACK")
    locker.close()
    t.join(30)
    assert finished, "сторінка чекала на чуже блокування запису"
    assert result == {"status": 200, "dml": []}
    assert perf.WRITER.flush_views() == 1              # одне оголошення квартири 5
    with Session() as s:
        row = s.get(Listing, 15)
        assert row.views == 2 and row.viewed_at is not None


def test_views_buffer_equals_direct_writes_and_survives_a_busy_database(site):
    """Відкриття 3 квартир (одна двічі) → ті самі views/viewed_at, що й прямий запис."""
    c, Session, engine = site
    perf = _mod("realty.web.perf")
    perf.WRITER.flush_views()
    with Session() as s:
        before = {r.id: r.views or 0 for r in s.scalars(select(Listing))}
    seen_before = _seen_marks(Session)
    for pid in (1, 8, 8, 9):
        assert c.get(f"/property/{pid}?verify=0").status_code == 200
    expected = dict(before)
    for lid in (1, 2, 3, 18, 18, 19):
        expected[lid] += 1
    # База зайнята довше за короткий тайм-аут фонового запису — нічого не губиться.
    locker = engine.connect()
    locker.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        assert perf.WRITER.flush_views() == 0
        assert set(perf.WRITER.pending_views()) == {1, 2, 3, 18, 19}
    finally:
        locker.exec_driver_sql("ROLLBACK")
        locker.close()
    perf.WRITER.stop()                                 # зупинка сайту дописує буфер
    with Session() as s:
        got = {r.id: r.views or 0 for r in s.scalars(select(Listing))}
        assert all(s.get(Listing, i).viewed_at is not None for i in (1, 2, 3, 18, 19))
    assert got == expected
    assert _seen_marks(Session) == seen_before            # перегляди не «бачення» (D43)


# --- Перевірка при відкритті — чергою, без мережі в сайті ---------------------------------------


class _FakeFetcher:
    """Замість HTTP: OLX відповідає 410 (знято), решта — 200. Рахує запити."""

    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    def probe(self, url, delay=0.0):
        _FakeFetcher.calls.append(url)
        return 410 if "olx.ua" in url else 200

    def close(self):
        pass


def test_open_check_is_queued_and_shown_by_long_poll(site, monkeypatch):
    """Відкриття квартири не чекає джерел: завдання в черзі, результат — long-poll.

    На старому коді GET сам викликав verify_batch (HEAD-запити в запиті).
    Повторне відкриття в межах run.opened_recheck_minutes нових запитів не робить.
    """
    c, Session, engine = site
    verify = _mod("realty.verify")
    livecheck = _mod("realty.web.livecheck")
    queue = _mod("realty.lookup.queue")
    _FakeFetcher.calls = []
    monkeypatch.setattr(verify, "Fetcher", _FakeFetcher)
    # Процес перевірки працює з тією самою базою, що й сайт (DB_URL у його
    # оточенні); у тесті — синтетична база фікстури.
    import realty.db as dbmod

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(verify, "session_scope", scope)
    launched = []
    monkeypatch.setattr(livecheck, "launch", lambda job: launched.append(job) or "test")

    page = c.get("/property/1")
    assert page.status_code == 200
    assert _FakeFetcher.calls == [], "сторінка ходила до джерела в самому запиті"
    assert "<!--live-check-->" in page.text and "/api/property/1/liveness" in page.text
    assert livecheck.LIVE.join(10)
    assert len(launched) == 1
    job = queue.get(launched[0])
    assert (job.kind, job.key, job.state) == ("opened", "property:1", "queued")

    # Те, що зробив би процес realty-lookup@ (cli.py lookup check --job N).
    seen_before = _seen_marks(Session)
    assert _run_jobs(launched[0]) == [(launched[0], "done")]
    assert _seen_marks(Session) == seen_before            # перевірка — не «бачення» (D43)
    assert sorted(_FakeFetcher.calls) == sorted(
        ["https://dom.ria.com/uk/x-1.html", "https://dom.ria.com/uk/x-3.html",
         "https://www.olx.ua/d/uk/obyavlenie/x-IDabc.html"])
    state = c.get("/api/property/1/liveness?wait=5").json()
    assert state["state"] == "done" and state["delisted"] == 1 and state["checked"] == 3
    with Session() as s:
        assert s.get(Listing, 2).is_active is False      # знято 410 — як і досі

    # Ще раз відкрили одразу — усе вже перевіряли щойно: ні завдання, ні запитів.
    _FakeFetcher.calls = []
    assert c.get("/property/1").status_code == 200
    assert livecheck.LIVE.join(10)
    assert len(launched) == 1 and _FakeFetcher.calls == []
    assert c.get("/api/property/1/liveness").json()["state"] == "skipped"
    # verify=0 — без перевірки й без блоку очікування.
    assert "<!--live-check-->" not in c.get("/property/1?verify=0").text


def test_repeated_opens_share_one_queued_job(site, monkeypatch):
    c, _Session, _engine = site
    livecheck = _mod("realty.web.livecheck")
    launched = []
    monkeypatch.setattr(livecheck, "launch", lambda job: launched.append(job) or "test")
    for _ in range(3):
        assert c.get("/property/9").status_code == 200
        assert livecheck.LIVE.join(10)
    assert len(launched) == 1
    assert c.get("/api/property/9/liveness").json()["state"] == "queued"


def test_liveness_route_is_for_both_roles_and_needs_login(site, monkeypatch):
    c, _Session, _engine = site
    anon = TestClient(app, base_url=SITE)
    assert anon.get("/api/property/1/liveness").status_code == 401
    assert c.get("/api/property/1/liveness").json()["state"] in ("none", "skipped",
                                                                 "pending", "queued")


# --- Повільна перебудова не затримує інших -------------------------------------------------------


def test_slow_snapshot_rebuild_does_not_delay_the_next_requests(site, monkeypatch):
    """Перебудова знімка стоїть на бар'єрі — /analytics, «/» і квартира відповідають.

    На старому коді перебудова йшла в запиті під глобальним замком, і всі
    наступні запити «Аналітики» й квартир чекали на неї (покинутий /analytics
    затримував наступну вкладку: на M4 «/» 181 → 1 049 мс).
    """
    c, _Session, _engine = site
    assert c.get("/analytics").status_code == 200        # знімок є
    webcache = _mod("realty.webcache")
    entered, release = threading.Event(), threading.Event()
    real_build = cache._build

    def slow_build(session, generation):
        entered.set()
        assert release.wait(30)
        return real_build(session, generation)

    monkeypatch.setattr(cache, "_build", slow_build)
    webcache.bump("analytics", "дублі")
    webcache.GENERATIONS.poll()                          # фоновий потік сайту: нове покоління
    assert entered.wait(10), "фонова перебудова не почалась"
    results = {}

    def burst():
        for url in ("/analytics", "/", "/property/3?verify=0"):
            results[url] = c.get(url).status_code

    t = threading.Thread(target=burst)
    t.start()
    t.join(15)                                           # детектор зависання
    blocked = t.is_alive()
    release.set()
    t.join(30)
    assert not blocked, "запити чекали на перебудову знімка"
    assert results == {"/analytics": 200, "/": 200, "/property/3?verify=0": 200}
    assert cache.HOLDER.wait_rebuild(30)
    assert cache.HOLDER.current().generation == webcache.GENERATIONS.current("analytics")


# --- Вхід поза event loop -------------------------------------------------------------------------


def test_session_validation_runs_off_the_event_loop(site, monkeypatch):
    """Перевірка сесії стоїть (зайнята ops.db) — інші запити сайту йдуть.

    На старому коді validate виконувався просто на event loop: поки він
    чекав, стояли ВСІ запити (busy_timeout ops.db — 30 с).
    """
    c, _Session, _engine = site
    entered, release = threading.Event(), threading.Event()
    real = sessions.validate

    def slow_validate(*a, **k):
        entered.set()
        assert release.wait(30)
        return real(*a, **k)

    monkeypatch.setattr(sessions, "validate", slow_validate)
    with TestClient(app, base_url=SITE) as shared:
        shared.cookies.update(c.cookies)
        first = threading.Thread(target=lambda: shared.get("/api/stats"))
        first.start()
        assert entered.wait(10)
        other = {}
        second = threading.Thread(target=lambda: other.update(r=shared.get("/healthz")))
        second.start()
        second.join(10)                                    # детектор зависання
        went = not second.is_alive()
        release.set()
        first.join(30)
        second.join(30)
    assert went, "запит чекав на чужу перевірку сесії (event loop стояв)"
    assert other["r"].status_code == 200


def test_session_last_seen_is_written_in_the_background(site):
    """GET не пише last_seen сесії; фоновий запис — пише; «Вийти» не воскрешається."""
    c, _Session, _engine = site
    perf = _mod("realty.web.perf")
    with ops.ops_session() as s:
        row = s.scalars(select(sessions.AuthSession)).one()
        sid = row.sid
        row.last_seen = ops._now() - timedelta(hours=1)
    with statements(ops.engine) as seen:
        assert c.get("/api/stats").status_code == 200
    assert not [q for q in seen if q.startswith("UPDATE auth_sessions")], seen
    assert perf.WRITER.flush_touches() == 1
    with ops.ops_session() as s:
        assert ops._now() - s.get(sessions.AuthSession, sid).last_seen < timedelta(minutes=1)
    # Вихід: рядок сесії зник, відкладений запис його не повертає.
    perf.WRITER.touch_session(sid, ops._now())
    c.post("/logout", headers={"Origin": SITE})
    perf.WRITER.flush_touches()
    with ops.ops_session() as s:
        assert s.get(sessions.AuthSession, sid) is None


# --- Стиснення, Server-Timing, /status ----------------------------------------------------------


def test_gzip_on_origin_keeps_headers(site):
    c, _Session, _engine = site
    r = c.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert r.num_bytes_downloaded < len(r.content) * 0.25
    assert r.headers.get("x-robots-tag") == "noindex, nofollow"
    assert r.headers.get("server-timing", "").startswith("app;dur=")
    small = c.post("/api/listings/1/report", json={}, headers={"Origin": SITE,
                                                               "Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers           # дрібні відповіді — без стиснення


def test_status_polling_comes_from_config_and_pauses_in_background(site, tmp_path, monkeypatch):
    import shutil

    from realty import configfiles

    c, _Session, _engine = site
    html = c.get("/status").text
    assert "setInterval(refresh, 5000);" in html            # config: status_s = 5
    assert 'addEventListener("visibilitychange"' in html and "document.hidden" in html
    assert "/api/status/speed" in html                       # панель «Швидкість»
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    p = target / "speed.toml"
    p.write_text(p.read_text().replace("status_s = 5\n", "status_s = 7\n")
                 .replace("pause_hidden = true", "pause_hidden = false"))
    monkeypatch.setenv(configfiles.ENV_DIR, str(target))
    html = c.get("/status").text
    assert "setInterval(refresh, 7000);" in html
    assert "<!--status-poll-->" not in html


def test_speed_panel_polling_also_pauses_in_a_background_tab(site):
    """Обгортка паузи має стояти ДО панелі «Швидкість»: інакше її setInterval —
    справжній, і /api/status/speed опитується й у фоновій вкладці (рецензія)."""
    c, _Session, _engine = site
    html = c.get("/status").text
    assert html.index("window.setInterval = function") < html.index("setInterval(speed")
    assert html.index("window.setInterval = function") < html.index("setInterval(refresh")


# --- Зламаний конфіг на старті: фоновий потік сайту живе ----------------------------------------


def test_background_writer_survives_a_config_broken_at_start(site):
    """config/speed.toml не читається з першого разу — потік не вмирає (рецензія):
    тики (покоління даних, стан циклу) йдуть, перегляди записуються."""
    from realty import configfiles

    c, Session, engine = site
    deferred = _mod("realty.web.deferred")

    def broken():
        raise configfiles.ConfigError("speed.toml: зламано")

    clock = [1000.0]
    w = deferred.DeferredWriter(settings=broken, clock=lambda: clock[0])
    ticks = []
    w.add_tick(lambda: ticks.append(1))
    w.add("web_timings", {})                          # запит сайту кладе рядок — не падає
    w.take()
    with Session() as s:
        views = s.get(Listing, 15).views or 0
    w.add_views([15], NOW, engine=engine)
    clock[0] += deferred.EMERGENCY.deferred.views_flush_s
    w.run_once()
    assert ticks == [1] and w.views_written == 1
    with Session() as s:
        assert s.get(Listing, 15).views == views + 1


def test_emergency_values_are_the_config_values():
    """Аварійні числа — ті самі, що в config/speed.toml (не розходяться мовчки)."""
    from realty import configfiles

    cfg, e = configfiles.load("speed"), _mod("realty.web.deferred").EMERGENCY
    assert e.generations.poll_s == cfg.generations.poll_s == _mod("realty.webcache").EMERGENCY_POLL_S
    assert e.timings.flush_s == cfg.timings.flush_s
    assert (e.deferred.views_flush_s, e.deferred.busy_timeout_ms, e.deferred.max_buffer_rows) == \
        (cfg.deferred.views_flush_s, cfg.deferred.busy_timeout_ms, cfg.deferred.max_buffer_rows)


def test_generations_are_reread_even_with_a_broken_config(monkeypatch):
    from realty import configfiles

    webcache = _mod("realty.webcache")
    clock, reads = [0.0], []
    g = webcache.Generations(reader=lambda: reads.append(1) or {"lists": len(reads),
                                                                 "analytics": 0},
                             clock=lambda: clock[0])

    def broken(name):
        raise configfiles.ConfigError("зламано")

    monkeypatch.setattr(configfiles, "get", broken)
    assert g.current("lists") == 1
    clock[0] += 3 * webcache.EMERGENCY_POLL_S
    assert g.current("lists") == 2                    # без потоку — читає сам, а не «ніколи»


# --- Покоління: диригент і cli.py ----------------------------------------------------------------


def test_cycle_bumps_lists_after_every_step_and_analytics_after_dedup_and_at_the_end(
        tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker as sm

    from realty import runner

    webcache = _mod("realty.webcache")
    ops_engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", ops_engine)
    monkeypatch.setattr(ops, "OpsSession", sm(bind=ops_engine, expire_on_commit=False,
                                              future=True))
    ops.init_ops(force=True)
    monkeypatch.setattr(runner, "_collected", lambda *a: (1, 1, 1))
    ok = [sys.executable, "-c", "pass"]
    steps = [runner.Step("перший", ok, 30), runner.Step("дублі", ok, 30),
             runner.Step("третій", ok, 30)]
    res = runner.run_cycle(steps=steps, lock_path=tmp_path / "cycle.lock",
                           disabled_flag=tmp_path / "off")
    assert [r.status for r in res.steps] == ["ok", "ok", "ok"]
    assert webcache.read() == {"lists": 3, "analytics": 2}   # «дублі» + кінець циклу


def test_any_cli_command_that_writes_bumps_the_lists_generation(tmp_path):
    """`cli.py dedup` на тимчасовій базі: записала — покоління «lists» +1 (і «analytics»)."""
    import os
    import subprocess

    db = tmp_path / "cli.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        s.add_all([_l(1, None), _l(2, None, source="olx", external_id="2b")])
        s.commit()
    engine.dispose()
    env = {**os.environ, "DB_URL": f"sqlite:///{db}",
           "OPS_DB_URL": f"sqlite:///{tmp_path / 'ops.db'}"}

    def gens():
        con = __import__("sqlite3").connect(tmp_path / "ops.db")
        try:
            return dict(con.execute("SELECT name, gen FROM web_generations").fetchall())
        except Exception:                                   # noqa: BLE001 — таблиці ще немає
            return {}
        finally:
            con.close()

    r = subprocess.run([sys.executable, str(ROOT / "cli.py"), "stats"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    assert gens().get("lists", 0) == 0                       # лише читала — не збільшує
    r = subprocess.run([sys.executable, str(ROOT / "cli.py"), "dedup"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    assert gens() == {"lists": 1, "analytics": 1}


def test_a_key_computed_while_the_commit_is_in_flight_is_superseded(site):
    """Подія Engine «commit» спрацьовує ДО коміту в SQLite: запит у цьому вікні
    бачить старі дані вже під новою версією. Друге збільшення версії — після
    коміту — робить такий ключ застарілим (рецензія: інакше старі id жили б у
    кеші до наступного покоління, до 15 хв)."""
    c, Session, engine = site
    txnwatch = _mod("realty.txnwatch")
    before = _matched(c.get("/?rooms=2").text)
    in_window, release = threading.Event(), threading.Event()
    seen = {}

    def hold(conn):
        if not in_window.is_set():
            seen["version"] = txnwatch.data_version(engine)
            in_window.set()
            release.wait(30)

    def write():
        with Session() as s:
            s.add(_l(500, None))
            s.commit()

    v0 = txnwatch.data_version(engine)
    event.listen(engine, "commit", hold)
    try:
        t = threading.Thread(target=write)
        t.start()
        assert in_window.wait(15)
        assert seen["version"] > v0                       # вікно: версія вже нова, коміту ще немає
        assert _matched(c.get("/?rooms=2").text) == before  # читає знімок ДО коміту
        release.set()
        t.join(30)
    finally:
        release.set()
        event.remove(engine, "commit", hold)
    assert _matched(c.get("/?rooms=2").text) == before + 1


def test_full_scrape_script_bumps_the_lists_generation_after_each_source(site, monkeypatch,
                                                                          tmp_path):
    """scripts/full_scrape.py пише в realty.db поза cli.main — і теж каже сайту
    (рецензія: досі кеш списків сайту не бачив повного збору до 15 хв)."""
    import importlib.util
    import signal

    import realty.db as dbmod

    c, Session, engine = site
    txnwatch, webcache = _mod("realty.txnwatch"), _mod("realty.webcache")
    spec = importlib.util.spec_from_file_location("full_scrape_under_test",
                                                  ROOT / "scripts" / "full_scrape.py")
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)

    class Pipe:
        made = 0

        def __init__(self, sources, **kw):
            self.source = sources[0]

        def run(self):
            Pipe.made += 1
            with Session() as s:                          # «зібрано» одне оголошення
                s.add(_l(600 + Pipe.made, None, source=self.source,
                         external_id=f"fs-{self.source}"))
                s.commit()
            return SimpleNamespace(render=lambda: "")

    monkeypatch.setattr(fs, "Pipeline", Pipe)
    monkeypatch.setattr(fs, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(txnwatch, "_autobump", None)
    monkeypatch.setattr(txnwatch.atexit, "register", lambda fn: None)
    monkeypatch.setattr(sys, "argv", ["full_scrape.py", "--sources", "olx,lun", "--no-llm"])
    gen0 = webcache.read()["lists"]
    assert fs.main() == 0
    assert webcache.read()["lists"] == gen0 + 2           # по одному на джерело


def test_dml_tracker_counts_committed_writes_only(tmp_path):
    txnwatch = _mod("realty.txnwatch")
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
    v0 = txnwatch.data_version(engine)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1")).all()
        conn.commit()
    assert txnwatch.data_version(engine) == v0               # читання — не зміна
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO t VALUES (1)"))
        conn.rollback()
    assert txnwatch.data_version(engine) == v0               # відкат — не зміна
    with engine.connect() as conn:
        conn = conn.execution_options(**{txnwatch.MINOR: True})
        conn.execute(text("INSERT INTO t VALUES (2)"))
        conn.commit()
    assert txnwatch.data_version(engine) == v0               # перегляди — не зміна списку
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO t VALUES (3)"))
    # Двічі: у подію «commit» (ще ДО коміту в SQLite) і після нього — див.
    # test_a_key_computed_while_the_commit_is_in_flight_is_superseded.
    assert txnwatch.data_version(engine) > v0
