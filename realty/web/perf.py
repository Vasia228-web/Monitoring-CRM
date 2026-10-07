"""Вимірювання швидкості сайту (Блок 2, крок E2): Server-Timing, журнал часу, маячок.

Навіщо. Власник поставив ціль — сервер відповідає на основні сторінки ≤300 мс у
95% запитів, а перехід між вкладками з телефона ≤1,5 с (D46 п. 6), і в спокої, і
під час циклу збору. Щоб лікувати причини, а не вгадувати, потрібні числа з
робочого сайту, причому без зайвих процесів — заміри Етапу 0 самі навантажили
слабкий ноутбук (D45, інцидент 2). Тому:

  * відповідь тим, хто ввійшов, несе заголовок `Server-Timing: app;dur=…` —
    час сервера до початку відповіді; браузер бачить його поруч зі своїм часом
    і відділяє сервер від мережі. Якщо в цьому ж запиті перебудовувався знімок
    «Аналітики», додається `snapshot;dur=…` — «холодний» запит видно окремо.
    Незнайомцям з інтернету (401, сторінка входу) заголовок не дається: їм
    час і навантаження сервера ні до чого. Винятки — /healthz (перевірка
    розгортання) і режим розробки без входу;
  * той самий час іде в журнал `ops.db web_timings` — через буфер і фоновий
    пакетний запис (`deferred.py`), а не в запиті. Пишуться лише запити тих,
    хто ввійшов через браузер, і лише ролей із `timings.roles` (сканери з
    інтернету й скрипти на кшталт зонда журнал не засмічують), без IP і без
    User-Agent — лише шаблон маршруту, роль і чи йшов цикл. Опитування
    /api/status (кожні 5 с, поки відкрита сторінка стану) — лише кожне
    `timings.sample_polls`-те;
  * маячок браузера (`_rum.html`) після переходу надсилає POST /api/rum: тип
    переходу, чи з'єднання вже було відкрите, час до першого байта й до
    завантаження, час кнопок (fetch до /api/…), «телефон/комп'ютер». Лише для
    ролей із `rum.roles`; IP і рядок браузера не зберігаються; не більше
    `rum.max_per_min` маячків на хвилину від ролі — решта приймається, але не
    пишеться (рахується);
  * GET /api/status/speed — зведення для власника (префікс /api/status, друг
    отримує 403): p50/p95 за маршрутами окремо для спокою й циклу, маячок,
    останні переходи, вікна блокування запису кроків циклу, цілі з конфігу.

Важливо для безпеки: запити через тунель Cloudflare теж приходять із
127.0.0.1, тож ніяких поблажок «локальним» тут немає — і маячок, і зведення
проходять звичайний вхід (AuthMiddleware) і перевірку same-origin для POST.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from starlette.routing import compile_path

from .. import configfiles, ops
from ..analytics import cache as analytics_cache
from ..config import RUN_TIMEOUT
from .deferred import DeferredWriter

log = logging.getLogger(__name__)
router = APIRouter()

WRITER = DeferredWriter()

SPEED = "speed"
ROLES = ("owner", "friend")
STATUS_POLL = "/api/status"
RUM_PATH = "/api/rum"
HEALTHZ = "/healthz"
NAV_TYPES = ("navigate", "reload", "back_forward", "prerender")
DEVICES = ("mobile", "desktop")
_PROTO = re.compile(r"^[a-z0-9./-]{1,16}$")
# Межа правдоподібності для чисел маячка: 10 хв. Більше — не перехід, а сміття
# (вкладка, що прокинулась після сну); такий рядок не зберігаємо.
_MAX_MS = 600_000


def _cfg():
    return configfiles.get(SPEED)


def _short_hash() -> str:
    return configfiles.get_hash(SPEED)[:12]


# --- Чи йде цикл збору --------------------------------------------------------------------


class CycleState:
    """Чи йде зараз цикл збору і який крок — для позначки в журналі часу.

    Оновлюється фоновим потоком (`deferred.py`) раз на `generations.poll_s`,
    а не в запиті: запит лише читає два поля в пам'яті.
    """

    def __init__(self) -> None:
        self.active = False
        self.step: str | None = None
        self.checked_at: float | None = None

    def refresh(self) -> None:
        # Стеля віку «running» — ліміт циклу (RUN_TIMEOUT) із запасом.
        self.active, self.step = ops.current_cycle(RUN_TIMEOUT * 1.5)
        self.checked_at = time.monotonic()


CYCLE = CycleState()
WRITER.add_tick(CYCLE.refresh)

# Покоління даних для кешів сайту (Блок 2, крок E5, D50): той самий фоновий
# потік раз на `generations.poll_s` читає ops.db web_generations і скидає лише
# ті кеші, чиє покоління змінилось.
from . import speedcache  # noqa: E402

WRITER.add_tick(speedcache.poll)


# --- Шаблони маршрутів ---------------------------------------------------------------------


def _route_table(app) -> list[tuple[re.Pattern, str, frozenset]]:
    cached = getattr(app.state, "_perf_routes", None)
    if cached is not None:
        return cached
    out = []

    def add(route, path):
        if not path:
            return
        regex, _, _ = compile_path(path)
        out.append((regex, path, frozenset(getattr(route, "methods", None) or ())))

    for r in app.router.routes:
        if hasattr(r, "effective_route_contexts"):          # підключений роутер
            for ctx in r.effective_route_contexts():
                add(ctx.original_route,
                    ctx.path or getattr(ctx.original_route, "path", ""))
        else:
            add(r, getattr(r, "path", ""))
    app.state._perf_routes = out
    return out


def route_template(app, path: str, method: str | None = None) -> str | None:
    """Шаблон маршруту для адреси з браузера: «/property/{property_id}», а не номер.

    Номер квартири, яку хтось відкривав, у журнал не йде — він для зведення не
    потрібен. Невідома адреса — None (рядок не зберігається).
    """
    for regex, template, methods in _route_table(app):
        if (method is None or method in methods) and regex.match(path):
            return template
    return None


# --- Server-Timing і журнал часу -----------------------------------------------------------


def _show_timing(scope) -> bool:
    """Чи давати заголовок Server-Timing цій відповіді.

    Лише тим, хто ввійшов (роль ставить AuthMiddleware після перевірки входу),
    /healthz (перевірка розгортання, D49) і в режимі розробки без входу.
    Незнайомцю з інтернету (401, сторінка входу, редирект на неї) час
    сервера не потрібен: маячок працює лише на сторінках тих, хто ввійшов,
    а сканерам заголовок показував би навантаження сервера (зайнята ops.db у
    циклі) без жодної користі.
    """
    if (scope.get("state") or {}).get("role"):
        return True
    if scope.get("path") == HEALTHZ:
        return True
    from .auth import accounts
    return not accounts()


class ServerTimingMiddleware:
    """Чистий ASGI (без BaseHTTPMiddleware): міряє від входу запиту до початку
    відповіді, додає `Server-Timing: app;dur=…` і кладе рядок у буфер журналу."""

    def __init__(self, app) -> None:
        self.app = app
        self._polls = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        at = ops._now()
        seen = {"status": None, "ms": None, "bytes": 0, "snapshot_ms": None}
        # Сюди analytics/cache.py допише час перебудови знімка, якщо вона
        # сталася в цьому запиті (холодний запит).
        builds: list[float] = []
        token = analytics_cache.BUILD_MS.set(builds)

        async def timed_send(message):
            if message["type"] == "http.response.start":
                ms = (time.perf_counter() - started) * 1000
                seen["status"], seen["ms"] = message["status"], ms
                if builds:
                    seen["snapshot_ms"] = sum(builds)
                if _show_timing(scope):
                    value = f"app;dur={ms:.1f}"
                    if builds:
                        value += f", snapshot;dur={sum(builds):.1f}"
                    headers = list(message.get("headers") or [])
                    headers.append((b"server-timing", value.encode("ascii")))
                    message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                seen["bytes"] += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        except Exception:
            if seen["status"] is None:
                seen["status"] = 500
                seen["ms"] = (time.perf_counter() - started) * 1000
            raise
        finally:
            analytics_cache.BUILD_MS.reset(token)
            try:
                self._record(scope, seen, at)
            except Exception as e:                  # noqa: BLE001 — вимір не ламає відповідь
                log.warning("журнал часу: рядок не записано: %s", e)

    def _record(self, scope, seen: dict, at: datetime) -> None:
        if seen["status"] is None:
            return
        state = scope.get("state") or {}
        if state.get("via") == "header":
            # Скрипт із заголовком Basic (зонд швидкості): ~720 його запитів за
            # замір змішались би з переглядами власника в p50/p95 і зсунули б
            # лічильник вибірки /api/status. Зонд пише свої виміри сам
            # (logs/speed/).
            return
        cfg = _cfg()
        if not cfg.timings.enabled:
            return
        route = getattr(scope.get("route"), "path", None)
        if route is None or route == RUM_PATH:
            return
        role = state.get("role")
        from .auth import accounts
        if role is None and accounts():
            # Вхід увімкнено, а ролі немає — це 401/303 для незнайомця (сканери з
            # інтернету). Такі запити журнал не засмічують.
            return
        if role is not None and role not in cfg.timings.roles:
            # Чи писати час запитів друга — рішення власника (D49, чекає): одна
            # правка timings.roles вимикає це без зміни коду.
            return
        method = scope.get("method", "GET")
        if route == STATUS_POLL and method == "GET":
            self._polls += 1
            if (self._polls - 1) % cfg.timings.sample_polls:
                return
        WRITER.add("web_timings", {
            "at": at, "route": route[:128], "method": method[:8],
            "status": int(seen["status"]), "ms": round(seen["ms"], 2),
            "bytes": seen["bytes"], "cycle_active": CYCLE.active,
            "cycle_step": CYCLE.step, "role": role, "config_hash": _short_hash(),
            "snapshot_ms": None if seen["snapshot_ms"] is None
            else round(seen["snapshot_ms"], 2),
        })


# --- Маячок браузера -----------------------------------------------------------------------


def rum_for(request: Request):
    """Для шаблону: налаштування маячка, якщо його треба вставити на цю сторінку
    (лише тим, хто ввійшов, і лише ролям із `rum.roles`), інакше None."""
    try:
        cfg = _cfg().rum
    except configfiles.ConfigError:
        return None
    role = getattr(request.state, "role", None)
    return cfg if cfg.enabled and role in cfg.roles else None


def _num(value, *, limit: float = _MAX_MS, integer: bool = True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > limit:
        return None
    return int(round(value)) if integer else round(float(value), 2)


class BadBeacon(ValueError):
    pass


def parse_beacon(app, raw: bytes, max_resources: int) -> dict:
    """Перевіряє тіло маячка; повертає поля рядка web_rum (без часу й ролі)."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BadBeacon("тіло не JSON") from None
    if not isinstance(data, dict):
        raise BadBeacon("очікувався об'єкт JSON")
    path = data.get("route")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 512:
        raise BadBeacon("немає маршруту")
    route = route_template(app, path, "GET")
    if route is None:
        raise BadBeacon("невідомий маршрут")
    load = _num(data.get("load"))
    if load is None:
        raise BadBeacon("немає часу завантаження")
    nav = data.get("nav") if data.get("nav") in NAV_TYPES else None
    proto = data.get("proto") if isinstance(data.get("proto"), str) \
        and _PROTO.match(data["proto"]) else None
    resources = []
    raw_res = data.get("res")
    if isinstance(raw_res, list):
        for entry in raw_res[:max_resources]:
            if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)):
                continue
            tmpl = route_template(app, entry[0].split("?", 1)[0][:512])
            ms = _num(entry[1])
            # Кнопки — це звернення до /api/…; сам маячок кнопкою не є.
            if tmpl is not None and tmpl.startswith("/api/") and tmpl != RUM_PATH \
                    and ms is not None:
                resources.append([tmpl, ms])
    return {
        "route": route[:128], "nav_type": nav,
        "reused": data["reused"] if isinstance(data.get("reused"), bool) else None,
        "ttfb_ms": _num(data.get("ttfb")), "dcl_ms": _num(data.get("dcl")), "load_ms": load,
        "transfer_bytes": _num(data.get("transfer"), limit=100_000_000),
        "server_ms": _num(data.get("server"), integer=False),
        "proto": proto, "device": data.get("device") if data.get("device") in DEVICES else None,
        "resources": json.dumps(resources, ensure_ascii=False) if resources else None,
    }


