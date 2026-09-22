"""Сесії входу й блокування за невдалими спробами.

Обидва — у телеметрійній базі (`ops.db`), а не в пам'яті процесу: блокування
мають переживати перезапуск сайту, а «Вийти» має обривати сесію на сервері, а
не лише стирати куку в браузері (вкрадена копія куки після виходу — мертва).

Ключ підпису — випадковий файл `data/session_secret` (права 600, не в git і не
в бекапах). Видалити файл = розлогінити всіх.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import DateTime, Integer, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from .. import ops
from ..config import DATA_DIR

SESSION_DAYS = 30
MAX_FAILURES = 10            # 10 невдалих — блок; 11-та спроба вже відхиляється
BLOCK_MINUTES = 15
WINDOW_MINUTES = 15          # невдалі спроби лічаться в межах цього вікна
TOUCH_EVERY = timedelta(minutes=5)
SECRET_PATH = DATA_DIR / "session_secret"


class AuthSession(ops.OpsBase):
    __tablename__ = "auth_sessions"

    sid: Mapped[str] = mapped_column(String(64), primary_key=True)
    user: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    # Відбиток пароля на момент входу (HMAC із секретом): змінили пароль у .env —
    # старі сесії більше не діють.
    pw_tag: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=ops._now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=ops._now)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)


class AuthBlock(ops.OpsBase):
    """Невдалі спроби з однієї адреси; після MAX_FAILURES — блок на BLOCK_MINUTES."""

    __tablename__ = "auth_blocks"

    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    first_failure_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime)
    country: Mapped[str | None] = mapped_column(String(8))
    user_agent: Mapped[str | None] = mapped_column(Text)
    last_user: Mapped[str | None] = mapped_column(String(128))


# --- Секрет і підписи ------------------------------------------------------------------


def _secret() -> bytes:
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(value)
    return value


def _mac(data: str) -> str:
    return hmac.new(_secret(), data.encode("utf-8"), hashlib.sha256).hexdigest()


def pw_tag(password: str) -> str:
    return _mac("pw:" + password)[:32]


# --- Сесії -------------------------------------------------------------------------------


def create(user: str, role: str, password: str, ip: str | None, ua: str | None) -> str:
    """Нова сесія; повертає значення куки: id сесії + підпис."""
    ops.init_ops()
    sid = secrets.token_urlsafe(32)
    now = ops._now()
    with ops.ops_session() as s:
        s.add(AuthSession(sid=sid, user=user, role=role, pw_tag=pw_tag(password),
                          created_at=now, last_seen=now,
                          expires_at=now + timedelta(days=SESSION_DAYS),
                          ip=ip, user_agent=(ua or "")[:400]))
    return f"{sid}.{_mac('sid:' + sid)[:32]}"


def validate(cookie: str | None, accounts: dict) -> AuthSession | None:
    """Сесія за кукою — або None. Перевіряє підпис, термін, пароль і роль."""
    if not cookie or "." not in cookie:
        return None
    sid, _, sig = cookie.partition(".")
    if not hmac.compare_digest(sig, _mac("sid:" + sid)[:32]):
        return None
    ops.init_ops()
    now = ops._now()
    with ops.ops_session() as s:
        row = s.get(AuthSession, sid)
        if row is None or row.expires_at <= now:
            return None
        acct = accounts.get(row.user)
        # Користувача прибрали з .env, змінили йому пароль чи роль — сесія мертва.
        if acct is None or acct[1] != row.role or \
                not hmac.compare_digest(row.pw_tag, pw_tag(acct[0])):
            s.delete(row)
            return None
        if now - row.last_seen > TOUCH_EVERY:
            row.last_seen = now
        s.expunge(row)
        return row


def destroy(cookie: str | None) -> None:
    if not cookie or "." not in cookie:
        return
    sid = cookie.partition(".")[0]
    ops.init_ops()
    with ops.ops_session() as s:
        s.execute(delete(AuthSession).where(AuthSession.sid == sid))


# --- Блокування --------------------------------------------------------------------------


def blocked(ip: str) -> AuthBlock | None:
    ops.init_ops()
    now = ops._now()
    with ops.ops_session() as s:
        row = s.get(AuthBlock, ip)
        if row and row.blocked_until and row.blocked_until > now:
            s.expunge(row)
            return row
    return None


def register_failure(ip: str, country: str | None, ua: str | None, user: str | None) -> AuthBlock:
    ops.init_ops()
    now = ops._now()
    with ops.ops_session() as s:
        row = s.get(AuthBlock, ip)
        if row is None:
            row = AuthBlock(ip=ip, failures=0)
            s.add(row)
        expired_block = row.blocked_until is not None and row.blocked_until <= now
        stale = row.first_failure_at is not None and \
            now - row.first_failure_at > timedelta(minutes=WINDOW_MINUTES)
        if expired_block or stale:
            row.failures, row.first_failure_at = 0, None
            row.blocked_at = row.blocked_until = None
        row.failures += 1
        row.first_failure_at = row.first_failure_at or now
        row.last_failure_at = now
        row.country = (country or row.country or "")[:8] or None
        row.user_agent = (ua or row.user_agent or "")[:400] or None
        row.last_user = (user or "")[:128] or None
        if row.failures >= MAX_FAILURES and row.blocked_until is None:
            row.blocked_at = now
            row.blocked_until = now + timedelta(minutes=BLOCK_MINUTES)
        s.flush()
        s.expunge(row)
        return row


def register_success(ip: str) -> None:
    unblock(ip)


def unblock(ip: str) -> bool:
    ops.init_ops()
    with ops.ops_session() as s:
        return s.execute(delete(AuthBlock).where(AuthBlock.ip == ip)).rowcount > 0


def active_blocks() -> list[AuthBlock]:
    ops.init_ops()
    now = ops._now()
    with ops.ops_session() as s:
        rows = list(s.scalars(select(AuthBlock).where(AuthBlock.blocked_until > now)
                              .order_by(AuthBlock.blocked_at.desc())))
        for r in rows:
            s.expunge(r)
        return rows


# --- Пристрій і браузер ------------------------------------------------------------------

_DEVICES = (
    (r"iPhone", "iPhone"), (r"iPad", "iPad"), (r"Android", "Android"),
    (r"Macintosh|Mac OS X", "Mac"), (r"Windows", "Windows"), (r"Linux", "Linux"),
)
_BROWSERS = (
    (r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox/|FxiOS/", "Firefox"),
    (r"CriOS/|Chrome/", "Chrome"), (r"Version/.*Safari/", "Safari"),
    (r"curl/", "curl"), (r"python-httpx|python-requests|aiohttp", "скрипт Python"),
    (r"Go-http-client", "скрипт Go"),
)
_BOTS = re.compile(r"bot|crawl|spider|scan|curl|wget|python|go-http|zgrab|masscan", re.I)


def describe_agent(ua: str | None) -> tuple[str, str]:
    """(пристрій, браузер) людською мовою з User-Agent."""
    ua = ua or ""
    device = next((name for pat, name in _DEVICES if re.search(pat, ua)), "невідомо")
    browser = next((name for pat, name in _BROWSERS if re.search(pat, ua)), "невідомо")
    if _BOTS.search(ua) and browser in ("невідомо", "curl", "скрипт Python", "скрипт Go"):
        device = "бот / скрипт"
    return device, browser
