"""Гачки доказів Блоків 3 і 4 на тілі відповіді перевірки (інтеграція, конфлікт 3; E8, D52).

Один запит на ключ: сторінку DOM.RIA перевірка вже завантажила й розібрала — з того
самого стану беремо сирі докази для майбутніх блоків «тип продавця» (seller_evidence,
seller_profile) і «район/ЖК» (place_raw). Білий список шляхів — config/liveness.toml
[capture]; лише скалярні значення (рядок, число, логічне) — вкладені об'єкти, імена,
телефони не беруться ніколи. Записує apply.py ЛИШЕ туди, де порожньо.
"""
from __future__ import annotations


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
    cap = cfg.capture

    def hook(item, result, verdict):
        extra = verdict.extra or {}
        realty, data = extra.get("ria_realty"), extra.get("ria_data")
        if not isinstance(realty, dict):
            return None
        roots = {"realty": realty, "data": data if isinstance(data, dict) else {}}
        out: dict = {}
        place = {k: v for k, p in cap.ria_place.items()
                 if (v := _value(roots, p)) not in (None, "")}
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


def default_hooks(cfg) -> list:
    if not cfg.capture.enabled:
        return []
    return [ria_state_hook(cfg)]