class RateCap:
    """Не більше `limit` подій за `window_s` на ключ (роль) — ковзне вікно в пам'яті.

    Маячок шле браузер того, хто ввійшов, але це не гарантія: вкрадена кука
    друга або сторінка, що зациклилась, могли б слати сотні маячків на
    секунду (замір рецензії на M4: 387 за секунду, усі прийняті), і кожен
    ставав би рядком на 30 днів; зведення для власника над таким журналом
    брало сотні МБ пам'яті на 3,7-гігабайтній машині (D49). Ліміт — на роль, а
    не на сесію: потоп від друга не забирає квоту власника.
    """

    def __init__(self, *, clock=time.monotonic, window_s: float = 60.0) -> None:
        self._clock = clock
        self._window_s = window_s
        self._lock = threading.Lock()
        self._seen: dict[str, deque] = {}
        self.dropped = 0

    def allow(self, key: str, limit: int) -> bool:
        now = self._clock()
        with self._lock:
            q = self._seen.setdefault(key, deque())
            while q and now - q[0] >= self._window_s:
                q.popleft()
            if len(q) >= limit:
                self.dropped += 1
                return False
            q.append(now)
            return True


RUM_CAP = RateCap()


@router.post(RUM_PATH, include_in_schema=False)
async def api_rum(request: Request):
    """Маячок часу переходу (обидві ролі). Вхід і same-origin — як для будь-якого POST."""
    cfg = _cfg().rum
    limit = cfg.max_body_bytes
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return JSONResponse({"ok": False, "error": "завелике тіло"}, status_code=413)
    role = getattr(request.state, "role", None)
    record = cfg.enabled and role in cfg.roles
    # Ліміт — до розбору: у потопі не витрачати процесор і на розбір.
    if record and not RUM_CAP.allow(role, cfg.max_per_min):
        return Response(status_code=204)            # понад ліміт: прийнято, не записано
    try:
        row = parse_beacon(request.app, body, cfg.max_resources)
    except BadBeacon as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if not record:
        return Response(status_code=204)            # прийнято, але не записується
    WRITER.add("web_rum", {**row, "at": ops._now(), "role": role,
                           "config_hash": _short_hash()})
    return Response(status_code=204)


