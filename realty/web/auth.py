"""Вхід до інтерфейсу: сторінка з формою, сесії, ролі, ліміт спроб.

Раніше тут була базова HTTP-автентифікація — браузерне віконце. Воно
поводилось по-різному скрізь: на iPhone клавіатура дописувала пробіл, і вхід
«не приймав» правильний пароль; відкрите віконце крутило сотні повторних
запитів; іконка сайту вимагала пароль і знову відкривала діалог; вийти було
неможливо. Тепер — звичайна сторінка `/login` і сесія на сервері.

Хто є хто (усе лише з `.env`):
  * AUTH_USER / AUTH_PASSWORD   — власник: усе, включно з /status, керуванням
    блокуваннями й запуском збору;
  * FRIEND_USER / FRIEND_PASSWORD — друг: перегляд, аналітика, «в обробці»
    (список спільний), без /status.
Якщо AUTH_* не задані — інтерфейс відкритий (режим розробки, з попередженням).

Ліміт спроб — за СПРАВЖНЬОЮ адресою відвідувача з `CF-Connecting-IP`. Цьому
заголовку віримо лише тоді, коли запит прийшов від тунелю, тобто з локальної
машини: інакше будь-хто підставив би чужу адресу. Ліміт діє і на форму, і на
вхід через заголовок Authorization для скриптів.
"""
from __future__ import annotations

import base64
import hmac
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from . import sessions

log = logging.getLogger(__name__)

ROLE_OWNER, ROLE_FRIEND = "owner", "friend"
COOKIE = "realty_session"
# Шляхи без входу: перевірка живучості, robots, іконка (браузер просить її без
# пароля), сама сторінка входу.
OPEN_PATHS = {"/healthz", "/robots.txt", "/favicon.ico", "/login"}
# Лише для власника: стан системи, запуск збору, керування блокуваннями; документація
# API (вона показує й маршрути власника — рішення власника 08.10, D55 п. 5; хвиля W3,
# D58). Swagger UI — під /api/docs разом зі своїм oauth2-redirect (app.py).
OWNER_ONLY = ("/status", "/api/status", "/api/auth", "/api/dedup",
              "/openapi.json", "/api/docs", "/redoc")
# Лише для власника всередині СПІЛЬНИХ префіксів: ручне «активне/неактивне»
# (POST /api/listings/{id}/status) живе під /api/listings, решту якого бачить і друг
# (D55 п. 5). Шлях — уже розкодований (scope["path"]), як і для маршрутизатора.
OWNER_ONLY_RE = (re.compile(r"/api/listings/[^/]+/status/?"),)


def owner_only(path: str) -> bool:
    """Шлях лише для власника: префікс OWNER_ONLY або ВЕСЬ шлях збігається з OWNER_ONLY_RE."""
    return path.startswith(OWNER_ONLY) or any(rx.fullmatch(path) for rx in OWNER_ONLY_RE)


# Звідки дозволено вірити заголовку CF-Connecting-IP: лише локальний тунель.
TRUSTED_PROXIES = {"127.0.0.1", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).parent
                                          / "templates"))
router = APIRouter()


# --- Облікові записи ---------------------------------------------------------------------


def accounts() -> dict[str, tuple[str, str]]:
    """{логін: (пароль, роль)}. Порожньо — вхід вимкнено (режим розробки)."""
    owner_u, owner_p = os.getenv("AUTH_USER", "").strip(), os.getenv("AUTH_PASSWORD", "").strip()
    if not (owner_u and owner_p):
        return {}
    out = {owner_u: (owner_p, ROLE_OWNER)}
    friend_u = os.getenv("FRIEND_USER", "").strip()
    friend_p = os.getenv("FRIEND_PASSWORD", "").strip()
    if friend_u and friend_p:
        if friend_u == owner_u:
            log.error("FRIEND_USER збігається з AUTH_USER — вхід друга вимкнено")
        else:
            out[friend_u] = (friend_p, ROLE_FRIEND)
    return out


def credentials() -> tuple[str, str] | None:
    """Сумісність: логін і пароль власника або None."""
    u, p = os.getenv("AUTH_USER", "").strip(), os.getenv("AUTH_PASSWORD", "").strip()
    return (u, p) if u and p else None


