"""Сирі докази типу продавця — за білим списком config/seller.toml (Блок 3, крок E11, D60).

Чисті функції (без бази й мережі) для збирачів і нічних робіт:

  * `from_feed_item` — об'єкт стрічки LUN (payload) чи flombu (attributes JSON:API):
    лише скаляри з білого списку шляхів і позначка «стрічку бачили» (дата);
  * `from_olx_detail` — сторінка оголошення OLX: чип «Приватна особа»/«Бізнес»
    (листовий вузол контейнера параметрів без двокрапки), прапорці («Без комісії»),
    «Тип угоди» кодами, непрозорий id профілю; trader-title (ім'я для показу) не
    читається ніколи;
  * `from_rieltor_detail` — картка rieltor.ua: роль лише з білого списку текстів,
    наявність агенції й непрозорий id агенції (хеш піддомену);
  * `olx_tab_page` — сторінка вкладки пошуку OLX: чи активна саме ця вкладка, ключі
    «olx:<токен>» карток (без просуваних), чи є наступна сторінка;
  * `opaque_id` — префікс + blake2b(slug): імен і адрес у базі немає; slug, схожий на
    номер телефону, не зберігається зовсім (хеш номера — теж «похідне від телефону»).

Кожен рядок-значення ще й проходить privacy.find: номер — значення не береться.
Записують ці словники ЛИШЕ туди, де порожньо (pipeline._fill_only, liveness.apply,
night.evidence): нові ключі JSON, seller_profile — якщо NULL.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Довжина рядкового значення доказу: коди й короткі підписи («rieltor», «%»), не текст.
MAX_TEXT = 48
_warned: set[str] = set()


def config():
    """Чинний config/seller.toml або None (зламаний — доказів не беремо, журнал раз)."""
    from .. import configfiles

    try:
        return configfiles.get("seller")
    except configfiles.ConfigError as e:
        if "seller" not in _warned:
            _warned.add("seller")
            log.error("config/seller.toml не читається — доказів типу продавця не беру: %s", e)
        return None


def today() -> str:
    return date.today().isoformat()


def _has_phone(text: str) -> bool:
    from .. import privacy

    try:
        return bool(privacy.find(text)[1])
    except Exception:                                  # noqa: BLE001 — сумнів = не беремо
        return True


def clean(value):
    """Скаляр доказу: логічне, число, короткий рядок без номера; інше — None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > MAX_TEXT or _has_phone(text):
            return None
        return text
    return None                                        # dict/list/None — не беремо


def _walk(obj, parts: list[str]):
    for part in parts:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def path_value(obj: dict, path: str, deref=None):
    """Значення шляху config/seller.toml: «a.b» — скаляр, «a?» — наявність (bool),
    «a[].b» — чи є хоч один елемент списку a з істинним b (bool)."""
    presence = path.endswith("?")
    raw_path = path.rstrip("?")
    if "[]" in raw_path:
        head, tail = raw_path.split("[]", 1)
        items = _walk(obj, head.split(".")) if head else None
        rest = [p for p in tail.lstrip(".").split(".") if p]
        if not isinstance(items, list):
            return None
        return any(bool(_walk(x, rest)) is True for x in items if isinstance(x, dict))
    raw = _walk(obj, raw_path.split("."))
    if deref is not None and isinstance(raw, str):
        raw = deref(raw)
    if presence:
        return bool(raw) if raw is not None else False
    return clean(raw)


def from_feed_item(obj: dict, feed_cfg, *, day: str | None = None, deref=None) -> dict:
    """seller_evidence з об'єкта стрічки: поля білого списку + позначка «бачили»."""
    out: dict = {}
    for key, path in feed_cfg.fields.items():
        value = path_value(obj, path, deref)
        if value is not None:
            out[key] = value
    out[feed_cfg.checked_key] = day or today()
    return out


def opaque_id(prefix: str, slug: str | None, profile_cfg) -> str | None:
    """Префікс + blake2b(slug) — непрозорий id профілю; None — slug порожній чи схожий
    на номер телефону (понад max_digits цифр)."""
    if not slug or not isinstance(slug, str):
        return None
    slug = slug.strip().lower()
    if not slug or sum(ch.isdigit() for ch in slug) > profile_cfg.max_digits:
        return None
    digest = hashlib.blake2b(slug.encode("utf-8"), digest_size=profile_cfg.hash_bytes)
    return f"{prefix}{digest.hexdigest()}"


# --- OLX ------------------------------------------------------------------------------------


def olx_leaves(box, rx) -> tuple[dict[str, str], list[str]]:
    """Листові вузли контейнера параметрів OLX: пари «Ключ: Значення» (`rx` — той самий
    вираз, що в збору, sources/olx._PARAM_RE) і голі тексти (чип «Приватна особа»/
    «Бізнес», «Без комісії»)."""
    params: dict[str, str] = {}
    bare: list[str] = []
    if box is None:
        return params, bare
    for node in box.find_all(["p", "li", "span"]):
        if node.find(["p", "li", "span"]):
            continue
        text = node.get_text(" ", strip=True)
        m = rx.match(text)
        if m:
            params.setdefault(m.group(1).strip().lower(), m.group(2).strip())
        elif text:
            bare.append(text)
    return params, bare


