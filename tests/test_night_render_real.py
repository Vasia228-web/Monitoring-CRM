"""Нічний рендер OLX у справжньому Chromium — на локальному сервері (E11, D60).

Справжній Playwright, але лише 127.0.0.1 (conftest: allow_browser — запити браузера
назовні обриваються й записуються). Перевіряє те, чого не бачить FakeRenderer:
код відповіді без винятків (200/403/410), вміст сторінки після домальовування,
незавантажені зображення (block_resources), перезапуск браузера кожні restart_every
рендерів. На коді до E11 модуля night/render.py немає.

Рецензія E11 (08.10): після того як сторож убив завислий браузер, page.close() крутився
вічно (100% процесора), а закритий без stop() Playwright не давав підняти новий
(sync_playwright().start() падав би до кінця вікна); профіль Chromium (кеш сторінок з
іменами продавців) лишався після вбивства.
"""
from __future__ import annotations

import dataclasses
import threading
import time
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
        if self.path.startswith("/hang"):
            # Сторінка відкрилась, а далі головний потік сторінки зайнятий назавжди:
            # page.content() тайм-ауту не має — спрацьовує лише сторож.
            body = (b'<html><body>x<script>setTimeout(()=>{while(true){}},50)</script>'
                    b'</body></html>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
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
def test_real_night_renderer_codes_content_blocked_images_and_restart(local_site, tmp_path):
    from realty.night.render import NightRenderer

    rcfg = dataclasses.replace(configfiles.load("night").olx_render, settle_ms=400,
                               restart_every=2, render_timeout_seconds=30)
    r = NightRenderer(rcfg, label="тест", profile_dir=tmp_path / "profile")
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


@pytest.mark.allow_browser
@pytest.mark.skipif(not _chromium_available(), reason="Chromium для Playwright не встановлено")
def test_real_hung_page_is_killed_and_the_next_render_works(local_site, tmp_path):
    """Сторож убив браузер на завислій сторінці — рендер повертає «timeout» за стелю, а
    наступний рендер піднімає новий браузер і працює. Профіль (кеш) після вбивства й
    після закриття стерто. На коді до рецензії перший рендер не повертався зовсім
    (page.close() мертвого драйвера), тому сценарій — у потоці з межею часу."""
    from realty.night.render import NightRenderer

    rcfg = dataclasses.replace(configfiles.load("night").olx_render, settle_ms=300,
                               render_timeout_seconds=5)
    assert any(a.startswith("--disk-cache-size=") for a in rcfg.launch_args)
    profile = tmp_path / "profile"
    out: dict = {}

    def scenario():
        r = NightRenderer(rcfg, label="тест", profile_dir=profile)
        try:
            t0 = time.monotonic()
            out["hang"] = r.render(local_site + "/hang")
            out["hang_s"] = time.monotonic() - t0
            out["profile_after_kill"] = profile.exists()
            out["ok"] = r.render(local_site + "/ok")
            out["profile_while_open"] = profile.exists()
        finally:
            r.close()
        out["profile_after_close"] = profile.exists()

    th = threading.Thread(target=scenario, daemon=True)
    th.start()
    th.join(60)
    assert not th.is_alive(), "рендер після вбивства браузера не повернувся (завис)"
    assert out["hang"].code == 0 and out["hang"].error == "timeout" and out["hang_s"] < 20
    assert out["ok"].code == 200 and "Квартира" in out["ok"].html
    assert out["profile_after_kill"] is False and out["profile_while_open"] is True
    assert out["profile_after_close"] is False


# --- Без браузера: підмінений Playwright ----------------------------------------------------------


class _FakePlaywright:
    """Як справжній sync Playwright: поки попередній екземпляр не зупинено, новий start()
    падає («Sync API inside the asyncio loop»); виклик сторінки після вбивства драйвера
    записується (справжній — крутиться вічно)."""

    live = 0

    def __init__(self, killed: threading.Event, log: list) -> None:
        self.killed, self.log = killed, log
        self.chromium = self

    # sync_playwright()
    def start(self):
        if _FakePlaywright.live:
            raise RuntimeError("It looks like you are using Playwright Sync API inside the "
                               "asyncio loop.")
        _FakePlaywright.live += 1
        self.killed.clear()
        return self

    def stop(self):
        _FakePlaywright.live -= 1
        self.log.append("stop")

    # chromium
    def launch_persistent_context(self, user_data_dir, **kw):
        self.log.append(("launch", user_data_dir, tuple(kw.get("args") or ())))
        (Path(user_data_dir) / "cache").mkdir(parents=True, exist_ok=True)
        (Path(user_data_dir) / "cache" / "page").write_text("ім'я продавця")
        return _FakeCtx(self)

    # стара версія (launch + new_context) — та сама поведінка
    def launch(self, **kw):
        self.log.append(("launch", None, tuple(kw.get("args") or ())))
        return self

    def new_context(self, **_kw):
        return _FakeCtx(self)


class _FakeCtx:
    def __init__(self, pw) -> None:
        self.pw = pw

    def route(self, *_a):
        pass

    def set_default_timeout(self, _ms):
        pass

    def set_default_navigation_timeout(self, _ms):
        pass

    def new_page(self):
        return _FakePage(self.pw)

    def close(self):
        if self.pw.killed.is_set():
            self.pw.log.append("ctx.close after kill")
        self.pw.log.append("ctx.close")


class _FakePage:
    def __init__(self, pw) -> None:
        self.pw = pw
        self.url = ""

    def goto(self, url, **_kw):
        self.url = url
        if "hang" in url:
            self.pw.killed.wait(10)                     # «висить», доки сторож не вб'є
            raise RuntimeError("Target page, context or browser has been closed")

        class R:
            status = 200
        return R()

    def wait_for_timeout(self, _ms):
        pass

    def content(self):
        return "<html>ok</html>"

    def close(self):
        if self.pw.killed.is_set():
            self.pw.log.append("page.close after kill")


def test_after_a_watchdog_kill_playwright_is_stopped_and_the_next_render_works(
        monkeypatch, tmp_path):
    import playwright.sync_api

    from realty import fetcher
    from realty.night import render

    killed, log = threading.Event(), []
    _FakePlaywright.live = 0
    monkeypatch.setattr(playwright.sync_api, "sync_playwright",
                        lambda: _FakePlaywright(killed, log))
    monkeypatch.setattr(render.NightRenderer, "_driver_pid", lambda self: 4242)
    monkeypatch.setattr(fetcher, "kill_tree", lambda pid: killed.set())
    monkeypatch.setattr(render, "kill_tree", lambda pid: killed.set())
    rcfg = dataclasses.replace(configfiles.load("night").olx_render, settle_ms=0,
                               render_timeout_seconds=0.3)
    profile = tmp_path / "profile"
    try:
        r = render.NightRenderer(rcfg, label="тест", profile_dir=profile)
    except TypeError:                                     # код до рецензії: profile_dir немає
        r = render.NightRenderer(rcfg, label="тест")
    hung = r.render("http://127.0.0.1:9/hang")
    assert hung.code == 0 and hung.error == "timeout"
    assert "stop" in log                                  # Playwright зупинено після вбивства
    assert "page.close after kill" not in log and "ctx.close after kill" not in log
    assert not profile.exists()                           # кеш сторінок не пережив убивства
    ok = r.render("http://127.0.0.1:9/ok")
    assert ok.code == 200 and ok.html == "<html>ok</html>"
    launches = [x for x in log if isinstance(x, tuple) and x[0] == "launch"]
    assert len(launches) == 2 and launches[0][1] == str(profile)
    assert "--disk-cache-size=1" in launches[0][2]
    r.close()
    assert _FakePlaywright.live == 0 and not profile.exists()


def test_launch_allowance_covers_a_browser_restart():
    from realty.night import render

    rcfg = dataclasses.replace(configfiles.load("night").olx_render, restart_every=2)
    r = render.NightRenderer(rcfg)
    assert r.launch_allowance() == rcfg.render_timeout_seconds          # браузера ще немає
    r._ctx, r._since_start = object(), 1
    assert r.launch_allowance() == 0
    r._since_start = 2                                                    # перезапуск
    assert r.launch_allowance() == rcfg.render_timeout_seconds + render.CLOSE_TIMEOUT_S


def test_cgroup_headroom_is_read_from_the_own_cgroup_and_its_parents(tmp_path):
    from realty.night import render

    root = tmp_path / "cg"
    svc = root / "user.slice" / "user-1000.slice" / "realty-night.service"
    svc.mkdir(parents=True)
    mb = 1024 * 1024
    (svc / "memory.high").write_text(f"{1800 * mb}\n")
    (svc / "memory.current").write_text(f"{1500 * mb}\n")
    (svc.parent / "memory.high").write_text("max\n")
    (svc.parent / "memory.max").write_text(f"{3000 * mb}\n")
    (svc.parent / "memory.current").write_text(f"{2900 * mb}\n")
    proc = tmp_path / "cgroup"
    proc.write_text("0::/user.slice/user-1000.slice/realty-night.service\n")
    assert render.cgroup_headroom_mb(proc, root) == 100       # предок тісніший за юніт
    (svc.parent / "memory.max").write_text("max\n")
    assert render.cgroup_headroom_mb(proc, root) == 300       # лише межа юніта
    assert render.cgroup_headroom_mb(tmp_path / "немає", root) is None    # macOS
    proc.write_text("1:name=systemd:/x\n")                   # cgroup v1 — невідомо
    assert render.cgroup_headroom_mb(proc, root) is None


def test_render_headroom_is_the_smaller_of_machine_and_cgroup(monkeypatch):
    from realty.night import evidence, render

    monkeypatch.setattr(render, "mem_available_mb", lambda: 3000)
    monkeypatch.setattr(render, "cgroup_headroom_mb", lambda: 120)
    assert render.render_headroom_mb() == 120
    monkeypatch.setattr(render, "cgroup_headroom_mb", lambda: None)
    assert render.render_headroom_mb() == 3000
    monkeypatch.setattr(render, "mem_available_mb", lambda: None)
    assert render.render_headroom_mb() is None
    jobs = evidence.OlxJobs({}, None, renderer=None, scope=None, scfg=None,
                            ncfg=configfiles.load("night"))
    assert jobs.mem_fn is render.render_headroom_mb           # смуга дивиться і на cgroup
