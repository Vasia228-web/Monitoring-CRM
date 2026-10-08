"""Хто куди має доступ: кожен маршрут сайту класифікований явно.

Навіщо. Блоки промту 11 додають нові сторінки й API (/api/status/speed,
/api/rum, /find, панелі на /status …). Доступ визначає префікс у
`auth.OWNER_ONLY`, і новий маршрут, що випадково не потрапив під префікс,
непомітно відкрився б другові — або, навпаки, сторінка для обох ролей сховалась
би за власником. Тому тут одна таблиця POLICY: маршрут без класу валить тест,
клас «лише власник» мусить збігатися з префіксами auth.py, а поведінку
перевірено справжніми входами (власник і друг) через TestClient.

Класи:
  public — без входу (auth.OPEN_PATHS);
  friend — будь-хто, хто ввійшов: друг і власник;
  owner  — лише власник (auth.OWNER_ONLY); друг отримує 403.

Два доступи друга, що були тут записані як є (D48: документація API показувала й
маршрути власника, а POST /api/listings/{id}/status давав другові вручну позначити
актуальність), закрито рішенням власника 08.10 (D55 п. 5; хвиля W3, D58): /openapi.json,
/api/docs (разом з /api/docs/oauth2-redirect), /redoc — префікси auth.OWNER_ONLY;
ручна позначка — вираз auth.OWNER_ONLY_RE усередині спільного /api/listings.
Клас «лише власник» тепер — `auth.owner_only(шлях)`: префікс або вираз.
"""
from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from starlette.routing import Mount, WebSocketRoute

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from realty import ops  # noqa: E402
from realty.web import auth, sessions  # noqa: E402
from realty.web.app import app  # noqa: E402

PUBLIC, FRIEND, OWNER = "public", "friend", "owner"

# Одна таблиця на весь сайт: (метод, шлях) → хто має доступ.
POLICY: dict[tuple[str, str], str] = {
    # --- без входу ---
    ("GET", "/healthz"): PUBLIC,
    ("GET", "/robots.txt"): PUBLIC,
    ("GET", "/favicon.ico"): PUBLIC,
    ("GET", "/login"): PUBLIC,
    ("POST", "/login"): PUBLIC,
    # --- друг і власник ---
    ("POST", "/logout"): FRIEND,
    ("GET", "/"): FRIEND,
    ("GET", "/processing"): FRIEND,
    ("GET", "/analytics"): FRIEND,
    ("GET", "/property/{property_id}"): FRIEND,
    ("GET", "/api/listings"): FRIEND,
    ("GET", "/api/properties"): FRIEND,
    ("GET", "/api/properties/{property_id}/prices"): FRIEND,
    ("GET", "/api/stats"): FRIEND,
    ("GET", "/api/analytics/segments"): FRIEND,
    ("GET", "/api/analytics/property/{property_id}"): FRIEND,
    # «в обробці» — спільний список (D38); скарга «дані не збігаються».
    ("POST", "/api/listings/{listing_id}/processing"): FRIEND,
    ("POST", "/api/properties/{property_id}/processing"): FRIEND,
    ("POST", "/api/listings/{listing_id}/report"): FRIEND,
    # Маячок часу переходу (Блок 2, D49): обидві ролі; вхід і same-origin — як
    # для будь-якого POST.
    ("POST", "/api/rum"): FRIEND,
    # Стан перевірки при відкритті квартири (Блок 2, крок E5, D50): банер на
    # сторінці квартири, яку бачать обидві ролі. Лише читання ops.db.
    ("GET", "/api/property/{property_id}/liveness"): FRIEND,
    # «Райони й ЖК» (Блок 4, E10, D57): розподіл за районами й ЖК — обидві ролі.
    ("GET", "/places"): FRIEND,
    # --- лише власник ---
    # Документація API показує й маршрути власника; ручне «активне/неактивне» — лише
    # власнику (рішення власника 08.10, D55 п. 5; D58).
    ("GET", "/openapi.json"): OWNER,
    ("GET", "/api/docs"): OWNER,
    ("GET", "/api/docs/oauth2-redirect"): OWNER,
    ("GET", "/redoc"): OWNER,
    ("POST", "/api/listings/{listing_id}/status"): OWNER,
    ("GET", "/status"): OWNER,
    ("GET", "/api/status"): OWNER,
    ("GET", "/api/status/reports"): OWNER,
    ("GET", "/api/status/dedup"): OWNER,
    ("GET", "/api/status/runs"): OWNER,
    ("GET", "/api/status/speed"): OWNER,                    # зведення «Швидкість» (D49)
    ("GET", "/api/status/liveness"): OWNER,                 # «Зняті оголошення» (E8, D52)
    ("POST", "/api/status/liveness-fuse"): OWNER,           # зняти запобіжник (E8, D52)
    ("GET", "/api/status/places"): OWNER,                   # «Райони й ЖК» (E10, D57)
    ("POST", "/api/status/run"): OWNER,                     # запуск збору
    ("GET", "/api/auth/blocks"): OWNER,
    ("POST", "/api/auth/blocks/{ip}/unblock"): OWNER,
    ("POST", "/api/dedup/split"): OWNER,
    ("POST", "/api/dedup/merge"): OWNER,
    ("POST", "/api/dedup/decisions/{decision_id}/undo"): OWNER,
}

