"""Доступ ззовні через Cloudflare Tunnel.

Два режими, вибір — лише з `.env`, код і юніти однакові:

  * тимчасовий (зараз): `PUBLIC_DOMAIN` порожній → quick tunnel без акаунта й
    домену. Cloudflare видає адресу `*.trycloudflare.com` одразу, але НОВУ після
    кожного перезапуску. Тому адресу читаємо з виводу cloudflared, кладемо в
    `data/public_url` і надсилаємо в Telegram, щойно вона змінилась;
  * постійний (коли буде домен): `PUBLIC_DOMAIN=…` і `CLOUDFLARE_TUNNEL_TOKEN=…`
    → іменований тунель. Жодна інша частина системи домену не знає: заміна
    значень у `.env` і перезапуск служби.

Без логіна й пароля тунель не піднімається взагалі: відкрити базу в інтернет
без захисту — гірше, ніж не відкрити.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import notify
from .config import DATA_DIR

log = logging.getLogger(__name__)

PUBLIC_URL_PATH = DATA_DIR / "public_url"
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
EX_CONFIG = 78          # код «неправильна конфігурація»: systemd не перезапускає


def find_binary() -> str | None:
    explicit = os.getenv("CLOUDFLARED_BIN", "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    local = Path.home() / ".local" / "bin" / "cloudflared"
    if local.exists():
        return str(local)
    return shutil.which("cloudflared")


def plan(env: dict | None = None) -> dict:
    """Що і як запускати — чиста функція, щоб перевірити без мережі."""
    env = dict(os.environ if env is None else env)
    port = env.get("PORT", "8000")
    domain = env.get("PUBLIC_DOMAIN", "").strip().removeprefix("https://").strip("/")
    token = env.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    if not (env.get("AUTH_USER", "").strip() and env.get("AUTH_PASSWORD", "").strip()):
        return {"error": "AUTH_USER і AUTH_PASSWORD не задані в .env — без пароля сайт "
                         "в інтернет не відкриваю"}
    if domain and not token:
        return {"error": "PUBLIC_DOMAIN задано, а CLOUDFLARE_TUNNEL_TOKEN — ні: для "
                         "постійного домену потрібен іменований тунель"}
    if domain:
        # Токен — через оточення, не аргументом: аргументи видно в `ps`.
        return {"mode": "named", "args": ["tunnel", "--no-autoupdate", "run"],
                "env": {"TUNNEL_TOKEN": token}, "url": f"https://{domain}"}
    return {"mode": "quick",
            "args": ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            "env": {}, "url": None}


def remember(url: str) -> bool:
    """Записує адресу; True — якщо вона нова."""
    old = PUBLIC_URL_PATH.read_text().strip() if PUBLIC_URL_PATH.exists() else ""
    if old == url:
        return False
    PUBLIC_URL_PATH.write_text(url + "\n")
    return True


def announce(url: str, mode: str) -> None:
    if not notify.configured():
        log.warning("адреса змінилась (%s), але Telegram не налаштовано", url)
        return
    note = ("тимчасова адреса Cloudflare — після перезавантаження машини вона "
            "зміниться, і я надішлю нову" if mode == "quick" else "постійна адреса")
    try:
        notify.send_message(f"🌐 Сайт доступний: {url}\n({note}; вхід — логін і пароль з .env)")
    except notify.NotifyError as e:
        log.error("не вдалось надіслати адресу: %s", e)


def run() -> int:
    p = plan()
    if "error" in p:
        log.error(p["error"])
        return EX_CONFIG
    binary = find_binary()
    if not binary:
        log.error("cloudflared не знайдено (~/.local/bin/cloudflared або PATH)")
        return EX_CONFIG
    if p["url"] and remember(p["url"]):
        announce(p["url"], p["mode"])
    log.info("тунель: режим %s", p["mode"])
    proc = subprocess.Popen([binary, *p["args"]], env={**os.environ, **p["env"]},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1)
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if p["mode"] == "quick" and (m := QUICK_URL.search(line)):
                url = m.group(0)
                if remember(url):
                    log.info("нова адреса: %s", url)
                    announce(url, "quick")
    finally:
        code = proc.wait()
    log.warning("cloudflared завершився з кодом %s", code)
    # Ненульовий код — systemd перезапустить; нульовий теж не нормальний стан.
    return code or 1
