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
        response = await self._dispatch(request, call_next)
        # Заборона індексації на рівні заголовка — діє і для JSON, і для 401,
        # де мета-тегу сторінки немає.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    async def _dispatch(self, request, call_next):
        creds = credentials()
        if creds is None or request.url.path in OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            import base64

            try:
                raw = base64.b64decode(header[6:]).decode("utf-8")
                user, _, password = raw.partition(":")
                # Клавіатура телефона дописує пробіл після слова, і вхід
                # відмовляв при правильному паролі. Логін обрізаємо, пароль —
                # ніколи: у ньому пробіл може бути навмисним.
                user = user.strip()
            except (ValueError, UnicodeDecodeError):
                user = password = ""
            # Порівняння сталого часу: інакше пароль можна підібрати за
            # тривалістю відповіді. Порівнюємо байти, бо compare_digest не
            # приймає не-ASCII — інакше пароль із кирилицею давав би 500.
            if (secrets.compare_digest(user.encode(), creds[0].encode())
                    and secrets.compare_digest(password.encode(), creds[1].encode())):
                return await call_next(request)

            _explain_mismatch(user, password, creds)

        return Response(status_code=401, content="Потрібна авторизація",
                        headers={"WWW-Authenticate": f'Basic realm="{REALM}"'})


def _explain_mismatch(user: str, password: str, creds: tuple[str, str]) -> None:
    """Каже, ЩО не зійшлося, не кажучи, які саме значення.

    Без цього невдалий вхід з чужого пристрою не діагностується взагалі:
    у журналі лише «401». Тут не друкуються ні логін, ні пароль — лише
    характер розбіжності (регістр, пробіли, довжина).
    """
    want_user, want_password = creds
    def compare(got: str, want: str) -> str:
        if got == want:
            return "збігається"
        if not got:
            return "порожнє"
        if got.strip() == want:
            return "зайві пробіли по краях"
        if got.lower() == want.lower():
            return "інший регістр (на телефоні перша літера часто велика)"
        if len(got) != len(want):
            return f"інша довжина: {len(got)} замість {len(want)}"
        return "не збігається"

    log.warning("невдалий вхід: логін — %s; пароль — %s",
                compare(user, want_user), compare(password, want_password))


def robots_txt() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def warn_if_open() -> None:
    if credentials() is None:
        log.warning("AUTH_USER/AUTH_PASSWORD не задані — інтерфейс відкритий. "
                    "Для публічного хостингу це обов'язково.")