OWNER_U, OWNER_PW = "vasia", "пароль власника 1"
FRIEND_U, FRIEND_PW = "druh", "druh-pass-2"
SITE = "https://mojkvartiry.test"
SAMPLE_PARAMS = {"property_id": "1", "listing_id": "1", "decision_id": "1",
                 "ip": "203.0.113.250"}


# Позначки «методу» для маршрутів, яких AuthMiddleware не бачить: класифікувати
# їх нема як — їх не має бути (test_no_websocket_or_mount_routes).
WS, MOUNT, UNKNOWN = "WS", "MOUNT", "?"


def site_routes(application=None) -> set[tuple[str, str]]:
    """(метод, шлях) усіх маршрутів, включно з тими, що в підключених роутерах.

    FastAPI 0.14x не розгортає `include_router` у плаский список: у
    `app.routes` лежить обгортка, і маршрути статусу, аналітики, входу були б
    просто невидимі для перевірки, яка дивиться лише на верхній рівень.

    Маршрути без HTTP-методів не відкидаються: WebSocket (і вгорі, і в
    підключеному роутері) — як ("WS", шлях), змонтований застосунок — як
    ("MOUNT", шлях), будь-що інше без методів — ("?", шлях). Інакше новий
    WebSocket під префіксом власника тихо пройшов би перевірку класів.
    """
    out: set[tuple[str, str]] = set()

    def add(route, path):
        if isinstance(route, (WebSocketRoute, APIWebSocketRoute)):
            out.add((WS, path))
            return
        if isinstance(route, Mount):
            out.add((MOUNT, path))
            return
        methods = getattr(route, "methods", None)
        if not methods:
            out.add((UNKNOWN, path))
            return
        for m in methods:
            if m == "HEAD" and "GET" in methods:  # HEAD Starlette додає сам до GET
                continue
            out.add((m, path))

    for r in (application or app).routes:
        if hasattr(r, "effective_route_contexts"):
            for ctx in r.effective_route_contexts():
                path = ctx.path or getattr(ctx.starlette_route, "path", None) \
                    or getattr(ctx.original_route, "path", "")
                add(ctx.original_route, path)
        else:
            add(r, getattr(r, "path", ""))
    return out


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: SAMPLE_PARAMS[m.group(1)], path)


# --- Таблиця повна й узгоджена з auth.py ---------------------------------------------------


def test_every_route_is_classified():
    unclassified = sorted(site_routes() - POLICY.keys())
    assert not unclassified, ("Маршрути без класу доступу — додайте їх у POLICY "
                              f"(і, якщо треба, у auth.OWNER_ONLY): {unclassified}")


def test_no_websocket_or_mount_routes():
    odd = sorted(k for k in site_routes() if k[0] in (WS, MOUNT, UNKNOWN))
    assert not odd, (
        "AuthMiddleware (BaseHTTPMiddleware) не бачить websocket і змонтованих "
        "застосунків — такий маршрут був би відкритий без входу. Спершу явна "
        f"перевірка входу в самому обробнику, потім клас тут: {odd}")