def check(user: str, password: str, accts: dict) -> str | None:
    """Роль, якщо логін і пароль правильні. Логін обрізаємо (клавіатура телефона
    дописує пробіл), пароль — ніколи: у ньому пробіл може бути навмисним."""
    user = user.strip()
    ok_role = None
    for name, (pw, role) in accts.items():
        # Порівнюємо всіх і байтами: сталий час і підтримка кирилиці.
        if hmac.compare_digest(user.encode(), name.encode()) & \
                hmac.compare_digest(password.encode(), pw.encode()):
            ok_role = role
    return ok_role


# --- Хто прийшов ---------------------------------------------------------------------------


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    forwarded = request.headers.get("cf-connecting-ip", "").strip()
    if peer in TRUSTED_PROXIES and forwarded:
        return forwarded
    return peer


def _country(request: Request) -> str | None:
    return request.headers.get("cf-ipcountry") or None


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") and \
        not request.url.path.startswith("/api/")


def from_browser(request: Request) -> bool:
    """Запит від браузера, а не від скрипта.

    Усі сучасні браузери додають до КОЖНОГО запиту заголовки `Sec-Fetch-*`
    (і до сторінок, і до фонових fetch); curl і скрипти — ні. Для старих
    браузерів — запасна ознака: вони просять HTML.
    """
    return any(h in request.headers for h in ("sec-fetch-mode", "sec-fetch-site",
                                              "sec-fetch-dest")) or _wants_html(request)


def same_origin(request: Request) -> bool:
    """Запит, що змінює дані, прийшов із самого сайту, а не з чужої сторінки."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source or source == "null":
        return False
    host = urlsplit(source).netloc.lower()
    allowed = {h.lower() for h in (request.headers.get("host"),
                                   request.headers.get("x-forwarded-host"),
                                   os.getenv("PUBLIC_DOMAIN", "").strip()
                                   .removeprefix("https://").strip("/")) if h}
    return host in allowed


def _safe_next(target: str | None) -> str:
    """Куди повернутись після входу — лише шлях на цьому ж сайті."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


# --- Відповіді ---------------------------------------------------------------------------


def _hhmm(dt: datetime | None) -> str:
    """Час із бази (UTC без зони) — місцевим часом машини."""
    if dt is None:
        return "—"
    offset = datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)
    return (dt + offset).strftime("%H:%M")


def _unauthorized(request: Request) -> Response:
    if _wants_html(request):
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
    # Без WWW-Authenticate: інакше браузер знову показав би своє віконце.
    return PlainTextResponse("Потрібна авторизація", status_code=401)


def _blocked_response(request: Request, row) -> Response:
    text = (f"Забагато невдалих спроб входу. Вхід з вашої адреси заблоковано до "
            f"{_hhmm(row.blocked_until)}.")
    if _wants_html(request) or request.url.path == "/login":
        return templates.TemplateResponse(request, "login.html", {
            "error": text, "blocked": True, "next": "/", "user": ""}, status_code=429)
    return PlainTextResponse(text, status_code=429)


def _forbidden(request: Request, why: str) -> Response:
    if _wants_html(request):
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'><title>Недоступно</title>"
            f"<body style='font-family:system-ui;padding:40px'><h2>Недоступно</h2>"
            f"<p>{why}</p><p><a href='/'>На головну</a></p>", status_code=403)
    return JSONResponse({"ok": False, "error": why}, status_code=403)


def _fail(request: Request, user: str) -> None:
    sessions.register_failure(client_ip(request), _country(request),
                              request.headers.get("user-agent"), user)


# --- Проміжний шар -----------------------------------------------------------------------


@dataclass
class _Who:
    """Що вирішила перевірка входу (у потоці, поза event loop)."""

    user: str | None = None
    role: str | None = None
    via: str | None = None
    blocked: object | None = None       # рядок блокування → 429
    wrong: str | None = None            # невдалий вхід заголовком → 401 з цим текстом


