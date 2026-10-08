"""Нічний рендер OLX у справжньому Chromium — на локальному сервері (E11, D60).

Справжній Playwright, але лише 127.0.0.1 (conftest: allow_browser — запити браузера
назовні обриваються й записуються). Перевіряє те, чого не бачить FakeRenderer:
код відповіді без винятків (200/403/410), вміст сторінки після домальовування,
незавантажені зображення (block_resources), перезапуск браузера кожні restart_every
рендерів. На коді до E11 модуля night/render.py немає.
"""
from __future__ import annotations

import dataclasses
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from realty import configfiles


def _chromium_available() -> bool:
    """Той самий спосіб, що в test_network_guard: чи є Chromium для Playwright."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:                                   # noqa: BLE001
        return False


SEEN: list[str] = []


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append(self.path)
        if self.path.startswith("/gone"):
            self.send_response(410)
            self.end_headers()
            self.wfile.write("Це оголошення більше не доступне".encode())
            return
        if self.path.startswith("/blocked"):
            self.send_response(403)
            self.end_headers()
            return
        if self.path.startswith("/img"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(b"\x89PNG\r\n\x1a\n")
            return
        body = ('<html><body><h4 data-testid="offer_title">Квартира</h4>'
                '<div data-testid="ad-parameters-container"><p><span>Бізнес</span></p></div>'
                '<img src="/img.png"><div id="late"></div>'
                '<script>setTimeout(()=>{document.getElementById("late").textContent="готово"},'
                '100)</script></body></html>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    SEEN.clear()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.allow_browser
@pytest.mark.skipif(not _chromium_available(), reason="Chromium для Playwright не встановлено")
def test_real_night_renderer_codes_content_blocked_images_and_restart(local_site):
    from realty.night.render import NightRenderer

    rcfg = dataclasses.replace(configfiles.load("night").olx_render, settle_ms=400,
                               restart_every=2, render_timeout_seconds=30)
    r = NightRenderer(rcfg, label="тест")
    try:
        ok = r.render(local_site + "/ok")
        assert ok.code == 200 and "Бізнес" in ok.html and "готово" in ok.html
        assert r.render(local_site + "/gone").code == 410
        first_driver = r._driver_pid()
        blocked = r.render(local_site + "/blocked")           # третій — після перезапуску
        assert blocked.code == 403 and blocked.html is None
        assert r._driver_pid() != first_driver
        assert r.renders == 3
    finally:
        r.close()
    assert "/img.png" not in SEEN                              # зображення не вантажимо
    assert r._ctx is None
