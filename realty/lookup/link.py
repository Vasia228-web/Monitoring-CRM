"""«Перевірити зараз» за посиланням — у процесі перевірки (realty-lookup@), не в сайті.

Блок 5, крок E14, D59. Вимога власника: «один запит до джерела, далі звичайний
конвеєр якості, з обмеженням частоти в конфігу». Завдання `link` ставить сайт
(realty/lookup/limits.py), виконує — той самий процес, що «доїдає» перевірки при
відкритті квартири (opened.run, замок data/lookup.lock, шаблон systemd
realty-lookup@ з лімітами пам'яті й пріоритетом нижче за сайт).

Правила:
  * замок циклу (цикл, нічний диригент, міграція) зайнятий — завдання
    відкладається БЕЗ жодного запиту; після циклу його бере наступний процес
    перевірки (той самий механізм, що й для перевірки при відкритті, D50).
    Замок лише перевіряється (runner.lock_busy), не береться ніколи;
  * збір на машині вимкнено (COLLECTOR_OFF) — завдання закривається без мережі;
  * оголошення ключа вже є в базі — перевірка актуальності тим самим шляхом, що
    й при відкритті квартири (Блок 1: liveness.service.run, reason="lookup"):
    підписи «знято», правило повторного 404 з перевіркою існування, запобіжник.
    Поля наявних оголошень ця перевірка не переписує;
  * оголошення немає в базі — один запит адаптера сайту:
      - DOM.RIA: картка API (та сама, з якої збирає збирач): видалено → «знято»;
        інше місто → «не з Івано-Франківська»; не квартира на продаж → причина;
        інакше — НОВЕ оголошення звичайним конвеєром якості (gate.screen +
        Pipeline._upsert, без LLM; телефони в описі замінює privacy.py, ключ
        site_key ставить слухач ORM) і окрема квартира (dedup.place_new_listing);
      - OLX, rieltor: лише «живе / знято / не знайдено» за правилами Блоку 1
        (той самий класифікатор підписів), без вставки — розбирача однієї
        сторінки з містом для них немає;
      - flombu, лише-LUN, Благо — вимкнено конфігом (config/lookup.toml).
  * якщо, поки йшов запит, почався цикл, — відповідь відкидається, і завдання
    знову чекає кінця циклу (запис під час циклу не робиться ніколи; D59,
    відхилення 3);
  * у журнал — лише ключ «сайт:id» і підсумок, ніколи не адреса з параметрами.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select

from .. import configfiles
from ..models import Listing
from . import queue

log = logging.getLogger(__name__)

# Результат завдання (ops.lookup_checks.result, JSON): outcome — код тексту
# config/lookup.toml [messages]; решта — для сторінки й /status.
ALIVE_CODES = ("check_alive", "check_delisted", "check_restored", "check_unchanged")


def _cfg():
    return configfiles.load("lookup")


def _lcfg():
    from ..liveness import policy as pol

    return pol.load()


def _make_fetcher():
    from ..fetcher import Fetcher

    return Fetcher(delay=1.0, use_cache=False, label="lookup")


def _site_key(job) -> str | None:
    prefix = queue.link_key("")
    if not job.key or not job.key.startswith(prefix):
        return None
    key = job.key[len(prefix):]
    return key if ":" in key else None


def _existing(session, site_key: str) -> list:
    """Рядки ключа в базі (за site_key і за власним id сайту)."""
    from .resolve import find_rows

    return find_rows(session, [site_key])


# --- Наявне оголошення: перевірка актуальності за правилами Блоку 1 --------------------


def _check_existing(found, *, fetcher) -> dict:
    from ..liveness import service

    keys = sorted({f.site_key for f in found if f.site_key})
    if not keys:
        return {"outcome": "check_unchanged", "requests": 0}
    stats = service.run(kind="explicit", keys=keys, reason="lookup", fetcher=fetcher)
    result = {k: int(stats.get(k) or 0) for k in ("checked", "alive", "delisted", "restored",
                                                 "unknown", "requests", "blocked")}
    if result["restored"]:
        outcome = "check_restored"
    elif result["delisted"]:
        outcome = "check_delisted"
    elif result["alive"]:
        outcome = "check_alive"
    elif result["blocked"]:
        outcome = "check_blocked"
    else:
        outcome = "check_unchanged"
    pids = sorted({f.property_id for f in found if f.property_id is not None})
    result.update(outcome=outcome, property_id=pids[0] if len(pids) == 1 else None)
    return result


# --- Нове оголошення: адаптери сайтів (рівно один запит) ---------------------------------


def _verdict_outcome(verdict) -> str:
    from ..liveness.signatures import ALIVE, NOT_FOUND, REMOVED

    if verdict.kind == ALIVE:
        return "check_alive_not_added"
    if verdict.kind == REMOVED:
        return "check_removed"
    if verdict.kind == NOT_FOUND:
        return "check_not_found_on_source"
    if verdict.signature in ("blocked", "server_error", "net_error"):
        return "check_blocked"
    return "check_unknown"


def _probe_only(site_key: str, target: str, *, fetcher, lcfg) -> dict:
    """OLX, rieltor: один запит і класифікатор Блоку 1; у базу нічого не пишеться."""
    from ..liveness import policy as pol
    from ..liveness.signatures import classify

    host = pol.host_for(lcfg, site_key, target)
    if host is None or not lcfg.hosts[host].checkable:
        return {"outcome": "check_disabled", "requests": 0}
    spec = lcfg.hosts[host]
    res = fetcher.check(target, method=spec.method, delay=pol.pace(lcfg, host),
                        max_bytes=spec.max_bytes)
    verdict = classify(spec, lcfg.ria_page, site_key, res)
    return {"outcome": _verdict_outcome(verdict), "requests": 1, "http": res.code,
            "signature": verdict.signature}


def _domria_card(site_key: str, *, fetcher, cfg, lcfg) -> tuple[dict, dict | None]:
    """Картка DOM.RIA: (результат, запис для вставки або None). Один GET."""
    from ..config import DOMRIA_CITY_ID
    from ..fetcher import BLOCKING_CODES
    from ..liveness import policy as pol

    ident = site_key.split(":", 1)[1]
    rules = lcfg.ria_page
    host = pol.host_for(lcfg, site_key, None)
    delay = pol.pace(lcfg, host) if host else 1.0
    res = fetcher.check(rules.api_card_url.format(id=ident), method="GET", delay=delay,
                        max_bytes=cfg.check.domria_card_max_bytes)
    out = {"requests": 1, "http": res.code}
    if res.code == 0 or res.code in BLOCKING_CODES or res.code >= 500:
        return {**out, "outcome": "check_blocked"}, None
    if res.code in (404, 410):
        # Картка 404/410 — не перевірений підпис (Етап 0: 100 зі 100 карток — 200,
        # зняті теж 200 з is_delete), тож це «не знайшли», а не «знято».
        return {**out, "outcome": "check_not_found_on_source"}, None
    if res.code != 200 or not res.body:
        return {**out, "outcome": "check_unknown"}, None
    try:
        card = json.loads(res.body)
    except ValueError:
        return {**out, "outcome": "check_unknown"}, None
    if not isinstance(card, dict) or str(card.get("realty_id")) != ident:
        return {**out, "outcome": "check_unknown"}, None
    if any(card.get(k) for k in rules.api_deleted_keys):
        return {**out, "outcome": "check_removed"}, None
    if card.get("city_id") != DOMRIA_CITY_ID:
        city = card.get("city_name_uk") or card.get("city_name")
        return {**out, "outcome": "check_not_city",
                "city": str(city)[:64] if city else None}, None
    if card.get("realty_type_id") not in cfg.check.domria_flat_realty_type_ids or \
            card.get("advert_type_id") not in cfg.check.domria_sale_advert_type_ids:
        return {**out, "outcome": "check_not_flat"}, None
    from ..sources.domria import DomRiaSource

    src = DomRiaSource()
    rec = src._parse(card)
    if not rec or not rec.get("original_url"):
        return {**out, "outcome": "check_unknown"}, None
    return out, src.finalize(rec)


def _insert(rec: dict) -> dict:
    """Нове оголошення звичайним конвеєром якості + окрема квартира. Наявне не чіпає."""
    from .. import dedup
    from ..db import session_scope
    from ..pipeline import Pipeline
    from ..quality.staging import QualityGate

    gate = QualityGate()
    with session_scope() as s:
        exists = s.scalar(select(Listing.id).where(Listing.source == rec["source"],
                                                   Listing.external_id == rec["external_id"]))
        if exists is not None:
            # Збір устиг раніше — запис джерела не переписуємо (поля наявних
            # оголошень оновлює лише збір, D43).
            pid = s.scalar(select(Listing.property_id).where(Listing.id == exists))
            return {"outcome": "check_exists", "listing_id": exists, "property_id": pid}
        screened = gate.screen(s, [rec])
        if not screened:
            return {"outcome": "check_unknown"}
        with s.begin_nested():
            action = Pipeline._upsert(s, screened[0])
        row = s.scalar(select(Listing).where(Listing.source == rec["source"],
                                             Listing.external_id == rec["external_id"]))
        pid = dedup.place_new_listing(s, row.id)
        quality = row.quality_status
        lid = row.id
    added = action == "inserted"
    return {"outcome": ("check_added" if quality == "ok" else "check_added_quarantine")
            if added else "check_exists",
            "listing_id": lid, "property_id": pid, "quality": quality,
            "inserted": 1 if added else 0}


# --- Виконання завдання -------------------------------------------------------------------


def execute(job_id: int, *, fetcher=None) -> tuple[str, dict]:
    """Виконати одне завдання `link`: (кінцевий стан, результат)."""
    from ..db import SessionLocal
    from . import opened

    job = queue.get(job_id)
    if job is None:
        return "missing", {}
    if job.kind != queue.KIND_LINK:
        return "foreign", {}
    if job.state not in queue.RUNNABLE:
        return "taken", {}
    site_key = _site_key(job)
    if site_key is None:
        queue.finish(job_id, "failed", result={"outcome": "check_failed"},
                     message="ключ завдання не «сайт:id»", only_if=queue.RUNNABLE)
        return "failed", {}
    if opened.collector_off():
        result = {"outcome": "check_collector_off", "requests": 0}
        queue.finish(job_id, "skipped", result=result, message="COLLECTOR_OFF",
                     only_if=queue.RUNNABLE)
        return "skipped", result
    holder = opened.cycle_busy()
    if holder is not None:
        if job.state == "queued":
            queue.defer(job_id, message=f"замок циклу: PID {holder.get('pid')}")
        return "deferred", {}
    if not queue.claim(job_id):
        return "taken", {}
    own = fetcher is None
    try:
        cfg = _cfg()
        lcfg = _lcfg()
        family = site_key.split(":", 1)[0]
        with SessionLocal() as s:
            found = _existing(s, site_key)
        fetcher = fetcher or _make_fetcher()
        if found:
            result = _check_existing(found, fetcher=fetcher)
        elif not cfg.check.enabled or family not in cfg.check.families:
            result = {"outcome": "check_disabled", "requests": 0}
        elif family == "domria":
            result, rec = _domria_card(site_key, fetcher=fetcher, cfg=cfg, lcfg=lcfg)
            if rec is not None:
                if "domria" not in cfg.check.insert_families:
                    result = {**result, "outcome": "check_alive_not_added"}
                elif opened.cycle_busy() is not None:
                    # Цикл почався, поки йшов запит: записуємо лише після нього.
                    queue.back_to_deferred(job_id, message="цикл почався під час перевірки")
                    log.info("перевірка за посиланням %s: відкладено (цикл)", site_key)
                    return "deferred", {}
                else:
                    result = {**result, **_insert(rec)}
        elif not job.target:
            result = {"outcome": "need_full_link", "requests": 0}
        else:
            result = _probe_only(site_key, job.target, fetcher=fetcher, lcfg=lcfg)
        state = "done"
        queue.finish(job_id, state, result=result, message=result.get("outcome"))
        log.info("перевірка за посиланням %s: %s (запитів %s)", site_key,
                 result.get("outcome"), result.get("requests"))
        return state, result
    except Exception as e:                       # noqa: BLE001 — завдання закриваємо будь-як
        log.warning("перевірка за посиланням %s не вдалась: %s", site_key, type(e).__name__)
        queue.finish(job_id, "failed", result={"outcome": "check_failed"},
                     message=type(e).__name__)
        return "failed", {}
    except BaseException:
        queue.finish(job_id, "failed", result={"outcome": "check_failed"},
                     message="процес перевірки зупинено")
        raise
    finally:
        if own and fetcher is not None:
            fetcher.close()