def _authenticate(request: Request, accts: dict) -> _Who:
    """Хто прийшов — кука сесії або заголовок Basic для скриптів.

    Виконується в пулі потоків (Блок 2, крок E5, D50): тут читання й записи в
    ops.db (сесії, ліміт спроб), а на event loop вони зупиняли ВСІ запити сайту
    разом, поки ops.db була зайнята (busy_timeout 30 с; план Блоку 2, D48).
    Рішення ті самі, що й досі: ролі, ліміт спроб, блокування — без змін.
    Оновлення last_seen сесії (раз на 5 хв) — фоном (`deferred.session_touch`).
    """
    touch_later = _touch_deferred()
    row = sessions.validate(request.cookies.get(COOKIE), accts, touch=not touch_later)
    if row is not None:
        if touch_later and sessions.needs_touch(row):
            from .perf import WRITER
            WRITER.touch_session(row.sid, ops_now())
        return _Who(row.user, row.role, "cookie")
    header = request.headers.get("authorization", "")
    # Заголовок входу — лише для скриптів. Браузер, що пам'ятає пароль
    # від старого віконця, сам підставляє його до кожного запиту: Firefox
    # показував «Невірний логін або пароль» замість форми й рахував
    # кожне відкриття як невдалу спробу — так можна було заблокувати себе.
    if header.startswith("Basic ") and not from_browser(request):
        # Вхід для скриптів — під тим самим лімітом, що й форма.
        ip = client_ip(request)
        if (b := sessions.blocked(ip)) is not None:
            return _Who(blocked=b)
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raw = ":"
        u, _, p = raw.partition(":")
        role = check(u, p, accts)
        if role is None:
            _fail(request, u.strip())
            _explain_mismatch(u, p, accts)
            b = sessions.blocked(ip)
            note = (f". Забагато невдалих спроб — вхід з цієї адреси заблоковано "
                    f"до {_hhmm(b.blocked_until)}") if b else ""
            return _Who(wrong="Невірний логін або пароль" + note)
        sessions.register_success(ip)
        return _Who(u.strip(), role, "header")
    return _Who()


def _touch_deferred() -> bool:
    from .. import configfiles

    try:
        return configfiles.get("speed").deferred.session_touch
    except configfiles.ConfigError:
        return False                    # як до Блоку 2: оновлення в самій перевірці


def ops_now():
    from .. import ops
    return ops._now()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await self._dispatch(request, call_next)
        # Заборона індексації — у заголовку кожної відповіді, включно з 401.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    async def _dispatch(self, request, call_next):
        accts = accounts()
        path = request.url.path
        if not accts or path in OPEN_PATHS:
            return await call_next(request)

        who = await anyio.to_thread.run_sync(_authenticate, request, accts)
        if who.blocked is not None:
            return _blocked_response(request, who.blocked)
        if who.wrong is not None:
            return PlainTextResponse(who.wrong, status_code=401)
        user, role, via = who.user, who.role, who.via

        if user is None:
            return _unauthorized(request)
        if owner_only(path) and role != ROLE_OWNER:
            return _forbidden(request, "Ця сторінка доступна лише власнику.")
        # Запит, що змінює дані, з куки — лише з самого сайту. Чужа сторінка
        # може змусити браузер надіслати форму, але не підробить Origin.
        # Заголовок Authorization чужий сайт підставити не може — скриптам
        # перевірка не потрібна.
        if via == "cookie" and request.method not in SAFE_METHODS \
                and not same_origin(request):
            return _forbidden(request, "Запит прийшов не з цього сайту — відхилено.")
        request.state.user = user
        request.state.role = role
        # Як саме ввійшли: «cookie» — браузер, «header» — скрипт (зонд швидкості).
        # Журнал часу запити скриптів не пише (perf.py): ~720 запитів зонда за
        # замір змішались би з переглядами власника в p50/p95.
        request.state.via = via
        return await call_next(request)


# Стара назва — щоб не ламати імпорти.
BasicAuthMiddleware = AuthMiddleware