# --- Зведення «Швидкість» ------------------------------------------------------------------


def _pct(values: list[float], p: float) -> float | None:
    """Перцентиль «найближчий ранг»: значення, яке справді траплялось.

    Для p95 по 20 запитах це 19-те за величиною — без інтерполяції, що
    вигадувала б час, якого не було.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return round(ordered[rank - 1], 1)


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "p50": _pct(values, 50), "p95": _pct(values, 95),
            "max": round(max(values), 1) if values else None}


def summary(now: datetime | None = None) -> dict:
    """Зведення для власника: сервер за добу, браузер за тиждень, вікна запису.

    Читається не більше `summary.max_rows` найновіших рядків кожного журналу і
    лише потрібні колонки (не об'єкти ORM). Замір D49 (M4): по 300 тис. рядків
    маячка й журналу часу — 2,4–2,7 с і +710 МБ пам'яті без стелі, 76–82 мс і
    +21 МБ зі стелею 20 000. Досягли стелі — `truncated: true`, перцентилі
    тоді — за найновішими рядками вікна.
    """
    from ..ops import WebRum, WebTiming, WriteWindow

    cfg, digest = configfiles.get_with_hash(SPEED)
    now = now or ops._now()
    since = now - timedelta(hours=cfg.summary.server_window_h)
    rum_since = now - timedelta(days=cfg.summary.rum_window_days)
    cap = cfg.summary.max_rows
    with ops.ops_session() as s:
        timings = s.execute(select(WebTiming.route, WebTiming.method, WebTiming.ms,
                                   WebTiming.cycle_active, WebTiming.snapshot_ms)
                            .where(WebTiming.at >= since)
                            .order_by(WebTiming.at.desc(), WebTiming.id.desc())
                            .limit(cap)).all()
        rum = s.execute(select(WebRum.load_ms, WebRum.reused, WebRum.device,
                               WebRum.resources)
                        .where(WebRum.at >= rum_since)
                        .order_by(WebRum.at.desc(), WebRum.id.desc())
                        .limit(cap)).all()
        recent = list(s.scalars(select(WebRum).where(WebRum.at >= rum_since)
                                .order_by(WebRum.at.desc(), WebRum.id.desc())
                                .limit(cfg.summary.recent)))
        windows = list(s.scalars(select(WriteWindow)
                                 .order_by(WriteWindow.at.desc(), WriteWindow.id.desc())
                                 .limit(cfg.summary.recent)))
    target = cfg.targets.server_p95_ms
    groups: dict[tuple[str, str], dict[str, list[float]]] = {}
    for route, method, ms, cycle, snapshot_ms in timings:
        g = groups.setdefault((route, method), {"all": [], "idle": [], "cycle": [],
                                                "warm": [], "cold": []})
        g["all"].append(ms)
        g["cycle" if cycle else "idle"].append(ms)
        g["warm" if snapshot_ms is None else "cold"].append(ms)
    routes = []
    for (route, method), g in sorted(groups.items()):
        row = {"route": route, "method": method, **_stats(g["all"]),
               "idle": _stats(g["idle"]), "cycle": _stats(g["cycle"]),
               "warm": _stats(g["warm"]), "cold": _stats(g["cold"])}
        row["ok"] = row["p95"] is not None and row["p95"] <= target
        routes.append(row)

    loads = [load for load, _reused, _device, _res in rum if load is not None]
    reused = [load for load, was_reused, _device, _res in rum
              if was_reused and load is not None]
    phone = [load for load, was_reused, device, _res in rum
             if was_reused and device == "mobile" and load is not None]
    buttons = [ms for _load, _reused, _device, res in rum
               for _path, ms in json.loads(res or "[]")]
    tab = cfg.targets.tab_switch_ms
    return {
        "generated_at": ops.as_utc_iso(now),
        "config_hash": digest[:12],
        "targets": {"server_p95_ms": target, "tab_switch_ms": tab,
                    "button_ms": cfg.targets.button_ms},
        "server": {"window_h": cfg.summary.server_window_h, "routes": routes,
                   "rows": len(timings), "truncated": len(timings) >= cap,
                   "pending": WRITER.pending(), "dropped": WRITER.dropped},
        "rum": {
            "window_days": cfg.summary.rum_window_days,
            "rows": len(rum), "truncated": len(rum) >= cap,
            "over_limit": RUM_CAP.dropped,
            "all": _stats(loads), "reused": _stats(reused), "phone_reused": _stats(phone),
            "buttons": _stats(buttons),
            "recent": [{"at": ops.as_utc_iso(r.at), "route": r.route, "nav": r.nav_type,
                        "reused": r.reused, "load_ms": r.load_ms, "ttfb_ms": r.ttfb_ms,
                        "server_ms": r.server_ms, "device": r.device, "role": r.role,
                        "ok": r.load_ms is not None and r.load_ms <= tab}
                       for r in recent],
        },
        "write_windows": [{"at": ops.as_utc_iso(w.at), "process": w.process, "step": w.step,
                           "max_ms": w.max_ms, "total_ms": w.total_ms, "txns": w.txns,
                           "max_sql": w.max_sql} for w in windows],
        # Кеші шляху запиту (D50): покоління, влучання й промахи, фонова робота.
        "cache": {**speedcache.snapshot(), "analytics": _analytics_cache()},
    }


def _analytics_cache() -> dict:
    snap = analytics_cache.HOLDER.current()
    return {"built": snap is not None,
            "age_s": round(snap.age_seconds) if snap is not None else None,
            "generation": snap.generation if snap is not None else None,
            "builds": analytics_cache.HOLDER.builds,
            "background_builds": analytics_cache.HOLDER.background_builds,
            "patches": analytics_cache.HOLDER.patches}


@router.get("/api/status/speed")
def api_status_speed():
    """Лише власник: префікс /api/status (auth.OWNER_ONLY)."""
    return JSONResponse(summary())