def test_route_listing_sees_websockets_mounts_and_head_only_routes():
    """Сама перевірка не має бути сліпою: на штучному застосунку з усіма видами
    маршрутів (і вгорі, і в підключеному роутері) жоден не губиться."""
    from fastapi import APIRouter, FastAPI
    from starlette.responses import PlainTextResponse

    probe, sub = FastAPI(), APIRouter()

    @probe.websocket("/api/status/ws")
    async def ws_top(websocket):                          # pragma: no cover
        pass

    @sub.websocket("/live")
    async def ws_nested(websocket):                       # pragma: no cover
        pass

    @sub.get("/page")
    def page():                                           # pragma: no cover
        return {}

    probe.include_router(sub, prefix="/api/x")
    probe.mount("/static", PlainTextResponse("x"))
    probe.add_api_route("/head-only", lambda: None, methods=["HEAD"])
    found = site_routes(probe)
    assert {(WS, "/api/status/ws"), (WS, "/api/x/live"), ("GET", "/api/x/page"),
            (MOUNT, "/static"), ("HEAD", "/head-only")} <= found, found
    assert ("HEAD", "/api/x/page") not in found


def test_policy_has_no_stale_entries():
    stale = sorted(POLICY.keys() - site_routes())
    assert not stale, f"У POLICY є маршрути, яких на сайті вже немає: {stale}"


def test_owner_only_class_matches_auth_prefixes():
    """Клас «лише власник» ⇔ auth.owner_only(шлях) — і для шаблону маршруту, і для
    конкретної адреси; «без входу» ⇔ OPEN_PATHS."""
    for (method, path), level in POLICY.items():
        for p in (path, concrete(path)):
            owner = auth.owner_only(p)
            assert owner == (level == OWNER), \
                f"{method} {p}: клас {level}, а auth.owner_only — {owner}"
        assert (path in auth.OPEN_PATHS) == (level == PUBLIC), \
            f"{method} {path}: клас {level}, а OPEN_PATHS — {path in auth.OPEN_PATHS}"


def test_every_owner_prefix_still_protects_something():
    for prefix in auth.OWNER_ONLY:
        assert any(p.startswith(prefix) for _, p in POLICY), f"префікс {prefix} нічого не закриває"
    for rx in auth.OWNER_ONLY_RE:
        assert any(rx.fullmatch(p) for _, p in POLICY), f"вираз {rx.pattern} нічого не закриває"


def test_owner_only_pattern_does_not_close_shared_listing_routes():
    """Вираз ручної позначки не зачіпає сусідніх спільних маршрутів /api/listings/…"""
    for path in ("/api/listings", "/api/listings/1/processing", "/api/listings/1/report",
                 "/api/listings/1/statusx", "/api/listings/1/status/extra"):
        assert not auth.owner_only(path), path
    for path in ("/api/listings/1/status", "/api/listings/1/status/",
                 "/api/docs/oauth2-redirect", "/openapi.json", "/redoc"):
        assert auth.owner_only(path), path


# --- Поведінка зі справжніми входами ------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Окрема ops.db (сесії, блокування, телеметрія) і два облікові записи."""
    from realty import backup, dedup_audit, dedup_sample  # noqa: F401 — таблиці ops

    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(engine)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER_U)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    monkeypatch.setenv("FRIEND_USER", FRIEND_U)
    monkeypatch.setenv("FRIEND_PASSWORD", FRIEND_PW)
    return tmp_path


@pytest.fixture
def no_side_effects(monkeypatch):
    """Якщо політика колись зламається й друг пройде далі за 403, обробник
    упреться сюди, а не запустить збір чи перезведе квартири.

    Повертає «запобіжник», який вмикають ПІСЛЯ входу: успішний вхід сам
    знімає блок зі своєї адреси через `sessions.unblock`.
    """
    from realty.web import dedup_routes, status as status_mod

    def refuse(*a, **k):
        raise AssertionError("обробник лише-для-власника виконався для друга")

    # Запуск збору: _alive — перше, що робить api_run, ще ДО того, як дописати
    # рядок у logs/manual-*.log справжнього репозиторію; Popen — про всяк випадок.
    monkeypatch.setattr(status_mod, "_alive", refuse)
    monkeypatch.setattr(status_mod.subprocess, "Popen", refuse)
    # Розділити / злити / скасувати рішення — усі три відкривають session_scope
    # на спільній копії бази тестів.
    monkeypatch.setattr(dedup_routes, "session_scope", refuse)

    def arm():
        monkeypatch.setattr(sessions, "unblock", refuse)
    return arm


def client() -> TestClient:
    return TestClient(app, base_url=SITE, client=("127.0.0.1", 50000), follow_redirects=False)