def _olx_profile(soup, olx_cfg, profile_cfg) -> str | None:
    for a in soup.select(olx_cfg.profile_selector):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        for prefix, pattern in olx_cfg.profile_patterns.items():
            m = re.search(pattern, href)
            if not m:
                continue
            slug = m.group(1)
            if prefix.endswith(":shop:") and slug in ("www", "m"):
                continue                               # сам OLX, а не магазин
            return opaque_id(prefix, slug, profile_cfg)
    return None


def from_olx_detail(soup, params: dict, bare: list[str], cfg, *, day: str | None = None) -> dict:
    """{"seller_evidence": …, "seller_profile": …} зі сторінки оголошення OLX."""
    olx = cfg.olx
    ev: dict = {}
    for text in bare:
        code = olx.chip_values.get(text)
        if code is not None and olx.chip_key not in ev:
            ev[olx.chip_key] = code
        flag = olx.flag_values.get(text)
        if flag is not None:
            ev[flag] = True
    deal = params.get(olx.deal_param)
    if deal:
        codes = sorted({olx.deal_values[v.strip()] for v in deal.split(",")
                        if v.strip() in olx.deal_values})
        if codes:
            ev[olx.deal_key] = codes
    ev[olx.checked_key] = day or today()
    out = {"seller_evidence": ev}
    profile = _olx_profile(soup, olx, cfg.profile)
    if profile:
        out["seller_profile"] = profile
    return out


def olx_tab_page(soup, tab: str, cfg) -> dict:
    """Сторінка вкладки пошуку OLX: {"active": bool (активна саме ця вкладка),
    "keys": [olx:<токен>…] (без просуваних), "promoted": n, "next": bool}."""
    from .. import links

    tabs = cfg.olx_tabs
    want = tabs.labels[tab]
    active = [b.get_text(" ", strip=True) for b in soup.select(tabs.active_selector)]
    keys, promoted = [], 0
    for card in soup.select(tabs.card_selector):
        a = card.select_one("a[href]")
        if a is None:
            continue
        href = a.get("href") or ""
        query = urlsplit(href).query.lower()
        if any(reason in query for reason in tabs.skip_card_reasons):
            promoted += 1
            continue
        if href.startswith("/"):
            href = "https://www.olx.ua" + href
        try:
            key = links.site_key(href)
        except Exception:                              # noqa: BLE001 — не наш ключ
            key = None
        if key and key.startswith("olx:") and key not in keys:
            keys.append(key)
    nxt = soup.select_one('[data-testid="pagination-forward"]') is not None
    return {"active": want in active, "active_labels": active, "keys": keys,
            "promoted": promoted, "next": nxt}


# --- rieltor.ua -----------------------------------------------------------------------------


def from_rieltor_detail(soup, cfg, *, day: str | None = None) -> dict:
    """seller_evidence з картки rieltor.ua: роль (білий список), агенція (наявність і
    непрозорий id), позначка «картку бачили». Ім'я агента не читається ніколи."""
    r = cfg.rieltor
    ev: dict = {}
    el = soup.select_one(r.role_selector)
    if el is not None:
        text = " ".join(el.get_text(" ", strip=True).split())
        if text:
            ev[r.role_key] = text if text in r.role_values else r.role_other
    link = soup.select_one(r.agency_selector)
    ev[r.has_agency_key] = link is not None
    if link is not None:
        href = (link.get("href") or "").strip()
        slug = None
        try:
            parts = urlsplit(href)
        except ValueError:
            parts = None
        if parts is not None and parts.netloc:
            host = parts.netloc.lower().split(":")[0]
            if host.endswith(".rieltor.ua") and host.count(".") >= 2:
                slug = host[: -len(".rieltor.ua")]
            elif parts.path.strip("/"):
                slug = parts.path.strip("/")
        elif href.strip("/"):
            slug = href.strip("/")
        agency = opaque_id(r.agency_prefix, slug, cfg.profile) if slug not in (None, "www") \
            else None
        if agency:
            ev[r.agency_key] = agency
    ev[r.checked_key] = day or today()
    return ev


# --- Злиття «лише нові ключі» (для записів джерела й enrich) -------------------------------


def merge_new(old: dict | None, new: dict | None) -> dict | None:
    """Доказ: до наявного — лише ключі, яких там ще немає (значення не переписуються)."""
    if not new:
        return old
    merged = dict(old or {})
    for k, v in new.items():
        if k not in merged and v not in (None, "", [], {}):
            merged[k] = v
    return merged
