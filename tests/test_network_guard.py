"""Тести не виходять у зовнішню мережу (D45, інцидент 1).

07.10.2026 тестовий Chromium сам довантажив ~77 ресурсів із CDN джерела під
час справжнього циклу збору. Тепер заборона (`realty.netguard`, вмикається в
conftest) діє на сокети, розв'язання імен, httpx і браузер Playwright; кожна
спроба записується, навіть якщо код її проковтнув. Ці тести перевіряють саму
заборону: зовнішнє — падає, локальне — працює.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import netguard  # noqa: E402
from realty.netguard import NetworkBlocked  # noqa: E402

# Адреси, що нікуди не ведуть, навіть якщо заборона колись зламається: інакше
# прогін тестів сам повторив би D45 проти справжнього джерела. 192.0.2.0/24 —
# TEST-NET-1 (RFC 5737); .example — зарезервований домен (RFC 2606). Заборона
# перевіряє адресу ще до розв'язання імені, тож справжній хост для доказу не
# потрібен.
OUTSIDE_IP = "192.0.2.1"
SOURCE_HOST = "dom.ria.example"
OLX_HOST = "www.olx.example"
NBU_URL = "https://bank.gov.example/NBUStatService/v1/statdirectory/exchange"
CDN_PHOTO = "https://cdn.riastatic.example/photo.jpg"
CDN_SCRIPT = "https://dom.riastatic.example/app.js"


class _Page(BaseHTTPRequestHandler):
    """Локальна «збережена сторінка джерела» з ресурсами на чужих CDN — як у D45."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (b"<!doctype html><title>local</title><p>ok</p>"
                b"<img src='" + CDN_PHOTO.encode() + b"'>"
                b"<script src='" + CDN_SCRIPT.encode() + b"'></script>"
                b"<img src='http://" + OUTSIDE_IP.encode() + b"/pixel.gif'>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_guard_is_on_for_the_whole_suite():
    assert netguard.installed()


def test_tcp_connection_to_an_outside_address_raises():
    with netguard.expect_blocked() as caught:
        with pytest.raises(NetworkBlocked):
            socket.create_connection((OUTSIDE_IP, 80), timeout=1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkBlocked):
                s.connect((OUTSIDE_IP, 443))
            assert s.connect_ex is not None
            with pytest.raises(NetworkBlocked):
                s.connect_ex((OUTSIDE_IP, 443))
        finally:
            s.close()
    assert caught and all(OUTSIDE_IP in c for c in caught)


def test_udp_send_and_dns_lookup_raise():
    with netguard.expect_blocked() as caught:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(NetworkBlocked):
                u.sendto(b"x", (OUTSIDE_IP, 53))
        finally:
            u.close()
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo(SOURCE_HOST, 443)
        with pytest.raises(NetworkBlocked):
            socket.gethostbyname(SOURCE_HOST)
    assert any(SOURCE_HOST in c for c in caught)


def test_httpx_request_to_a_source_site_raises_not_a_connect_error():
    """Виняток — не мережевий збій: інакше цикли повторів пішли б по колу."""
    with netguard.expect_blocked() as caught:
        with pytest.raises(NetworkBlocked):
            httpx.get(f"https://{SOURCE_HOST}/uk/", timeout=2)
        with pytest.raises(NetworkBlocked):
            with httpx.Client() as c:
                c.get(f"https://{OLX_HOST}/")
    assert not issubclass(NetworkBlocked, OSError)
    assert caught[0] == f"httpx: https://{SOURCE_HOST}/uk/"


def test_async_httpx_request_raises():
    import asyncio

    async def go():
        async with httpx.AsyncClient() as c:
            await c.get(f"https://{SOURCE_HOST}/")

    with netguard.expect_blocked() as caught:
        with pytest.raises(NetworkBlocked):
            asyncio.run(go())
    assert caught


def test_swallowed_attempt_is_still_recorded():
    """Так поводиться запит курсу НБУ: `except Exception` і резервне значення.
    Виняток зник, але запис у журналі лишився — обгортка тесту його побачить."""
    def careless():
        try:
            httpx.get(NBU_URL)
        except Exception:                                   # noqa: BLE001
            return 42.0

    with netguard.expect_blocked() as caught:
        assert careless() == 42.0
    assert caught == [f"httpx: {NBU_URL}"]


