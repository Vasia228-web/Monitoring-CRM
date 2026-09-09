"""Базова авторизація для всього інтерфейсу.

Панель із зібраними даними й ручними тригерами краулера не має бути відкритою.
Логін і пароль беруться лише зі змінних оточення — у репозиторії їх немає.
Якщо змінні не задані, захист вимкнено: це режим локальної розробки, і про
нього виводиться попередження при старті.
"""
from __future__ import annotations

import logging
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

log = logging.getLogger(__name__)

REALM = "Realty Monitor"
# Шляхи, доступні без пароля: перевірка живучості для хостингу й robots.
OPEN_PATHS = {"/healthz", "/robots.txt"}


def credentials() -> tuple[str, str] | None:
    user = os.getenv("AUTH_USER", "").strip()
    password = os.getenv("AUTH_PASSWORD", "").strip()
    return (user, password) if user and password else None


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Пропускає далі лише з правильним логіном і паролем."""

    async def dispatch(self, request, call_next):
        creds = credentials()
        if creds is None or request.url.path in OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            import base64

            try:
                raw = base64.b64decode(header[6:]).decode("utf-8")
                user, _, password = raw.partition(":")
            except (ValueError, UnicodeDecodeError):
                user = password = ""
            # Порівняння сталого часу: інакше пароль можна підібрати за
            # тривалістю відповіді. Порівнюємо байти, бо compare_digest не
            # приймає не-ASCII — інакше пароль із кирилицею давав би 500.
            if (secrets.compare_digest(user.encode(), creds[0].encode())
                    and secrets.compare_digest(password.encode(), creds[1].encode())):
                return await call_next(request)

        return Response(status_code=401, content="Потрібна авторизація",
                        headers={"WWW-Authenticate": f'Basic realm="{REALM}"'})


def robots_txt() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def warn_if_open() -> None:
    if credentials() is None:
        log.warning("AUTH_USER/AUTH_PASSWORD не задані — інтерфейс відкритий. "
                    "Для публічного хостингу це обов'язково.")