# --- Сторінки входу й виходу -------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str = "/"):
    if not accounts():
        return RedirectResponse("/", status_code=303)
    # Лише перевірка, без запису last_seen: GET нічого не пише (Блок 2, E5).
    if sessions.validate(request.cookies.get(COOKIE), accounts(), touch=False) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": None, "next": _safe_next(next), "user": ""})


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    accts = accounts()
    if not accts:
        return RedirectResponse("/", status_code=303)
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"),
                    keep_blank_values=True)
    user = (form.get("username") or [""])[0]
    password = (form.get("password") or [""])[0]
    nxt = _safe_next((form.get("next") or ["/"])[0])
    ip = client_ip(request)

    if (b := sessions.blocked(ip)) is not None:
        return _blocked_response(request, b)
    role = check(user, password, accts)
    if role is None:
        row = sessions.register_failure(ip, _country(request),
                                        request.headers.get("user-agent"), user.strip())
        _explain_mismatch(user, password, accts)
        if row.blocked_until:
            # Ця (10-та) спроба ще перевірена — і невдала; наступна вже ні.
            return templates.TemplateResponse(request, "login.html", {
                "error": f"Невірний логін або пароль. Забагато невдалих спроб — вхід з "
                         f"цієї адреси заблоковано до {_hhmm(row.blocked_until)}.",
                "blocked": True, "next": nxt, "user": user.strip()}, status_code=401)
        left = sessions.MAX_FAILURES - row.failures
        hint = (f" Ще {left} спроб, потім вхід з цієї адреси заблокується на "
                f"{sessions.BLOCK_MINUTES} хвилин." if left <= 3 else "")
        return templates.TemplateResponse(request, "login.html", {
            "error": "Невірний логін або пароль." + hint, "next": nxt,
            "user": user.strip()}, status_code=401)

    sessions.register_success(ip)
    token = sessions.create(user.strip(), role, accts[user.strip()][0], ip,
                            request.headers.get("user-agent"))
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, token, max_age=sessions.SESSION_DAYS * 86400,
                    httponly=True, secure=True, samesite="lax", path="/")
    log.info("вхід: %s (%s) з %s", user.strip(), role, ip)
    return resp


@router.post("/logout", include_in_schema=False)
def logout(request: Request):
    sessions.destroy(request.cookies.get(COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp


# --- Службове ----------------------------------------------------------------------------


def _explain_mismatch(user: str, password: str, accts) -> None:
    """Каже, ЩО не зійшлося, не кажучи, які саме значення (лише для власника,
    щоб не підказувати, чи існує логін друга)."""
    if isinstance(accts, tuple):                       # стара сигнатура (логін, пароль)
        want_user, want_password = accts
    else:
        owner = next(((u, p) for u, (p, r) in accts.items() if r == ROLE_OWNER), ("", ""))
        want_user, want_password = owner

    def compare(got: str, want: str) -> str:
        if got == want:
            return "збігається"
        if not got:
            return "порожнє"
        if got.strip() == want:
            return "зайві пробіли по краях"
        if got.lower() == want.lower():
            return "інший регістр (на телефоні перша літера часто велика)"
        if len(got) != len(want):
            return f"інша довжина: {len(got)} замість {len(want)}"
        return "не збігається"

    log.warning("невдалий вхід: логін — %s; пароль — %s",
                compare(user, want_user), compare(password, want_password))


def robots_txt() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def warn_if_open() -> None:
    if credentials() is None:
        log.warning("AUTH_USER/AUTH_PASSWORD не задані — інтерфейс відкритий. "
                    "Для публічного хостингу це обов'язково.")


def current_role(request: Request) -> str | None:
    """Для шаблонів: роль того, хто дивиться (None — вхід вимкнено)."""
    return getattr(request.state, "role", None)


# --- Керування блокуваннями (лише власник: префікс /api/auth) ----------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


@router.get("/api/auth/blocks", include_in_schema=False)
def api_blocks():
    out = []
    for b in sessions.active_blocks():
        device, browser = sessions.describe_agent(b.user_agent)
        out.append({"ip": b.ip, "country": b.country or "—", "device": device,
                    "browser": browser, "blocked_at": _iso(b.blocked_at),
                    "failures": b.failures, "blocked_until": _iso(b.blocked_until),
                    "last_user": b.last_user})
    return JSONResponse({"blocks": out, "max_failures": sessions.MAX_FAILURES,
                         "block_minutes": sessions.BLOCK_MINUTES})


@router.post("/api/auth/blocks/{ip}/unblock", include_in_schema=False)
def api_unblock(ip: str, request: Request):
    ok = sessions.unblock(ip)
    log.warning("розблоковано вручну: %s (%s)", ip, getattr(request.state, "user", "?"))
    return JSONResponse({"ok": ok, "ip": ip}, status_code=200 if ok else 404)