def test_loopback_and_unix_sockets_stay_allowed(local_site):
    r = httpx.get(local_site + "/")
    assert r.status_code == 200 and b"ok" in r.content
    host, port = local_site.removeprefix("http://").split(":")
    socket.create_connection((host, int(port)), timeout=2).close()
    socket.create_connection(("localhost", int(port)), timeout=2).close()

    short = tempfile.mkdtemp(prefix="ng")                # шлях unix-сокета ≤104 символи
    path = os.path.join(short, "s")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cli.connect(path)
        cli.close()
    finally:
        srv.close()
        os.unlink(path)
        os.rmdir(short)
    assert netguard.attempts() == []


@pytest.mark.parametrize("host,local", [
    ("127.0.0.1", True), ("127.8.8.8", True), ("::1", True), ("[::1]", True),
    ("::ffff:127.0.0.1", True), ("localhost", True), ("LocalHost", True),
    ("api.localhost", True), (None, True), ("", True),
    ("0.0.0.0", False), ("10.0.0.1", False), ("192.168.47.1", False),
    ("100.114.183.106", False), ("8.8.8.8", False), ("dom.ria.com", False),
    ("dom.ria.example", False),
    ("localhost.evil.example", False), ("testserver", False),
])
def test_what_counts_as_this_machine(host, local):
    assert netguard.is_loopback_host(host) is local


def test_child_process_of_a_test_is_guarded_too(tmp_path):
    """Штучне джерело в test_timeouts запускається окремим процесом — та сама заборона."""
    log = tmp_path / "child.log"
    env = {**os.environ, netguard.ENV_FLAG: "1", netguard.ENV_LOG: str(log)}
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from realty import netguard\n"
            "assert netguard.install_from_env()\n"
            "import httpx\n"
            "try:\n"
            "    httpx.get('https://%s/')\n"
            "except netguard.NetworkBlocked:\n"
            "    sys.exit(3)\n" % (str(ROOT), OLX_HOST))
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 3, r.stderr
    assert f"httpx: https://{OLX_HOST}/" in log.read_text()


def test_cli_run_by_a_test_is_guarded_too(tmp_path):
    """`cli.py`, запущений тестом окремим процесом, вмикає ту саму заборону
    (REALTY_NETGUARD=1 від conftest); без змінної — нічого не робить."""
    code = ("import runpy, sys; sys.argv = ['cli.py', 'config', 'check']\n"
            "sys.path.insert(0, %r)\n"
            "import realty.netguard as ng\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='not_main')\n"
            "finally:\n"
            "    print('guard', ng.installed())\n" % (str(ROOT), str(ROOT / "cli.py")))
    on = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                        timeout=60, env={**os.environ, netguard.ENV_FLAG: "1",
                                         netguard.ENV_LOG: str(tmp_path / "c.log")})
    assert "guard True" in on.stdout, on.stdout + on.stderr
    env = {k: v for k, v in os.environ.items() if k != netguard.ENV_FLAG}
    off = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         timeout=60, env=env)
    assert "guard False" in off.stdout, off.stdout + off.stderr


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:                                      # noqa: BLE001
        return False


def test_browser_launch_without_marker_raises():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with netguard.expect_blocked() as caught:
        with sync_playwright() as p:
            with pytest.raises(NetworkBlocked):
                p.chromium.launch(headless=True)
    assert caught and caught[0].startswith("playwright: launch")


@pytest.mark.allow_browser
@pytest.mark.skipif(not _chromium_available(), reason="Chromium для Playwright не встановлено")
def test_allowed_browser_still_cannot_fetch_from_outside(local_site):
    """Сам сценарій D45: локальна сторінка з ресурсами на CDN джерела.

    Сторінка відкривається, а всі три зовнішні ресурси (два імені й пряма
    IP-адреса) обірвані браузером і записані — жоден запит не вийшов.
    """
    from playwright.sync_api import sync_playwright

    with netguard.expect_blocked() as caught:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_context().new_page()
                resp = page.goto(local_site + "/", wait_until="load", timeout=20_000)
                assert resp is not None and resp.status == 200
                assert page.inner_text("p") == "ok"
                page.wait_for_timeout(300)
            finally:
                browser.close()
    blocked = sorted(c.removeprefix("browser: ") for c in caught)
    assert blocked == sorted([CDN_PHOTO, CDN_SCRIPT, f"http://{OUTSIDE_IP}/pixel.gif"]), caught