def logged_in(user: str, pw: str, ip: str) -> TestClient:
    c = client()
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"},
               headers={"CF-Connecting-IP": ip, "Accept": "text/html"})
    assert r.status_code == 303 and auth.COOKIE in r.cookies, r.text[:200]
    return c


def routes_of(level: str, method: str | None = None) -> list[tuple[str, str]]:
    return sorted(k for k, v in POLICY.items() if v == level and (method is None or k[0] == method))


def _get(c: TestClient, path: str, html: bool = False):
    url = concrete(path)
    if path.startswith("/property/"):
        url += "?verify=0"            # без цього сторінка перевіряла б джерело просто зараз
    return c.get(url, headers={"Accept": "text/html"} if html else {})


def friend_client(via: str, ip: str, arm) -> TestClient:
    """Друг, що ввійшов: формою (кука) або заголовком Basic, як скрипт.

    У AuthMiddleware це дві окремі гілки, і роль у них ставиться окремо.
    Запобіжник `arm` — лише для куки: вхід заголовком на КОЖНОМУ запиті
    викликає sessions.register_success → sessions.unblock.
    """
    if via == "cookie":
        c = logged_in(FRIEND_U, FRIEND_PW, ip)
        arm()
        return c
    token = base64.b64encode(f"{FRIEND_U}:{FRIEND_PW}".encode()).decode()
    # Без Sec-Fetch-* і без text/html: інакше auth.from_browser() сприйме запит
    # як браузерний і заголовок проігнорує.
    c = client()
    c.headers.update({"Authorization": f"Basic {token}", "CF-Connecting-IP": ip})
    return c


# Заголовок Basic браузерові не зараховується (auth.from_browser) — тому пари
# «basic + браузер» немає.
@pytest.mark.parametrize("via,html", [("cookie", False), ("cookie", True), ("basic", False)],
                         ids=["cookie-api", "cookie-browser", "basic-api"])
def test_friend_gets_403_on_every_owner_only_get(env, no_side_effects, html, via):
    c = friend_client(via, "203.0.113.41", no_side_effects)
    for _, path in routes_of(OWNER, "GET"):
        r = _get(c, path, html=html)
        assert r.status_code == 403, f"друг ({via}): GET {path} → {r.status_code}"


@pytest.mark.parametrize("via", ["cookie", "basic"])
def test_friend_gets_403_on_every_owner_only_post(env, no_side_effects, via):
    c = friend_client(via, "203.0.113.42", no_side_effects)
    for _, path in routes_of(OWNER, "POST"):
        r = c.post(concrete(path), json={"source": "olx", "property_id": 1,
                                         "listing_ids": [1], "other": "2"},
                   headers={"Origin": SITE})
        assert r.status_code == 403, f"друг ({via}): POST {path} → {r.status_code}"


def test_owner_opens_every_owner_only_get(env):
    c = logged_in(OWNER_U, OWNER_PW, "203.0.113.43")
    for _, path in routes_of(OWNER, "GET"):
        r = _get(c, path)
        assert r.status_code == 200, f"власник: GET {path} → {r.status_code}"


def test_friend_opens_every_shared_get(env):
    c = logged_in(FRIEND_U, FRIEND_PW, "203.0.113.44")
    for _, path in routes_of(FRIEND, "GET"):
        r = _get(c, path)
        # 302 — стара квартира переадресована, 404 — квартири 1 у копії може не бути;
        # головне — не 401/403.
        assert r.status_code in (200, 302, 404), f"друг: GET {path} → {r.status_code}"


def test_friend_pages_hide_owner_controls(env):
    """Коди відповіді — не все: кнопки розділити/злити й посилання «Стан системи»
    на спільних сторінках бачить лише власник. Обидві ролі перевірені, щоб тест
    не проходив порожньо (наприклад, якщо квартири без кнопок)."""
    from realty.db import SessionLocal
    from realty.models import Listing

    with SessionLocal() as s:
        pid = s.scalar(select(Listing.property_id).where(Listing.property_id.is_not(None))
                       .group_by(Listing.property_id).having(func.count() > 1)
                       .order_by(Listing.property_id).limit(1))
    if pid is None:
        pytest.skip("у копії бази немає квартири з кількома оголошеннями")
    friend = logged_in(FRIEND_U, FRIEND_PW, "203.0.113.47")
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.48")
    page = f"/property/{pid}?verify=0"
    nav = ">Стан системи</a>"
    assert "/api/dedup" in owner.get(page).text
    assert nav in owner.get("/").text
    friend_page = friend.get(page)
    assert friend_page.status_code == 200
    assert "/api/dedup" not in friend_page.text
    assert nav not in friend.get("/").text


