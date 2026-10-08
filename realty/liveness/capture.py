"""Гачки доказів Блоків 3 і 4 на тілі відповіді перевірки (інтеграція, конфлікт 3; E8, D52).

Один запит на ключ: сторінку DOM.RIA перевірка вже завантажила й розібрала — з того
самого стану беремо сирі докази для майбутніх блоків «тип продавця» (seller_evidence,
seller_profile) і «район/ЖК» (place_raw). Білий список шляхів — config/liveness.toml
[capture]; лише скалярні значення (рядок, число, логічне) — вкладені об'єкти, імена,
телефони не беруться ніколи. Записує apply.py ЛИШЕ туди, де порожньо.

E11 (D60): уночі ключ хоста з capture.body_hosts (rieltor.ua), якому бракує доказів,
смуга питає GET замість HEAD (вердикт — той самий, за кодом; engine.check_one), а тіло
віддає `body_hook`: картка rieltor — роль і агенція (Блок 3, config/seller.toml
[rieltor]), блок ЖК (Блок 4). Жодного зайвого запиту.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _walk(root: dict, parts: list[str]):
    obj = root
    for part in parts:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _value(roots: dict, path: str):
    presence = path.endswith("?")
    parts = path.rstrip("?").split(".")
    raw = _walk(roots.get(parts[0]) or {}, parts[1:])
    if presence:
        return bool(raw) if raw is not None else False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        # id 0 у стані DOM.RIA — «немає» (district_id = 0 на копії): записане «лише
        # туди, де порожньо» заступило б справжнє значення назавжди.
        if raw == 0 and parts[-1].endswith("_id"):
            return None
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        return raw[:200] if raw else None
    return None                                   # dict/list/None — не беремо


def ria_state_hook(cfg):
    """Гачок для сторінок DOM.RIA: place_raw, seller_evidence, seller_profile."""
    from ..places import extract as place_extract

    cap = cfg.capture

    def hook(item, result, verdict):
        extra = verdict.extra or {}
        realty, data = extra.get("ria_realty"), extra.get("ria_data")
        if not isinstance(realty, dict):
            return None
        roots = {"realty": realty, "data": data if isinstance(data, dict) else {}}
        out: dict = {}
        # Рядки місця (назва району, новобудови) — без номера телефону (рецензія E11,
        # 08.10: слухачі ORM чистять лише опис і заголовок).
        place = {k: v for k, p in cap.ria_place.items()
                 if (v := _value(roots, p)) not in (None, "")
                 and not (isinstance(v, str) and place_extract.has_phone(v))}
        seller = {k: v for k, p in cap.ria_seller.items()
                  if (v := _value(roots, p)) not in (None, "")}
        if place:
            out["place_raw"] = place
        if seller:
            out["seller_evidence"] = seller
        if cap.ria_profile:
            uid = _value(roots, cap.ria_profile)
            if isinstance(uid, (int, str)) and not isinstance(uid, bool) and str(uid).strip():
                out["seller_profile"] = f"{cap.ria_profile_prefix}{uid}"
        return out or None

    hook.__name__ = "ria_state_hook"
    return hook


def _rieltor_page(body: str, key: str):
    from ..sources import rieltor
    from . import policy as pol

    return rieltor.page_evidence(body, pol.id_of_key(key))


def _rieltor_missing(scfg) -> list[tuple[str, str]]:
    return [("seller_evidence", scfg.rieltor.checked_key), ("place_raw", "rieltor_checked_at")]


# Хост тіла (capture.body_hosts) → (розбір картки, чого бракує рядку, щоб питати GET).
BODY_EXTRACTORS = {"rieltor.ua": (_rieltor_page, _rieltor_missing)}


def body_hook(cfg):
    """Гачок для тіла нічного GET (capture.body_hosts): лише ЖИВА картка саме цього
    ключа (чужа картка, сторінка 410, капча — нічого)."""
    from .signatures import ALIVE

    def hook(item, result, verdict):
        if item.host not in cfg.capture.body_hosts or not item.body_cap:
            return None
        if verdict.kind != ALIVE or not getattr(result, "body", None):
            return None
        extractor = BODY_EXTRACTORS.get(item.host)
        if extractor is None:
            return None
        return extractor[0](result.body, item.key) or None

    hook.__name__ = "body_hook"
    return hook


def body_need_keys(session, cfg, host: str) -> set[str]:
    """Ключі хоста з capture.body_hosts, рядкам яких бракує доказів (уночі — GET)."""
    from sqlalchemy import func, or_, select

    from ..models import Listing
    from ..seller import evidence

    entry = BODY_EXTRACTORS.get(host)
    spec = cfg.hosts.get(host)
    scfg = evidence.config()
    if entry is None or spec is None or scfg is None:
        return set()
    cond = or_(*[func.json_extract(getattr(Listing, column), f"$.{key}").is_(None)
                 for column, key in entry[1](scfg)])
    return set(session.scalars(select(Listing.site_key).where(
        Listing.site_key.like(f"{spec.family}:%"), cond).distinct()))


def default_hooks(cfg) -> list:
    if not cfg.capture.enabled:
        return []
    hooks = [ria_state_hook(cfg)]
    if cfg.capture.body_hosts:
        hooks.append(body_hook(cfg))
    return hooks
