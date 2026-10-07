"""Заборона зовнішньої мережі — для тестів і офлайн-інструментів.

Навіщо. 07.10.2026 (D45, інцидент 1) тестовий Chromium сам довантажив ~77
ресурсів із CDN джерела під час справжнього циклу збору: ніхто не просив його
ходити в мережу, але й ніщо не заважало. Тести й офлайн-скрипти (наприклад,
порівняння сторінок «до/після») працюють лише з локальними копіями, тож будь-яка
спроба вийти за межі цієї машини — помилка, а не «повільний тест».

Що блокує `install()`:
  * `socket.connect` / `connect_ex` / `sendto` на адресу, що не є loopback
    (127.0.0.0/8, ::1); unix-сокети дозволені;
  * `socket.create_connection` і розв'язання імен (`getaddrinfo`,
    `gethostbyname`) для будь-якого імені, крім localhost;
  * запит httpx на нелокальний хост — ще до розв'язання імені, з адресою в
    повідомленні;
  * запуск браузера Playwright, якщо його не дозволено явно (`permit(browser=True)`).
    Дозволений браузер однаково замкнений на цій машині: розв'язання імен у
    ньому вимкнене аргументом запуску, а кожен його запит на нелокальну адресу
    обривається й записується.

Кожна спроба записується в журнал (`attempts()`), навіть якщо код, що її
зробив, проковтнув виняток через `except Exception` (так робить, наприклад,
запит курсу НБУ). Обгортка тестів дивиться в журнал і валить тест — тихого
обходу немає. Виняток навмисно НЕ OSError: інакше httpx і цикли повторів
сприйняли б його як звичайний обрив мережі й пішли б по колу.

Застосунок заборону не вмикає: у роботі мережа потрібна. `cli.py` лише
викликає `install_from_env()`, яка діє тільки за REALTY_NETGUARD=1 — цю змінну
ставить conftest для дочірніх процесів тестів, у службах її немає.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

# Дочірні процеси тестів (наприклад, штучне джерело в test_timeouts) вмикають
# ту саму заборону за цими змінними й дописують спроби у файл журналу.
ENV_FLAG = "REALTY_NETGUARD"
ENV_LOG = "REALTY_NETGUARD_LOG"

# Схеми, які не виходять у мережу: вбудовані сторінки, файли, службові.
_LOCAL_SCHEMES = {"data", "about", "blob", "file", "chrome", "chrome-error",
                  "chrome-extension", "devtools", "javascript"}
# Аргумент Chromium: жодне ім'я чи адреса, крім loopback, не розв'язується
# (правило MAP * діє й на IP-літерали). Друга лінія — перехоплення запитів нижче.
BROWSER_ARGS = ("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, "
                "EXCLUDE 127.0.0.1, EXCLUDE [::1]",)


class NetworkBlocked(RuntimeError):
    """Спроба вийти в зовнішню мережу там, де це заборонено."""


_lock = threading.Lock()
_attempts: list[str] = []
_allow_network = False
_allow_browser = False
_installed = False
# Файл журналу пишуть лише дочірні процеси: батько бачить свої спроби в пам'яті.
_child_log: str | None = None


# --- Що вважається «локальним» ----------------------------------------------------------


def is_loopback_host(host) -> bool:
    """Ім'я або адреса, що не виходить за межі машини.

    None і порожній рядок означають «ця машина» (пасивний bind), localhost —
    теж; IP — лише loopback, включно з IPv4, загорнутим в IPv6.
    """
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    h = str(host).strip().strip("[]").lower()
    if h in ("", "localhost", "localhost.localdomain") or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def is_local_url(url: str) -> bool:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in _LOCAL_SCHEMES:
        return True
    if scheme in ("http", "https", "ws", "wss"):
        return is_loopback_host(parts.hostname)
    return False


# --- Журнал і дозволи ---------------------------------------------------------------------


def _record(kind: str, target: str) -> NetworkBlocked:
    entry = f"{kind}: {target}"
    with _lock:
        _attempts.append(entry)
    if _child_log:
        try:
            with open(_child_log, "a", encoding="utf-8") as fh:
                fh.write(f"[pid {os.getpid()}] {entry}\n")
        except OSError:
            pass
    return NetworkBlocked(
        f"Зовнішня мережа заборонена ({entry}). Тести й офлайн-інструменти працюють "
        f"лише з 127.0.0.1; свідомий виняток — позначка @pytest.mark.allow_network.")


def attempts() -> list[str]:
    """Усі заблоковані спроби з початку процесу (копія)."""
    with _lock:
        return list(_attempts)


def mark() -> int:
    """Позиція в журналі — щоб потім забрати лише нові записи."""
    with _lock:
        return len(_attempts)


def take_since(position: int) -> list[str]:
    """Забирає з журналу записи після `position` і повертає їх."""
    with _lock:
        new = _attempts[position:]
        del _attempts[position:]
        return new


@contextmanager
def expect_blocked():
    """Для тестів самої заборони: спроби всередині блоку очікувані.

    Вони переносяться з журналу в повернений список, тож обгортка тесту їх уже
    не бачить, а тест може перевірити, що саме було заблоковано.
    """
    caught: list[str] = []
    start = mark()
    try:
        yield caught
    finally:
        caught.extend(take_since(start))


@contextmanager
def permit(*, network: bool = False, browser: bool = False):
    """Тимчасові дозволи на час одного тесту (позначки allow_network/allow_browser)."""
    global _allow_network, _allow_browser
    before = (_allow_network, _allow_browser)
    _allow_network, _allow_browser = network, (browser or network)
    try:
        yield
    finally:
        _allow_network, _allow_browser = before


def _network_allowed() -> bool:
    return _allow_network


# --- Сокети ---------------------------------------------------------------------------------


def _inet_target(sock, address):
    """(хост, порт) для TCP/UDP-адрес; None — для unix та інших сімейств."""
    family = getattr(sock, "family", None)
    if family not in (socket.AF_INET, socket.AF_INET6):
        return None
    if isinstance(address, tuple) and address:
        return address[0], (address[1] if len(address) > 1 else None)
    return None


def _check_sock(kind: str, sock, address) -> None:
    target = _inet_target(sock, address)
    if target is None or _network_allowed() or is_loopback_host(target[0]):
        return
    raise _record(kind, f"{target[0]}:{target[1]}")


def _check_host(kind: str, host, port=None) -> None:
    if _network_allowed() or is_loopback_host(host):
        return
    raise _record(kind, f"{host}" + (f":{port}" if port is not None else ""))


def _patch(owner, name: str, make) -> None:
    setattr(owner, name, make(getattr(owner, name)))


def _install_sockets() -> None:
    def connect(orig):
        def guarded(self, address):
            _check_sock("socket.connect", self, address)
            return orig(self, address)
        return guarded

    def connect_ex(orig):
        def guarded(self, address):
            _check_sock("socket.connect_ex", self, address)
            return orig(self, address)
        return guarded

    def sendto(orig):
        def guarded(self, data, *args):
            if args:
                _check_sock("socket.sendto", self, args[-1])
            return orig(self, data, *args)
        return guarded

    def create_connection(orig):
        def guarded(address, *args, **kwargs):
            if isinstance(address, tuple) and address:
                _check_host("socket.create_connection", address[0],
                            address[1] if len(address) > 1 else None)
            return orig(address, *args, **kwargs)
        return guarded

    def getaddrinfo(orig):
        def guarded(host, port, *args, **kwargs):
            _check_host("dns", host, port)
            return orig(host, port, *args, **kwargs)
        return guarded

    def gethostbyname(orig):
        def guarded(host, *args, **kwargs):
            _check_host("dns", host)
            return orig(host, *args, **kwargs)
        return guarded

    _patch(socket.socket, "connect", connect)
    _patch(socket.socket, "connect_ex", connect_ex)
    _patch(socket.socket, "sendto", sendto)
    _patch(socket, "create_connection", create_connection)
    _patch(socket, "getaddrinfo", getaddrinfo)
    _patch(socket, "gethostbyname", gethostbyname)
    _patch(socket, "gethostbyname_ex", gethostbyname)


# --- httpx ----------------------------------------------------------------------------------


def _install_httpx() -> None:
    try:
        import httpx
    except ImportError:                                   # pragma: no cover
        return

    def sync(orig):
        def guarded(self, request):
            _check_url("httpx", str(request.url))
            return orig(self, request)
        return guarded

    def asyn(orig):
        async def guarded(self, request):
            _check_url("httpx", str(request.url))
            return await orig(self, request)
        return guarded

    _patch(httpx.HTTPTransport, "handle_request", sync)
    _patch(httpx.AsyncHTTPTransport, "handle_async_request", asyn)


def _check_url(kind: str, url: str) -> None:
    if _network_allowed() or is_local_url(url):
        return
    raise _record(kind, url)


# --- Playwright -----------------------------------------------------------------------------


def _with_browser_args(kwargs: dict) -> dict:
    out = dict(kwargs)
    out["args"] = [*(out.get("args") or []), *BROWSER_ARGS]
    return out


def _route_sync(route) -> None:
    url = route.request.url
    if _network_allowed() or is_local_url(url):
        route.continue_()
        return
    _record("browser", url)
    route.abort("blockedbyclient")


async def _route_async(route) -> None:
    url = route.request.url
    if _network_allowed() or is_local_url(url):
        await route.continue_()
        return
    _record("browser", url)
    await route.abort("blockedbyclient")


def _install_playwright() -> None:
    try:
        from playwright import async_api, sync_api
    except ImportError:                                   # pragma: no cover
        return

    def refuse(what: str) -> NetworkBlocked:
        return _record("playwright", f"{what} (браузер у тестах — лише з "
                                     f"@pytest.mark.allow_browser)")

    # --- синхронний API ---
    def s_launch(orig):
        def guarded(self, *args, **kwargs):
            if not (_allow_browser or _network_allowed()):
                raise refuse("launch")
            if not _network_allowed():
                kwargs = _with_browser_args(kwargs)
            return orig(self, *args, **kwargs)
        return guarded

    def s_persistent(orig):
        def guarded(self, *args, **kwargs):
            if not (_allow_browser or _network_allowed()):
                raise refuse("launch_persistent_context")
            if not _network_allowed():
                kwargs = _with_browser_args(kwargs)
            ctx = orig(self, *args, **kwargs)
            ctx.route("**/*", _route_sync)
            return ctx
        return guarded

    def s_refuse(name):
        def make(orig):
            def guarded(self, *args, **kwargs):
                if not _network_allowed():
                    raise refuse(name)
                return orig(self, *args, **kwargs)
            return guarded
        return make

    def s_new_context(orig):
        def guarded(self, *args, **kwargs):
            ctx = orig(self, *args, **kwargs)
            ctx.route("**/*", _route_sync)
            return ctx
        return guarded

    def s_new_page(orig):
        def guarded(self, *args, **kwargs):
            page = orig(self, *args, **kwargs)
            page.route("**/*", _route_sync)
            return page
        return guarded

    bt = sync_api.BrowserType
    _patch(bt, "launch", s_launch)
    _patch(bt, "launch_persistent_context", s_persistent)
    _patch(bt, "connect", s_refuse("connect"))
    _patch(bt, "connect_over_cdp", s_refuse("connect_over_cdp"))
    _patch(sync_api.Browser, "new_context", s_new_context)
    _patch(sync_api.Browser, "new_page", s_new_page)

    # --- асинхронний API ---
    def a_launch(orig):
        async def guarded(self, *args, **kwargs):
            if not (_allow_browser or _network_allowed()):
                raise refuse("launch")
            if not _network_allowed():
                kwargs = _with_browser_args(kwargs)
            return await orig(self, *args, **kwargs)
        return guarded

    def a_persistent(orig):
        async def guarded(self, *args, **kwargs):
            if not (_allow_browser or _network_allowed()):
                raise refuse("launch_persistent_context")
            if not _network_allowed():
                kwargs = _with_browser_args(kwargs)
            ctx = await orig(self, *args, **kwargs)
            await ctx.route("**/*", _route_async)
            return ctx
        return guarded

    def a_refuse(name):
        def make(orig):
            async def guarded(self, *args, **kwargs):
                if not _network_allowed():
                    raise refuse(name)
                return await orig(self, *args, **kwargs)
            return guarded
        return make

    def a_new_context(orig):
        async def guarded(self, *args, **kwargs):
            ctx = await orig(self, *args, **kwargs)
            await ctx.route("**/*", _route_async)
            return ctx
        return guarded

    def a_new_page(orig):
        async def guarded(self, *args, **kwargs):
            page = await orig(self, *args, **kwargs)
            await page.route("**/*", _route_async)
            return page
        return guarded

    abt = async_api.BrowserType
    _patch(abt, "launch", a_launch)
    _patch(abt, "launch_persistent_context", a_persistent)
    _patch(abt, "connect", a_refuse("connect"))
    _patch(abt, "connect_over_cdp", a_refuse("connect_over_cdp"))
    _patch(async_api.Browser, "new_context", a_new_context)
    _patch(async_api.Browser, "new_page", a_new_page)


# --- Вмикання -------------------------------------------------------------------------------


def install() -> None:
    """Вмикає заборону в цьому процесі (повторний виклик нічого не робить)."""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    _install_sockets()
    _install_httpx()
    _install_playwright()


def installed() -> bool:
    return _installed


def install_from_env() -> bool:
    """Для дочірніх процесів тестів: вмикає заборону, якщо її ввімкнув батько."""
    global _child_log
    if os.environ.get(ENV_FLAG) == "1":
        _child_log = os.environ.get(ENV_LOG) or None
        install()
        return True
    return False
