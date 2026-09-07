"""Мережевий шар: HTTP-клієнт, рендер у браузері, кеш, обмеження темпу.

Два рівні доступу:
  * `Fetcher.get` / `get_json` — звичайний httpx-клієнт із реалістичними
    заголовками, ретраями та паузою між запитами до одного хоста;
  * `BrowserFetcher.render` — реальний Chromium через Playwright для сайтів,
    які віддають контент лише після виконання JS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry, retry_if_exception_type, stop_after_attempt, wait_exponential,
)

from . import ops
from .config import (
    CACHE_DIR, CACHE_TTL, DEFAULT_DELAY, HTTP_RETRIES, HTTP_TIMEOUT, PROXY_URL, USER_AGENT,
)

# Коди, за якими сайт відмовляє навмисно, а не через збій.
BLOCKING_CODES = {401, 403, 429}

log = logging.getLogger(__name__)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}


class RateLimiter:
    """Мінімальна пауза між запитами в межах одного хоста."""

    def __init__(self, default_delay: float = DEFAULT_DELAY) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._default = default_delay

    def wait(self, url: str, delay: float | None = None) -> None:
        host = urlsplit(url).netloc
        d = self._default if delay is None else delay
        with self._lock:
            gap = time.monotonic() - self._last.get(host, 0.0)
            if gap < d:
                time.sleep(d - gap)
            self._last[host] = time.monotonic()


class DiskCache:
    """Файловий кеш відповідей — не смикаємо джерело двічі за одне й те саме."""

    def __init__(self, ttl: int = CACHE_TTL, directory: Path = CACHE_DIR) -> None:
        self.ttl = ttl
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".txt")

    def get(self, key: str) -> str | None:
        if self.ttl <= 0:
            return None
        p = self._path(key)
        if p.exists() and (time.time() - p.stat().st_mtime) < self.ttl:
            return p.read_text(encoding="utf-8")
        return None

    def set(self, key: str, value: str) -> None:
        if self.ttl > 0:
            self._path(key).write_text(value, encoding="utf-8")


class FetchError(RuntimeError):
    """Запит не вдався після всіх спроб."""


class Fetcher:
    """HTTP-клієнт із кешем, ретраями й обмеженням темпу."""

    def __init__(self, delay: float = DEFAULT_DELAY, use_cache: bool = True,
                 label: str | None = None) -> None:
        # `label` — ім'я джерела: за ним дашборд рахує частку успішних запитів.
        self.label = label
        self.limiter = RateLimiter(delay)
        self.cache = DiskCache() if use_cache else None
        self.client = httpx.Client(
            headers=BASE_HEADERS,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            proxy=PROXY_URL,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @retry(
        stop=stop_after_attempt(HTTP_RETRIES),
        wait=wait_exponential(multiplier=1.5, min=2, max=20),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        reraise=True,
    )
    def _request(self, url: str, params: dict | None, headers: dict | None,
                 delay: float | None) -> str:
        self.limiter.wait(url, delay)
        try:
            r = self.client.get(url, params=params, headers=headers)
        except Exception:
            ops.record_request(self.label, ok=False)
            raise
        ops.record_request(self.label, ok=r.status_code < 400,
                           blocked=r.status_code in BLOCKING_CODES)
        # 4xx (крім 429) повторювати марно — це відповідь сайту, а не збій.
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} для {url}")
        return r.text

    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            delay: float | None = None) -> str:
        key = url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
        if self.cache and (hit := self.cache.get(key)) is not None:
            log.debug("кеш: %s", url)
            return hit
        try:
            text = self._request(url, params, headers, delay)
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(f"{type(e).__name__} для {url}: {e}") from e
        if self.cache:
            self.cache.set(key, text)
        return text

    def get_json(self, url: str, params: dict | None = None,
                 delay: float | None = None) -> dict | list:
        text = self.get(url, params, headers={"Accept": "application/json"}, delay=delay)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError(f"Очікували JSON від {url}, отримали HTML/сміття") from e


class BrowserFetcher:
    """Рендер сторінки справжнім Chromium — для сайтів, що вимагають JS.

    Це звичайний браузер із типовими налаштуваннями локалі: жодних
    stealth-патчів чи підміни відбитків.
    """

    def __init__(self, delay: float = 2.5, headless: bool = True,
                 label: str | None = None) -> None:
        self.label = label
        self.limiter = RateLimiter(delay)
        self.headless = headless
        self.cache = DiskCache()
        self._pw = None
        self._browser = None
        self._ctx = None

    def _ensure(self):
        if self._ctx is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise FetchError(
                "Потрібен Playwright: pip install playwright && playwright install chromium"
            ) from e
        self._pw = sync_playwright().start()
        launch: dict = {"headless": self.headless}
        if PROXY_URL:
            launch["proxy"] = {"server": PROXY_URL}
        self._browser = self._pw.chromium.launch(**launch)
        self._ctx = self._browser.new_context(
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            viewport={"width": 1366, "height": 900},
            user_agent=USER_AGENT,
        )

    def render(self, url: str, wait_selector: str | None = None,
               settle_ms: int = 2500, delay: float | None = None) -> str:
        if (hit := self.cache.get("render:" + url)) is not None:
            log.debug("кеш (render): %s", url)
            return hit
        self._ensure()
        self.limiter.wait(url, delay)
        page = self._ctx.new_page()
        try:
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                ops.record_request(self.label, ok=False)
                # Playwright кидає власні помилки (обрив мережі, таймаут). Якщо
                # їх не привести до FetchError, вони пролітають повз усі
                # перехоплювачі й валять увесь прогін — так і сталося
                # на ERR_INTERNET_DISCONNECTED під час збору OLX.
                raise FetchError(f"{type(e).__name__} для {url}: {str(e)[:200]}") from e
            code = resp.status if resp is not None else 0
            ops.record_request(self.label, ok=code < 400,
                               blocked=code in BLOCKING_CODES)
            if resp is not None and resp.status >= 400:
                raise FetchError(f"HTTP {resp.status} для {url}")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15_000)
                except Exception:
                    log.warning("Селектор %s не з'явився на %s", wait_selector, url)
            page.wait_for_timeout(settle_ms)
            html = page.content()
        finally:
            page.close()
        self.cache.set("render:" + url, html)
        return html

    def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass
        self._ctx = self._browser = self._pw = None

    def __enter__(self) -> "BrowserFetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
