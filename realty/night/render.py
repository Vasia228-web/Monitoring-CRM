"""Рендер OLX у Chromium для нічної смуги olx.ua — Блоки 3/4 (E11, D60).

Той самий Playwright Chromium, що й у збору OLX (fetcher.BrowserFetcher), але для ночі:
  * код відповіді без винятків (401/403/429 — у блокування смуги, 410/404 — «не вдалось»),
    кешу на диску немає (відрендерена сторінка містить ім'я продавця — не зберігаємо);
  * один браузер, послідовно; зображення, відео й шрифти не вантажимо (лише розмітка:
    менше пам'яті й трафіку), перезапуск кожні restart_every рендерів;
  * стеля одного рендера — render_timeout_seconds (сторож убиває браузер і він
    перезапускається на наступному рендері);
  * MemAvailable машини — перед кожним рендером (3,7 ГБ на Fedora; Chromium до
    814 МБ): менше за min_mem_available_mb — рендери стоять до кінця вікна.
Паузу старт-до-старту й дедлайн тримає смуга (night.lane, ворота), не цей клас.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .. import ops
from ..config import REQUEST_TIMEOUT, USER_AGENT
from ..fetcher import BLOCKING_CODES, Watchdog, kill_tree

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderResult:
    code: int                       # 0 — відповіді немає (error)
    html: str | None
    final_url: str | None
    error: str | None = None
    seconds: float = 0.0


def mem_available_mb() -> int | None:
    """MemAvailable машини (Linux), МБ; None — невідомо (macOS)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class NightRenderer:
    def __init__(self, rcfg, *, label: str = "olx") -> None:
        self.cfg = rcfg
        self.label = label
        self.renders = 0
        self._since_start = 0
        self._pw = self._browser = self._ctx = None

    def _driver_pid(self) -> int | None:
        try:
            return self._pw._impl_obj._connection._transport._proc.pid
        except Exception:                               # noqa: BLE001
            return None

    def _ensure(self) -> None:
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        with Watchdog(self.cfg.render_timeout_seconds, self._driver_pid, label="нічний браузер"):
            self._browser = self._pw.chromium.launch(headless=True,
                                                     args=list(self.cfg.launch_args))
            self._ctx = self._browser.new_context(
                locale="uk-UA", timezone_id="Europe/Kyiv",
                viewport={"width": 1366, "height": 900}, user_agent=USER_AGENT)
            blocked = set(self.cfg.block_resources)
            if blocked:
                def route(r):
                    if r.request.resource_type in blocked:
                        r.abort()
                    else:
                        r.continue_()

                self._ctx.route("**/*", route)
            ms = int(min(REQUEST_TIMEOUT, self.cfg.render_timeout_seconds) * 1000)
            self._ctx.set_default_timeout(ms)
            self._ctx.set_default_navigation_timeout(ms)
        self._since_start = 0

    def render(self, url: str) -> RenderResult:
        if self._since_start >= self.cfg.restart_every:
            self.close()                                 # пам'ять Chromium росте
        started = time.monotonic()
        dog = None
        try:
            self._ensure()
            dog = Watchdog(self.cfg.render_timeout_seconds, self._driver_pid,
                           label=self.label or "нічний браузер")
            with dog:
                page = self._ctx.new_page()
                try:
                    resp = page.goto(url, wait_until="domcontentloaded")
                    code = resp.status if resp is not None else 0
                    html = None
                    if 200 <= code < 300:
                        page.wait_for_timeout(self.cfg.settle_ms)
                        html = page.content()
                    final = page.url
                finally:
                    try:
                        page.close()
                    except Exception:                    # noqa: BLE001
                        pass
        except Exception as e:                           # noqa: BLE001 — відповідь, не виняток
            fired = dog is not None and dog.fired
            self.close(dead=fired)
            ops.record_request(self.label, ok=False)
            return RenderResult(0, None, None, error="timeout" if fired else type(e).__name__,
                                seconds=round(time.monotonic() - started, 2))
        self.renders += 1
        self._since_start += 1
        ops.record_request(self.label, ok=200 <= code < 400, blocked=code in BLOCKING_CODES)
        return RenderResult(code, html, final, seconds=round(time.monotonic() - started, 2))

    def close(self, dead: bool = False) -> None:
        if dead:
            if pid := self._driver_pid():
                kill_tree(pid)
        else:
            try:
                with Watchdog(30, self._driver_pid, label="закриття нічного браузера"):
                    for obj, meth in ((self._ctx, "close"), (self._browser, "close"),
                                      (self._pw, "stop")):
                        if obj is not None:
                            try:
                                getattr(obj, meth)()
                            except Exception:            # noqa: BLE001
                                pass
            except Exception:                            # noqa: BLE001
                pass
        self._pw = self._browser = self._ctx = None
        self._since_start = 0
