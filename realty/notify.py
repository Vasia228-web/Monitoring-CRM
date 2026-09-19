"""Повідомлення в Telegram.

Токен і чат беруться лише з оточення (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
— у коді й репозиторії їх немає. Токен входить в URL запиту до Telegram, тому
дві обережності:
  * httpx пише URL у свій журнал на рівні INFO — його логер тут глушимо;
  * текст будь-якої помилки проходить через `_mask`, перш ніж потрапити в лог
    чи виняток.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
MESSAGE_LIMIT = 4000          # Telegram обрізає на 4096; лишаємо запас
DOCUMENT_LIMIT = 49 * 1024 * 1024   # бот може надіслати файл до 50 МБ


class NotifyError(RuntimeError):
    pass


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def configured() -> bool:
    return bool(_token() and _chat())


def _mask(text: str) -> str:
    token = _token()
    return text.replace(token, "***") if token else text


def _call(method: str, timeout: float, **kwargs) -> dict:
    if not configured():
        raise NotifyError("Telegram не налаштовано: немає TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID")
    url = f"{API_BASE}/bot{_token()}/{method}"
    try:
        r = httpx.post(url, timeout=timeout, **kwargs)
        data = r.json()
    except Exception as e:
        raise NotifyError(_mask(f"{type(e).__name__}: {e}")) from None
    if not data.get("ok"):
        raise NotifyError(_mask(f"Telegram відповів {r.status_code}: "
                                f"{data.get('description', data)}"))
    return data["result"]


def send_message(text: str, silent: bool = False) -> int:
    """Надсилає текст. Повертає message_id — доказ, що Telegram його прийняв."""
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT - 1] + "…"
    result = _call("sendMessage", timeout=30, data={
        "chat_id": _chat(), "text": text, "disable_notification": silent,
        "disable_web_page_preview": True,
    })
    return int(result["message_id"])


def send_document(path: Path, caption: str = "") -> int:
    size = path.stat().st_size
    if size > DOCUMENT_LIMIT:
        raise NotifyError(f"{path.name}: {size / 1e6:.1f} МБ — більше, ніж бот може надіслати")
    with path.open("rb") as fh:
        # Великий файл по Wi-Fi може йти хвилину-дві; ліміт є, просто ширший.
        result = _call("sendDocument", timeout=300,
                       data={"chat_id": _chat(), "caption": caption[:1000],
                             "disable_notification": True},
                       files={"document": (path.name, fh, "application/octet-stream")})
    return int(result["message_id"])
