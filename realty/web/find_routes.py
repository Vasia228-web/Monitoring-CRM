"""Пошук за посиланням (Блок 5, крок E14, D59): поле у верхній панелі, /find, «Перевірити зараз».

Вимога власника: вставляю посилання на оголошення з будь-якого з 5 джерел —
відкривається сторінка цієї квартири на нашому сайті з виділеним оголошенням;
приймає й наше посилання на квартиру та id оголошення; не знайдено — зрозуміла
причина; «Перевірити зараз» — один запит до джерела, з лімітом у конфігу.

Маршрути (обидві ролі; вхід і same-origin для POST — як і для решти сайту, auth.py):
  * POST /find (форма з верхньої панелі) — розбір і пошук у базі, далі 303 (PRG):
    одна квартира → /property/<id>?hl=<ключ>#found; інакше → /find?q=<безпечна
    форма> або /find?err=<код>. Вставлений текст іде лише в тілі POST: у адресі
    переходу, журналі й історії браузера — лише ключ «сайт:id» чи канонічна адреса
    без query й без піддомену агенції;
  * GET /find?q=… — сторінка вибору (кілька квартир) або причини «не знайдено»;
  * POST /api/find/check {q} — «Перевірити зараз»: завдання в черзі (202), ліміт
    (429) чи причина відмови (400). Сайт у мережу не ходить: завдання виконує
    процес realty-lookup@ (realty/lookup/link.py);
  * GET /api/find/check/{job_id} — стан перевірки для сторінки (опитування).

Поле у верхній панелі не робить жодного запиту до бази: шаблон бере лише конфіг
(`find_ui`, з пам'яті процесу).
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, quote, urlencode

import anyio
from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import configfiles, links
from ..db import SessionLocal
from ..lookup import limits, queue
from ..lookup import resolve as rs

log = logging.getLogger(__name__)
router = APIRouter()

# Причини відмови розбору (realty/links.py NotALink.reason) — лише вони можуть
# прийти в ?err=; будь-що інше — «не вдалося розібрати».
PARSE_CODES = ("empty", "no_url", "unrecognized", "unsupported_host", "short_link",
               "chat_link", "not_listing", "not_flat", "own_not_property")
FINAL = ("done", "failed", "skipped")


def _cfg():
    return configfiles.get("lookup")


def ui_config():
    """Для шаблону верхньої панелі: підказка, назва, стеля довжини — без бази.

    Зламаний config/lookup.toml — поля немає (сайт не падає; `config check`
    зупиняє таке ще до розгортання)."""
    try:
        return _cfg().ui
    except configfiles.ConfigError as e:
        log.error("config/lookup.toml не читається — поле пошуку сховано: %s", e)
        return None


def _role(request: Request) -> str:
    """Роль із сесії входу (сервер), а не з запиту; вхід вимкнено — як власник."""
    return getattr(request.state, "role", None) or "owner"


# --- Розбір вставленого тексту --------------------------------------------------------------


def _bare_host(netloc: str) -> str:
    host = netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    return host[4:] if host.startswith("www.") else host


def _own_hosts(request: Request) -> set[str]:
    """Хости, під якими людина зараз відкрила наш сайт (тунель, домен, localhost)."""
    out = set()
    for h in (request.headers.get("host"), request.headers.get("x-forwarded-host")):
        if h:
            out.add(_bare_host(h.split(",")[0].strip()))
    return out


_URL_HEAD = re.compile(r"(?i)^(?:https?://)?(?P<host>[^/\s?#]+)(?P<rest>/\S*)?$")


def parse_input(text: str, request: Request | None = None):
    """Вставлений текст → links.Link | links.NotALink.

    Понад спільний розбір (links.parse): відносне посилання нашого сайту
    («/property/123») і адреса під тим самим хостом, під яким відкрито сайт
    (тунель без PUBLIC_DOMAIN, локальна адреса), — теж наші посилання.
    """
    t = (text or "").strip()
    if t.startswith("/") and not t.startswith("//"):
        return links.parse("http://localhost" + t.split()[0])
    got = links.parse(t)
    if request is not None and isinstance(got, links.NotALink) \
            and got.reason == "unsupported_host":
        m = _URL_HEAD.match(t)
        if m and _bare_host(m.group("host")) in _own_hosts(request):
            return links.parse("http://localhost" + (m.group("rest") or "/"))
    return got


def safe_q(link, text: str) -> str:
    """Що покласти в адресу /find?q=…: ключ, канонічна адреса або сам голий id.

    Ніколи не сирий текст із параметрами: адреса переходу потрапляє в журнал
    доступу й історію браузера (у вставленому буває піддомен агенції з номером
    телефону, токен чату, utm)."""
    if link.via in links.BARE_VIAS:
        if link.via == "family_key":
            return link.key
        # Голий id: перевірений строгими виразами links.toml (лише цифри, літери,
        # «№», «#», «ID:») — коротко й без адреси.
        return text.strip()[:32]
    if link.case_lost:
        return f"https://www.olx.ua/d/uk/obyavlenie/id{link.id}.html"
    if link.family == "own":
        return f"№{link.id}"
    if link.canonical_url and "?" not in link.canonical_url:
        return link.canonical_url
    return link.key or ""


def property_url(res: rs.Resolution) -> str:
    url = f"/property/{int(res.property_id)}"
    if res.key:
        url += "?hl=" + quote(res.key, safe=":") + "#found"
    return url


def _resolve(request: Request, text: str) -> tuple[object, rs.Resolution]:
    cfg = _cfg()
    text = (text or "")[:cfg.ui.max_input_chars]
    link = parse_input(text, request)
    started = time.perf_counter()
    if isinstance(link, links.Link):
        with SessionLocal() as s:
            res = rs.resolve(s, link)
    else:
        res = rs.resolve(None, link)
    # У журнал — лише сімейство й підсумок, ніколи не вставлений текст.
    log.info("пошук за посиланням: %s → %s%s (%.0f мс)", res.family or "—", res.kind,
             f"/{res.code}" if res.code else "", (time.perf_counter() - started) * 1000)
    return link, res


def _target(request: Request, text: str) -> str:
    link, res = _resolve(request, text)
    if res.kind == "invalid":
        return "/find?" + urlencode({"err": res.code if res.code in PARSE_CODES
                                     else "unrecognized"})
    if res.kind == "property":
        return property_url(res)
    return "/find?" + urlencode({"q": safe_q(link, text)})


@router.post("/find")
async def find_submit(request: Request):
    """Форма верхньої панелі → 303 на квартиру, вибір або причину (PRG)."""
    raw = await request.body()
    form = parse_qs(raw[:65536].decode("utf-8", errors="replace"), keep_blank_values=True)
    text = (form.get("q") or [""])[0]
    url = await anyio.to_thread.run_sync(_target, request, text)
    return RedirectResponse(url, status_code=303)


# --- Сторінка /find --------------------------------------------------------------------------


def _check_offer(cfg, role: str, link, res: rs.Resolution) -> dict:
    """Чи показати «Перевірити зараз» на сторінці «не знайдено» і чому ні."""
    if res.kind != "not_found" or res.code != "not_in_db" or not isinstance(link, links.Link) \
            or link.key is None:
        return {}
    family = link.family
    if family == "blago":
        return {"why": "blago_unverifiable"}
    if not cfg.check.enabled or family not in cfg.check.families:
        return {"why": "check_unavailable"}
    if family != "domria" and not link.fetch_url:
        return {"why": "need_full_link"}
    if limits.role_limit(cfg, role) <= 0:
        return {"why": "check_off_for_role"}
    return {"q": link.key if family == "domria" else safe_q(link, "")}


def _in_work(s) -> int:
    from .analytics_routes import _in_work as count
    return count(s)


def _page(request: Request, q: str, err: str):
    from .app import templates

    cfg = _cfg()
    role = _role(request)
    link = res = None
    if err:
        res = rs.Resolution("invalid", err if err in PARSE_CODES else "unrecognized")
    elif q.strip():
        link, res = _resolve(request, q)
        if res.kind == "property":
            return RedirectResponse(property_url(res), status_code=303)
    else:
        res = rs.Resolution("invalid", "empty")
    with SessionLocal() as s:
        in_work = _in_work(s)
    recognized = None
    if isinstance(link, links.Link) and link.family not in ("number",):
        recognized = {"site": cfg.sites.get(link.family, link.family), "id": link.id}
    return templates.TemplateResponse(request, "find.html", {
        "page": "find", "in_work": in_work, "path": "/find", "f": {},
        "res": res, "recognized": recognized, "msg": cfg.messages, "sites": cfg.sites,
        "options": res.options[:cfg.ui.choice_limit], "more": max(0, len(res.options)
                                                                  - cfg.ui.choice_limit),
        "offer": _check_offer(cfg, role, link, res),
        "is_owner": role == "owner", "check_cfg": cfg.check,
    })


@router.get("/find", response_class=HTMLResponse)
def find_page(request: Request, q: str = Query(""), err: str = Query("")):
    """Сторінка результату: вибір між квартирами або причина «не знайдено»."""
    return _page(request, q[:_cfg().ui.max_input_chars], err[:32])


# --- Плашка на сторінці квартири (?hl=) ------------------------------------------------------


def found_context(rows, hl: str | None, role: str | None) -> dict | None:
    """Для сторінки квартири: які оголошення підсвітити й що написати в плашці.

    `rows` — уже завантажені оголошення квартири (жодного нового запиту). Ключ
    перевіряється тим самим виразом, що й явна форма «сайт:id» розбору: розмітка
    чи сміття в ?hl= — плашки немає, сторінка звичайна.
    """
    key = rs.valid_key(hl)
    if key is None:
        return None
    try:
        cfg = _cfg()
    except configfiles.ConfigError:
        return None
    found = rs.found_on_page(rows, key)
    if not found:
        return None
    family, ident = key.split(":", 1)
    checkable = (cfg.check.enabled and family in cfg.check.families
                 and limits.role_limit(cfg, role) > 0)
    offer = checkable and any(f.status != rs.ACTIVE and not f.unconfirmed for f in found)
    return {"key": key, "site": cfg.sites.get(family, family), "id": ident, "found": found,
            "urls": {f.url for f in found}, "ids": {f.listing_id for f in found},
            "msg": cfg.messages, "sites": cfg.sites, "check_q": key if offer else None,
            "check_cfg": cfg.check}


# --- «Перевірити зараз» ----------------------------------------------------------------------


def _text(cfg, code: str | None, result: dict | None = None) -> str:
    text = cfg.messages.get(code or "", "") or cfg.messages["check_failed"]
    city = (result or {}).get("city")
    if code == "check_not_city" and city:
        text = f"{text} ({city})"
    return text


def _reply(status: int, cfg, code: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": status < 400, "code": code, "text": _text(cfg, code), **extra},
                        status_code=status)


def _submit_check(request: Request, q: str) -> JSONResponse:
    from ..lookup import opened
    from .livecheck import LIVE

    cfg = _cfg()
    role = _role(request)
    if not cfg.check.enabled:
        return _reply(400, cfg, "check_disabled")
    link = parse_input((q or "")[:cfg.ui.max_input_chars], request)
    if not isinstance(link, links.Link) or link.key is None or link.family in ("own", "number") \
            or len(link.candidates) > 1:
        return _reply(400, cfg, "need_full_link")
    key, family = link.key, link.family
    with SessionLocal() as s:
        found = rs.find_rows(s, [key])
    if family == "blago" or (found and all(f.unconfirmed for f in found)):
        return _reply(400, cfg, "blago_unverifiable")
    if family not in cfg.check.families:
        return _reply(400, cfg, "check_disabled")
    target = None
    if not found and family != "domria":
        target = link.fetch_url
        if not target:
            return _reply(400, cfg, "need_full_link")
    sub = limits.submit(cfg, role=role, site_key=key, target=target,
                        cycle_busy=opened.cycle_busy() is not None)
    if sub.code == "check_off_for_role":
        return _reply(403, cfg, sub.code)
    if sub.code == "check_rate_limited":
        minutes = max(1, -(-int(sub.retry_after_s or 0) // 60))
        resp = _reply(429, cfg, sub.code, retry_after_s=sub.retry_after_s, retry_after_min=minutes)
        resp.headers["Retry-After"] = str(int(sub.retry_after_s or 60))
        return resp
    if not sub.reused or sub.state in queue.RUNNABLE:
        LIVE.watch(sub.job_id)
    log.info("перевірити зараз: %s, завдання %s (%s%s)", key, sub.job_id, sub.state,
             ", повтор" if sub.reused else "")
    return JSONResponse({**_status(cfg, sub.job_id), "job": sub.job_id, "reused": sub.reused},
                        status_code=202)


@router.post("/api/find/check")
def api_check(request: Request, payload: dict = Body(default={})):
    """«Перевірити зараз»: 202 {job} | 429 (ліміт) | 400/403 (причина)."""
    return _submit_check(request, str((payload or {}).get("q") or ""))


def _status(cfg, job_id: int) -> dict:
    from .livecheck import LIVE

    job = queue.get(job_id)
    if job is None or job.kind != queue.KIND_LINK:
        return {"ok": False, "state": "missing", "final": True, "outcome": "check_failed",
                "text": cfg.messages["check_failed"], "property_url": None}
    state = job.state
    if state in ("queued", "running", "deferred") and LIVE._expired(job):
        state = "failed"                     # процес не дожив — більше не чекаємо
    result = queue.result_of(job) if state in FINAL else {}
    code = (result.get("outcome") or "check_failed") if state in FINAL else f"check_{state}"
    pid = result.get("property_id")
    url = None
    if pid:
        url = f"/property/{int(pid)}?hl=" + quote(job.key[len(queue.link_key("")):],
                                                   safe=":") + "#found"
    return {"ok": True, "state": state, "final": state in FINAL, "outcome": code,
            "text": _text(cfg, code, result), "property_url": url}


@router.get("/api/find/check/{job_id}")
def api_check_status(job_id: int):
    """Стан перевірки для сторінки: queued | deferred | running | done | failed | skipped."""
    got = _status(_cfg(), job_id)
    return JSONResponse(got, status_code=200 if got["ok"] else 404)
