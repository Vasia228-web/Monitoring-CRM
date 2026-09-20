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
import time
import sys
from pathlib import Path

import httpx

from . import notify
from .config import DATA_DIR

log = logging.getLogger(__name__)

PUBLIC_URL_PATH = DATA_DIR / "public_url"
# `api.trycloudflare.com` — службова адреса самого Cloudflare: вона трапляється
# в тексті ПОМИЛКИ («failed to request quick Tunnel: Post https://api…»), і
# перша версія виразу прийняла її за адресу сайту.
QUICK_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
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
    """Що і як запускати — чиста функція, щоб перевірити без мережі.

    Іменований тунель піднімається двома способами, залежно від того, як
    видано доступ до акаунта Cloudflare:
      * `CLOUDFLARE_TUNNEL_TOKEN` — токен, створений у панелі;
      * `CLOUDFLARE_TUNNEL_NAME` — ім'я тунелю, створеного на цій машині після
        `cloudflared tunnel login` (облікові дані лежать у ~/.cloudflared).
    Домен в обох випадках — лише значення `PUBLIC_DOMAIN`.
    """
    env = dict(os.environ if env is None else env)
    port = env.get("PORT", "8000")
    local = f"http://127.0.0.1:{port}"
    domain = env.get("PUBLIC_DOMAIN", "").strip().removeprefix("https://").strip("/")
    token = env.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    name = env.get("CLOUDFLARE_TUNNEL_NAME", "").strip()
    if not (env.get("AUTH_USER", "").strip() and env.get("AUTH_PASSWORD", "").strip()):
        return {"error": "AUTH_USER і AUTH_PASSWORD не задані в .env — без пароля сайт "
                         "в інтернет не відкриваю"}
    if domain and not (token or name):
        return {"error": "PUBLIC_DOMAIN задано, але немає ні CLOUDFLARE_TUNNEL_TOKEN, "
                         "ні CLOUDFLARE_TUNNEL_NAME: для постійного домену потрібен "
                         "іменований тунель"}
    if token:
        # Токен — через оточення, не аргументом: аргументи видно в `ps`.
        return {"mode": "named", "args": ["tunnel", "--no-autoupdate", "run"],
                "env": {"TUNNEL_TOKEN": token}, "url": f"https://{domain}" if domain else None}
    if name:
        return {"mode": "named",
                "args": ["tunnel", "--no-autoupdate", "run", "--url", local, name],
                "env": {}, "url": f"https://{domain}" if domain else None}
    return {"mode": "quick",
            "args": ["tunnel", "--no-autoupdate", "--url", local],
            "env": {}, "url": None}


def responds(url: str, attempts: int = 3) -> bool:
    """Чи справді сайт відповідає за цією адресою.

    Адресу оголошуємо лише після перевірки: повідомлення з непрацюючим
    посиланням гірше за його відсутність. `/healthz` відкритий без пароля,
    тому 200 — це саме наш сервіс.
    """
    for i in range(attempts):
        try:
            r = httpx.get(url.rstrip("/") + "/healthz", timeout=15,
                          follow_redirects=True)
            if r.status_code == 200:
                return True
            log.warning("адреса %s відповіла %s", url, r.status_code)
        except Exception as e:
            log.warning("адреса %s ще не відповідає (%s)", url, type(e).__name__)
        if i + 1 < attempts:
            time.sleep(5)
    return False


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
    current = None
    # Ознака, що тимчасовий тунель у цій мережі неможливий: до Cloudflare
    # не достукатись. Перезапуски тут не допоможуть — потрібен домен.
    unreachable = False
    proc = subprocess.Popen([binary, *p["args"]], env={**os.environ, **p["env"]},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1)
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if "failed to request quick Tunnel" in line:
                unreachable = True
            if p["mode"] == "quick" and (m := QUICK_URL.search(line)):
                url = m.group(0)
                if url != current and responds(url):
                    current = url
                    if remember(url):
                        log.info("нова адреса: %s", url)
                        announce(url, "quick")
    finally:
        code = proc.wait()
    log.warning("cloudflared завершився з кодом %s", code)
    if unreachable:
        text = ("Тимчасова адреса Cloudflare недоступна з цієї мережі: "
                "trycloudflare.com не відкривається (перевірено з обох машин, "
                "сам Cloudflare працює). Перезапуски не допоможуть — потрібен "
                "домен і іменований тунель; точки входу для нього відкриті.")
        log.error(text)
        if notify.configured():
            try:
                notify.send_message("🚫 " + text)
            except notify.NotifyError:
                pass
        # 78 — конфігурація/мережа: systemd не крутить перезапуски по колу.
        return EX_CONFIG
    # Ненульовий код — systemd перезапустить; нульовий теж не нормальний стан.
    return code or 1
