"""Класифікатор відповіді: живе, знято, не знайдено чи не визначено (Блок 1, E8, D52).

Чисті функції — без бази й мережі. Вхід — `ProbeResult` (код, переадресації,
тіло GET), політика хоста й ключ, який питали; вихід — `Verdict`:

  * `alive`     — живе (зрозуміла відповідь: last_alive_at);
  * `removed`   — знято за ЯВНИМ сигналом: код із `removed_statuses` (410) або
                  сторінка DOM.RIA, де стан І банер разом кажуть «видалено»;
                  `repeat_404` ставить лише правило повторного 404 з перевіркою
                  існування (realty/liveness/existence.py), ніколи — класифікатор;
  * `not_found` — 404: «не достукались до цієї адреси», не зняття (рішення
                  власника 1, D46);
  * `unknown`   — капча, блокування, 5xx, тайм-аут, порожня чи нерозпізнана
                  сторінка, суперечність ознак, переадресація на чужий ключ.

Підписи (`signature`, ≤24 символи, у check_events.signature): alive, status_410,
ria_archive, repeat_404, not_found, blocked, net_error, server_error, too_large,
unrecognized, conflict, id_mismatch.

DOM.RIA (Етап 0, D45): HEAD сліпий — 18 з 18 знятих віддають 200. «Знято» —
200 без переадресації, стан сторінки `window.__INITIAL_STATE__.listing.data.realty`
(status = archive, isActive = false, isArchive = true, realty_id = id з адреси)
І банер — елемент класу m-sold із точним текстом «Оголошення видалено та не бере
участі у пошуку». CSS-правила `.bg.m-sold{…}` є й на живих сторінках — тому шукаємо
елемент, а не підрядок. Фраза «неактуальн» — кнопка скарги на ЖИВИХ сторінках.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..fetcher import BLOCKING_CODES, ProbeResult
from . import policy as pol

ALIVE, REMOVED, NOT_FOUND, UNKNOWN = "alive", "removed", "not_found", "unknown"
KINDS = (ALIVE, REMOVED, NOT_FOUND, UNKNOWN)
# Підписи «знято» за причинами — для подій і /status.
REMOVAL_SIGNATURES = ("status_410", "ria_archive", "repeat_404")


@dataclass(frozen=True)
class Verdict:
    kind: str
    signature: str
    code: int
    evidence: dict = field(default_factory=dict)
    # Дата зняття на джерелі (DOM.RIA deleted_at, UTC без зони) — лише з removed.
    source_removed_at: datetime | None = None
    # Перевірка існування знайшла оголошення за іншою адресою, і та адреса жива.
    repaired_url: str | None = None
    repair_strategy: str | None = None
    # Розібраний стан сторінки — лише для гачків доказів у потоці смуги; у базу не
    # йде й одразу після гачків відкидається (engine.run_lane): стан DOM.RIA —
    # сотні КБ на сторінку.
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def alive_flag(self) -> bool | None:
        """Значення check_events.alive: True — живе, False — знято, None — ні те, ні те."""
        return {ALIVE: True, REMOVED: False}.get(self.kind)


def classify_code(code: int) -> bool | None:
    """Семантика HEAD за кодом (verify.classify): 410 → знято, 2xx/3xx → живе;
    404, 0, 401/403/429, 5xx та інше → None (не висновок). Один 404 — не знято."""
    if code == 410:
        return False
    if 200 <= code < 400:
        return True
    return None


def verdict_from_code(code: int, *, signature_kind: str = "code") -> Verdict:
    """Вердикт за голим кодом (семантика HEAD) — для викликів без ProbeResult."""
    flag = classify_code(code)
    if flag is True:
        return Verdict(ALIVE, "alive", code, {"code": code})
    if flag is False:
        return Verdict(REMOVED, "status_410", code, {"code": code})
    if code == 404:
        return Verdict(NOT_FOUND, "not_found", code, {"code": code})
    return Verdict(UNKNOWN, _unknown_signature(code, None), code, {"code": code})


def _unknown_signature(code: int, error: str | None) -> str:
    if error == "too_large":
        return "too_large"
    if code == 0:
        return "net_error"
    if code in BLOCKING_CODES:
        return "blocked"
    if code >= 500:
        return "server_error"
    return "unrecognized"


def evidence_of(result: ProbeResult) -> dict:
    """Доказ відповіді без тіла: код, метод, кінцева адреса, переадресації — безпечні."""
    ev = {"code": result.code, "method": result.method,
          "final_url": pol.safe_url(result.final_url)}
    if result.chain:
        ev["chain"] = [[c, pol.safe_url(u)] for c, u in result.chain]
    if result.error:
        ev["error"] = result.error
    return ev


def _final_key(result: ProbeResult):
    from .. import links

    try:
        return links.site_key(result.final_url)
    except Exception:                                # noqa: BLE001 — не розібрали = не наш ключ
        return None


def _path_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).path.rstrip("/")
    except ValueError:
        return ""


def classify(spec, rules, key: str, result: ProbeResult) -> Verdict:
    """Вердикт для ключа `key` за відповіддю `result` і політикою хоста `spec`.

    `rules` — таблиця [ria_page] конфігу (для signature = ria_page).
    """
    ev = evidence_of(result)
    if result.error == "too_large" or (result.error and result.code == 0) or result.code == 0:
        return Verdict(UNKNOWN, _unknown_signature(result.code, result.error), result.code, ev)
    if result.code in BLOCKING_CODES or result.code >= 500:
        return Verdict(UNKNOWN, _unknown_signature(result.code, None), result.code, ev)
    if result.error:
        # Обірване тіло (тайм-аут посеред читання) — сторінки немає, висновку теж.
        return Verdict(UNKNOWN, "net_error", result.code, ev)
    # Переадресація на інше оголошення, каталог чи головну — не «живе» й не «знято».
    if result.chain:
        if pol.is_row_key(key):
            moved = _path_of(result.final_url) != _path_of(result.url)
        else:
            moved = _final_key(result) != key
        if moved:
            return Verdict(UNKNOWN, "id_mismatch", result.code, ev)
    code = result.code
    if code in spec.removed_statuses:
        return Verdict(REMOVED, f"status_{code}", code, ev)
    if code in spec.not_found_statuses:
        return Verdict(NOT_FOUND, "not_found", code, ev)
    if not 200 <= code < 400:
        return Verdict(UNKNOWN, "unrecognized", code, ev)
    if spec.signature == "ria_page":
        return ria_page(rules, key, result, ev)
    return Verdict(ALIVE, "alive", code, ev)


# --- DOM.RIA ------------------------------------------------------------------------------


def extract_state(html: str, marker: str) -> dict | None:
    """JSON після `marker` (window.__INITIAL_STATE__=…) — дужковим скануванням.

    Сторінка — ~212 КБ, стан — значна її частина; повний json.loads стану — один
    раз на сторінку. None — маркера немає або JSON не розбирається.
    """
    i = html.find(marker)
    if i < 0:
        return None
    start = html.find("{", i)
    if start < 0:
        return None
    depth, ins, esc = 0, False, False
    end = None
    for j in range(start, len(html)):
        ch = html[j]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            ins = not ins
            continue
        if ins:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end is None:
        return None
    try:
        value = json.loads(html[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


_SPACE = re.compile(r"\s+")


def has_banner(html: str, css_class: str, text: str) -> bool:
    """Чи є на сторінці ЕЛЕМЕНТ класу `css_class`, чий текст містить `text`.

    Розбір HTML (lxml), а не пошук підрядка: правило `.bg.m-sold{…}` у <style> є
    й на живих сторінках. Пробіли нормалізуються.
    """
    if css_class not in html:
        return False
    import lxml.html

    try:
        doc = lxml.html.document_fromstring(html)
    except (ValueError, Exception):                # noqa: BLE001 — не розібрали = банера немає
        return False
    want = _SPACE.sub(" ", text).strip()
    xpath = ("//*[contains(concat(' ', normalize-space(@class), ' '), ' "
             + css_class + " ')]")
    for el in doc.xpath(xpath):
        if want in _SPACE.sub(" ", el.text_content()):
            return True
    return False


def _dig(obj, path):
    for part in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def ria_deleted_at_utc(realty: dict, rules) -> datetime | None:
    """Дата зняття зі стану сторінки → UTC без зони: deleted_at_ts (секунди UTC),
    запасне deleted_at — час `source_tz` (Київ)."""
    ts = realty.get(rules.deleted_at_ts_field)
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    raw = realty.get(rules.deleted_at_field)
    if isinstance(raw, str) and raw.strip():
        from zoneinfo import ZoneInfo

        try:
            local = datetime.fromisoformat(raw.strip()).replace(
                tzinfo=ZoneInfo(rules.source_tz))
        except ValueError:
            return None
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    return None


def ria_page(rules, key: str, result: ProbeResult, ev: dict) -> Verdict:
    """Сторінка DOM.RIA з кодом 2xx: стан і банер мають сказати одне й те саме."""
    code = result.code
    body = result.body or ""
    if not body.strip():
        return Verdict(UNKNOWN, "unrecognized", code, {**ev, "ria": {"empty": True}})
    state = extract_state(body, rules.state_marker)
    realty = _dig(state, rules.state_path) if state else None
    banner = has_banner(body, rules.banner_class, rules.banner_text)
    if not isinstance(realty, dict):
        # Немає машинного стану (капча, сторінка помилки, нова розмітка). Банер сам
        # по собі — лише половина підпису.
        return Verdict(UNKNOWN, "conflict" if banner else "unrecognized", code,
                       {**ev, "ria": {"state": False, "banner": banner}})
    status = realty.get(rules.status_field)
    is_active = realty.get(rules.active_field)
    is_archive = realty.get(rules.archive_field)
    seen = {"status": status, "isActive": is_active, "isArchive": is_archive,
            "banner": banner}
    want_id = pol.id_of_key(key)
    got_id = realty.get(rules.id_field)
    if want_id is None or str(got_id) != want_id:
        return Verdict(UNKNOWN, "id_mismatch", code, {**ev, "ria": seen})
    extra = {"ria_realty": realty, "ria_data": _dig(state, rules.state_path[:-1])}
    archived = (status == rules.archive_status and is_active is False
                and is_archive is True)
    if archived and banner:
        if rules.removed_requires_no_redirect and result.chain:
            return Verdict(UNKNOWN, "conflict", code, {**ev, "ria": {**seen, "redirect": True}})
        deleted = ria_deleted_at_utc(realty, rules)
        if deleted is not None:
            seen["deleted_at"] = deleted.isoformat(timespec="seconds")
        return Verdict(REMOVED, "ria_archive", code, {**ev, "ria": seen},
                       source_removed_at=deleted, extra=extra)
    if (status == rules.active_status and is_active is True and not is_archive
            and not banner):
        return Verdict(ALIVE, "alive", code, {**ev, "ria": seen}, extra=extra)
    if archived or banner or status == rules.archive_status:
        return Verdict(UNKNOWN, "conflict", code, {**ev, "ria": seen})
    return Verdict(UNKNOWN, "unrecognized", code, {**ev, "ria": seen})
