"""Ліміти часу: запит, браузер, джерело, цикл.

10.09.2026 регулярний прогін простояв 9 днів: браузер перестав відповідати,
виклик, що на нього чекав, не мав ліміту, а планувальник не запускає новий
прогін, поки живий старий. Ці тести перевіряють механізм, а не обіцянку:
повільні сайти тут справжні (локальний сервер), зависання — справжні
(процес, що не реагує), браузер — справжній Chromium.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import ops, runner  # noqa: E402
from realty.fetcher import BrowserFetcher, DeadlineExceeded, Fetcher, Watchdog  # noqa: E402


# --- Локальний «поганий сайт» ---------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тиша в консолі тестів
        pass

    def do_GET(self):
        if self.path.startswith("/silent"):
            # Прийняли з'єднання й замовкли: ні заголовків, ні тіла.
            time.sleep(60)
            return
        if self.path.startswith("/trickle"):
            # Заголовки одразу, а тіло — по байту: жодна фаза httpx не
            # перевищує свого ліміту, але запит не закінчується ніколи.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                for _ in range(600):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body = json.dumps({"n": 2}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def bad_site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# --- Рівень 1: один запит ---------------------------------------------------------------


def test_trickling_response_is_cut_by_total_deadline(bad_site):
    f = Fetcher(use_cache=False, label="тест")
    f.request_timeout = 1.5
    t0 = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        f.get(bad_site + "/trickle")
    took = time.monotonic() - t0
    # Без загальної стелі цей запит тягнувся б дві хвилини (600 байт × 0,2 с).
    assert took < 5, took


def test_silent_server_gives_up_without_retries(bad_site):
    f = Fetcher(use_cache=False, label="тест")
    f.client.timeout = httpx.Timeout(1.0)
    t0 = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        f.get(bad_site + "/silent")
    took = time.monotonic() - t0
    # Три спроби з паузами дали б 3 с + 2 + 3 с очікування; таймаут не повторюємо.
    assert took < 2.5, took


# --- Рівень 2: браузер ------------------------------------------------------------------


def test_watchdog_kills_process_that_ignores_everything():
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with Watchdog(0.5, lambda: victim.pid, label="тест") as dog:
            time.sleep(1.5)
        assert dog.fired
        assert victim.wait(timeout=5) is not None
    finally:
        if victim.poll() is None:
            victim.kill()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.mark.skipif(not _chromium_available(), reason="Chromium для Playwright не встановлено")
def test_real_browser_hang_is_killed_and_source_abandoned():
    """Справжній Chromium, справжнє зависання: нескінченний цикл у сторінці.

    `page.evaluate` не має параметра тайм-ауту — рівно той клас викликів,
    на якому стояв OLX. Без сторожа тест висів би вічно.
    """
    from realty.fetcher import _descendants

    b = BrowserFetcher(label="тест", op_timeout=4)
    b._ensure()
    driver = b._driver_pid()
    family = [driver, *_descendants(driver)]
    page = b._ctx.new_page()
    t0 = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        with b._guarded("нескінченний скрипт"):
            page.evaluate("() => { while (true) {} }")
    assert time.monotonic() - t0 < 15
    assert b.dead
    # Наступний виклик — одразу відмова, без спроби «ще раз».
    t1 = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        b.render("https://example.test/")
    assert time.monotonic() - t1 < 1
    b.close()
    time.sleep(1)
    alive = [p for p in family if _alive(p)]
    assert not alive, f"лишились процеси браузера: {alive}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Зомбі теж «живий» для kill(0); перевіряємо стан, де це можливо.
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


# --- Рівні 3 і 4: джерело й цикл --------------------------------------------------------


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Окремі бази для батьківського процесу й дочірніх кроків."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    ops_url = f"sqlite:///{tmp_path / 'ops.db'}"
    monkeypatch.setenv("OPS_DB_URL", ops_url)
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path / 'realty.db'}")
    monkeypatch.setenv("CACHE_TTL", "0")
    monkeypatch.setenv("LLM_FALLBACK", "0")
    monkeypatch.setenv("REQUEST_TIMEOUT", "2")
    engine = create_engine(ops_url, future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(engine)
    return tmp_path


def _fake(mode: str, url: str, timeout: float) -> runner.Step:
    return runner.Step(f"збір: fake_{mode}",
                       [sys.executable, str(ROOT / "tests" / "fake_slow_source.py"), mode, url],
                       timeout, kind="source", source=f"fake_{mode}")


def test_hung_source_does_not_stop_the_cycle(isolated, bad_site):
    """Головний критерій: одне джерело зависло — решта зібрала своє."""
    steps = [
        _fake("freeze", bad_site + "/ok", timeout=6),      # висить, поки не вб'ють
        _fake("trickle", bad_site + "/trickle", timeout=60),  # сайт тягне відповідь
        _fake("ok", bad_site + "/ok", timeout=60),
    ]
    t0 = time.monotonic()
    result = runner.run_cycle(trigger="schedule", steps=steps, run_timeout=120,
                              lock_path=isolated / "cycle.lock",
                              disabled_flag=isolated / "OFF")
    took = time.monotonic() - t0

    by_name = {s.name: s for s in result.steps}
    assert by_name["збір: fake_freeze"].status == "timeout"
    assert by_name["збір: fake_ok"].status == "ok"
    assert result.kept == 2, result
    assert result.status == "partial"
    # Завислий процес обійшовся в ліміт джерела, а не в години.
    assert took < 45, took

    with ops.ops_session() as s:
        from sqlalchemy import select
        runs = {r.source: r for r in s.scalars(select(ops.RunRecord))}
        cycle = s.scalars(select(ops.CycleRecord)).one()
    # Вбитий процес не встиг закрити свій запис — це зробив диригент.
    assert runs["fake_freeze"].status == "timeout"
    # Повільний сайт кинуто за лімітом запиту, і це помилка, а не «ок, нуль».
    assert runs["fake_trickle"].status == "failed"
    assert "DeadlineExceeded" in (runs["fake_trickle"].message or "")
    assert runs["fake_ok"].status == "ok" and runs["fake_ok"].kept == 2
    assert cycle.status == "partial" and cycle.kept == 2
    assert ops.last_success_at() is not None


def test_cycle_deadline_stops_current_step_and_skips_the_rest(isolated, bad_site):
    steps = [
        runner.Step("довгий крок", [sys.executable, "-c", "import time; time.sleep(60)"], 600),
        runner.Step("наступний", [sys.executable, "-c", "print(1)"], 600),
    ]
    t0 = time.monotonic()
    result = runner.run_cycle(steps=steps, run_timeout=3,
                              lock_path=isolated / "cycle.lock",
                              disabled_flag=isolated / "OFF")
    assert time.monotonic() - t0 < 25
    assert result.steps[0].status == "timeout"
    assert result.steps[1].status == "skipped"
    assert result.status == "timeout"
    assert not result.succeeded


def test_cycle_that_collected_nothing_is_not_a_success(isolated):
    steps = [runner.Step("збір: порожньо", [sys.executable, "-c", "pass"], 60,
                         kind="source", source="порожньо")]
    result = runner.run_cycle(steps=steps, run_timeout=60,
                              lock_path=isolated / "cycle.lock",
                              disabled_flag=isolated / "OFF")
    assert result.status == "failed"
    assert "не зібрано" in result.message
    assert ops.last_success_at() is None


def test_second_cycle_waits_for_the_first(isolated):
    lock = runner.CycleLock(isolated / "cycle.lock")
    assert lock.acquire()
    try:
        result = runner.run_cycle(steps=[], lock_path=isolated / "cycle.lock",
                                  disabled_flag=isolated / "OFF")
        assert result.status == "skipped"
    finally:
        lock.release()


def test_overdue_cycle_is_reaped(isolated):
    """Диригент, що завис сам, не має тримати замок вічно."""
    holder = subprocess.Popen([sys.executable, "-c", f"""
import fcntl, json, time
fh = open({str(isolated / 'cycle.lock')!r}, 'a+')
fcntl.flock(fh, fcntl.LOCK_EX)
fh.seek(0); fh.truncate(); fh.write(json.dumps({{'pid': __import__('os').getpid(), 'started': time.time() - 100000}})); fh.flush()
time.sleep(60)
"""])
    try:
        time.sleep(1)
        result = runner.run_cycle(steps=[], run_timeout=60,
                                  lock_path=isolated / "cycle.lock",
                                  disabled_flag=isolated / "OFF")
        assert holder.wait(timeout=5) is not None
        assert result.status != "skipped"
    finally:
        if holder.poll() is None:
            holder.kill()


def test_collection_can_be_switched_off_on_this_machine(isolated):
    (isolated / "OFF").write_text("базу перенесено на Fedora")
    result = runner.run_cycle(steps=[runner.Step("x", [sys.executable, "-c", "1/0"], 5)],
                              lock_path=isolated / "cycle.lock",
                              disabled_flag=isolated / "OFF")
    assert result.status == "disabled"
    assert "Fedora" in result.message