def test_public_routes_work_without_login(env):
    c = client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/robots.txt").text.startswith("User-agent: *")
    assert c.get("/favicon.ico").status_code == 204
    page = c.get("/login", headers={"Accept": "text/html"})
    assert page.status_code == 200 and 'name="password"' in page.text
    wrong = c.post("/login", data={"username": OWNER_U, "password": "не той", "next": "/"},
                   headers={"CF-Connecting-IP": "203.0.113.45"})
    assert wrong.status_code == 401 and "Невірний логін або пароль" in wrong.text
    # Кожен маршрут класу public (і доданий згодом теж): проміжний шар його не
    # заступає — ні 403, ні переадресації на форму входу.
    for method, path in routes_of(PUBLIC):
        r = c.request(method, path, data={"username": "x", "password": "y"}
                      if method == "POST" else None,
                      headers={"CF-Connecting-IP": "203.0.113.46", "Accept": "text/html"})
        to_login = r.status_code == 303 and \
            r.headers.get("location", "").startswith("/login?next=")
        assert r.status_code != 403 and not to_login, f"{method} {path} → {r.status_code}"


def test_everything_else_needs_login(env, no_side_effects):
    no_side_effects()
    c = client()
    for method, path in sorted(k for k, v in POLICY.items() if v != PUBLIC):
        url = concrete(path)
        browser = c.request(method, url, headers={"Accept": "text/html"})
        script = c.request(method, url, headers={"Accept": "application/json"})
        if not path.startswith("/api/"):
            assert browser.status_code == 303 and \
                browser.headers["location"].startswith("/login"), f"{method} {path}"
        assert script.status_code == 401, f"{method} {path} → {script.status_code}"


# --- Закриті доступи друга (рішення власника 08.10, D55 п. 5; хвиля W3, D58) ---------------

DOCS = ("/openapi.json", "/api/docs", "/api/docs/oauth2-redirect", "/redoc")


def _first_listing() -> tuple[int, bool | None]:
    from realty.db import SessionLocal
    from realty.models import Listing

    with SessionLocal() as s:
        row = s.execute(select(Listing.id, Listing.manual_active)
                        .order_by(Listing.id).limit(1)).first()
    if row is None:
        pytest.skip("у копії бази немає оголошень")
    return int(row[0]), row[1]


def _manual_active(lid: int):
    from realty.db import SessionLocal
    from realty.models import Listing

    with SessionLocal() as s:
        return s.get(Listing, lid).manual_active


@pytest.mark.parametrize("via", ["cookie", "basic"])
def test_friend_gets_403_on_api_docs_and_manual_status(env, no_side_effects, via):
    """До D58 друг відкривав документацію API (з маршрутами власника) і міг вручну
    позначити оголошення неактуальним — тепер 403, і рядок у базі не змінюється."""
    lid, before = _first_listing()
    c = friend_client(via, "203.0.113.51" if via == "cookie" else "203.0.113.52",
                      no_side_effects)
    for path in DOCS:
        r = c.get(path)
        assert r.status_code == 403, f"друг ({via}): GET {path} → {r.status_code}"
    r = c.post(f"/api/listings/{lid}/status", json={"active": not before},
               headers={"Origin": SITE})
    assert r.status_code == 403, r.status_code
    assert _manual_active(lid) == before


def test_owner_still_opens_api_docs_and_marks_status(env):
    c = logged_in(OWNER_U, OWNER_PW, "203.0.113.53")
    for path in DOCS:
        r = c.get(path)
        assert r.status_code == 200, f"власник: GET {path} → {r.status_code}"
    assert "/api/listings/{listing_id}/status" in c.get("/openapi.json").json()["paths"]
    # Swagger UI посилається на oauth2-redirect під тим самим префіксом.
    assert "/api/docs/oauth2-redirect" in c.get("/api/docs").text
    lid, before = _first_listing()
    # Те саме значення — маршрут пройдено власником, а спільна копія бази не змінилась.
    r = c.post(f"/api/listings/{lid}/status", json={"active": before}, headers={"Origin": SITE})
    assert r.status_code == 200 and r.json()["manual_active"] == before, r.text[:200]
    assert _manual_active(lid) == before
