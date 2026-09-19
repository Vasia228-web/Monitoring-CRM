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
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import ops
from .config import (
    BROWSER_OP_TIMEOUT, CACHE_DIR, CACHE_TTL, DEFAULT_DELAY, HTTP_RETRIES, HTTP_TIMEOUT,
    PROXY_URL, REQUEST_TIMEOUT, USER_AGENT,
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
    """Мінімальна пауза між запитами в межах одного хоста.

    Пауза рахується під замком, а сон відбувається поза ним. Це не дрібниця:
    якщо спати із замком у руках, потік, що чекає свою чергу до одного сайту,
    зупиняє потоки до всіх інших — і паралельна робота по джерелах перестає
    бути паралельною, лишаючись такою тільки на вигляд.
    """

    def __init__(self, default_delay: float = DEFAULT_DELAY) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._default = default_delay

    def wait(self, url: str, delay: float | None = None) -> None:
        host = urlsplit(url).netloc
        d = self._default if delay is None else delay
        while True:
            with self._lock:
                now = time.monotonic()
                gap = now - self._last.get(host, 0.0)
                if gap >= d:
                    # Слот вільний: займаємо його одразу, ще під замком, щоб
                    # два потоки до одного хоста не пішли одночасно.
                    self._last[host] = now
                    return
                pause = d - gap
            time.sleep(pause)


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


class DeadlineExceeded(FetchError):
    """Зовнішня сторона не вклалась у ліміт часу.

    Окремий клас, бо реакція інша, ніж на звичайну помилку: повтор тут не
    допомагає, а лише множить очікування. Джерело, яке не відповіло, кидаємо
    й ідемо до наступного.
    """


def _retryable(exc: BaseException) -> bool:
    """Повторюємо збої мережі й 429/5xx, але НЕ таймаути.

    Три спроби по 30 с до сайту, що мовчить, — це півтори хвилини простою
    на кожному запиті, а не шанс на успіх.
    """
    if isinstance(exc, httpx.TimeoutException):
        return False
    return isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError))


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
            timeout=httpx.Timeout(HTTP_TIMEOUT, connect=min(15.0, HTTP_TIMEOUT)),
            follow_redirects=True,
            proxy=PROXY_URL,
        )
        self.request_timeout = REQUEST_TIMEOUT

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _fetch(self, method: str, url: str, params: dict | None = None,
               headers: dict | None = None, body: bool = True) -> tuple[httpx.Response, str]:
        """Один запит із жорсткою стелею часу від початку до кінця.

        Тайм-аути httpx обмежують кожну фазу окремо: з'єднання, очікування
        чергового шматка. Сайт, що віддає тіло по краплі, жодної з них не
        перевищує і може тягнути запит скільки завгодно. Тому тіло читаємо
        шматками й після кожного звіряємося з годинником.
        """
        started = time.monotonic()
        deadline = started + self.request_timeout
        with self.client.stream(method, url, params=params, headers=headers) as r:
            if not body:
                return r, ""
            chunks: list[bytes] = []
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise DeadlineExceeded(
                        f"відповідь не вклалась у {self.request_timeout:.0f} с для {url}")
            content = b"".join(chunks)
        return r, content.decode(r.encoding or "utf-8", errors="replace")

    @retry(
        stop=stop_after_attempt(HTTP_RETRIES),
        wait=wait_exponential(multiplier=1.5, min=2, max=20),
        retry=retry_if_exception(_retryable),
        reraise=True,
    )
    def _request(self, url: str, params: dict | None, headers: dict | None,
                 delay: float | None) -> str:
        self.limiter.wait(url, delay)
        try:
            r, text = self._fetch("GET", url, params=params, headers=headers)
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
        return text

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
        except httpx.TimeoutException as e:
            raise DeadlineExceeded(f"{type(e).__name__} для {url}: {e}") from e
        except Exception as e:
            raise FetchError(f"{type(e).__name__} для {url}: {e}") from e
        if self.cache:
            self.cache.set(key, text)
        return text

    def probe(self, url: str, delay: float | None = None) -> int:
        """Код відповіді без винятків — для перевірки, чи оголошення живе.

        Ходимо методом HEAD: перевірці потрібен статус, а не вміст сторінки.
        Виграш подвійний. По-перше, тіло не завантажується взагалі — повний
        обхід бази переставав бути завантаженням 3.4 ГБ HTML заради трьох
        цифр. По-друге, OLX за захистом CloudFront віддає 403 на будь-який
        GET без браузера, але на HEAD відповідає чесно — заміряно на 20
        оголошеннях, збіг із Chromium 20 із 20.

        Якщо сайт HEAD не підтримує (405/501), мовчки повторюємо GET: краще
        дорожчий запит, ніж хибний висновок про неіснуючу сторінку.
        """
        code = self._probe_once("HEAD", url, delay)
        if code in (405, 501):
            log.debug("%s не приймає HEAD — пробуємо GET", urlsplit(url).netloc)
            code = self._probe_once("GET", url, delay)
        return code

    def _probe_once(self, method: str, url: str, delay: float | None) -> int:
        self.limiter.wait(url, delay)
        try:
            # Тіло не читаємо навіть для GET: потрібен лише код.
            r, _ = self._fetch(method, url, body=False)
        except Exception:
            ops.record_request(self.label, ok=False)
            return 0
        ops.record_request(self.label, ok=r.status_code < 400,
                           blocked=r.status_code in BLOCKING_CODES)
        return r.status_code

    def get_json(self, url: str, params: dict | None = None,
                 delay: float | None = None) -> dict | list:
        text = self.get(url, params, headers={"Accept": "application/json"}, delay=delay)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError(f"Очікували JSON від {url}, отримали HTML/сміття") from e


def _descendants(pid: int) -> list[int]:
    """Усі нащадки процесу (діти, онуки...) — через pgrep, що є і на macOS, і в Linux."""
    out: list[int] = []
    queue = [pid]
    while queue:
        parent = queue.pop()
        try:
            r = subprocess.run(["pgrep", "-P", str(parent)], capture_output=True,
                               text=True, timeout=5)
        except Exception:
            continue
        kids = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        out.extend(kids)
        queue.extend(kids)
    return out


def kill_tree(pid: int) -> None:
    """SIGKILL процесу разом із нащадками. Нащадків збираємо ДО вбивства:
    після смерті батька вони переходять до init і вже не знаходяться."""
    for p in [pid, *_descendants(pid)]:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class Watchdog:
    """Будильник для операцій, які самі не вміють здаватися.

    Частина викликів Playwright не має параметра тайм-ауту взагалі
    (`page.content()`, `new_page()`, `close()`). Якщо браузер перестав
    відповідати, такий виклик чекає вічно — саме це й сталося 10.09.2026.
    Сторож у окремому потоці: не встигла операція за `seconds` — він убиває
    процес драйвера разом із браузером. Завислий виклик отримує обрив
    з'єднання замість нескінченного очікування, і джерело кидається.
    """

    def __init__(self, seconds: float, pid_getter, label: str = "") -> None:
        self.seconds = seconds
        self._pid_getter = pid_getter
        self.label = label
        self.fired = False
        self._timer: threading.Timer | None = None

    def _fire(self) -> None:
        pid = self._pid_getter()
        if not pid:
            return
        self.fired = True
        log.error("%s: браузер не відповів за %.0f с — зупиняємо процес %s",
                  self.label or "браузер", self.seconds, pid)
        kill_tree(pid)

    def __enter__(self) -> "Watchdog":
        self._timer = threading.Timer(self.seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._timer is not None:
            self._timer.cancel()


class BrowserFetcher:
    """Рендер сторінки справжнім Chromium — для сайтів, що вимагають JS.

    Це звичайний браузер із типовими налаштуваннями локалі: жодних
    stealth-патчів чи підміни відбитків.

    Кожна операція йде під сторожем (`Watchdog`). Якщо браузер довелося
    вбити, екземпляр позначається мертвим і далі одразу відповідає
    `DeadlineExceeded`: джерело, що не відповіло, ми кидаємо, а не
    перезапускаємо браузер по колу.
    """

    def __init__(self, delay: float = 2.5, headless: bool = True,
                 label: str | None = None, op_timeout: float = BROWSER_OP_TIMEOUT) -> None:
        self.label = label
        self.limiter = RateLimiter(delay)
        self.headless = headless
        self.cache = DiskCache()
        self.op_timeout = op_timeout
        self.dead = False
        self._pw = None
        self._browser = None
        self._ctx = None

    def _driver_pid(self) -> int | None:
        """PID процесу драйвера Playwright; браузер — його нащадок."""
        try:
            return self._pw._impl_obj._connection._transport._proc.pid
        except Exception:
            return None

    @contextmanager
    def _guarded(self, what: str):
        if self.dead:
            raise DeadlineExceeded(f"браузер уже зупинено після тайм-ауту ({what})")
        dog = Watchdog(self.op_timeout, self._driver_pid, label=self.label or "браузер")
        try:
            with dog:
                yield
        except Exception as e:
            if dog.fired:
                self.dead = True
                raise DeadlineExceeded(
                    f"браузер не відповів за {self.op_timeout:.0f} с ({what})") from e
            raise
        if dog.fired:
            # Операція встигла повернутись у ту ж мить, коли сторож спрацював:
            # браузер однаково вже вбитий.
            self.dead = True

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
        with self._guarded("запуск браузера"):
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
            # Стеля для всіх викликів, що приймають тайм-аут; решту стереже Watchdog.
            self._ctx.set_default_timeout(REQUEST_TIMEOUT * 1000)
            self._ctx.set_default_navigation_timeout(REQUEST_TIMEOUT * 1000)

    def render(self, url: str, wait_selector: str | None = None,
               settle_ms: int = 2500, delay: float | None = None) -> str:
        if (hit := self.cache.get("render:" + url)) is not None:
            log.debug("кеш (render): %s", url)
            return hit
        self._ensure()
        self.limiter.wait(url, delay)
        try:
            with self._guarded(url):
                html = self._render_page(url, wait_selector, settle_ms)
        except DeadlineExceeded:
            ops.record_request(self.label, ok=False)
            raise
        self.cache.set("render:" + url, html)
        return html

    def _render_page(self, url: str, wait_selector: str | None, settle_ms: int) -> str:
        page = self._ctx.new_page()
        try:
            try:
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=REQUEST_TIMEOUT * 1000)
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
            return page.content()
        finally:
            if not self.dead:
                try:
                    page.close()
                except Exception:
                    pass

    def probe(self, url: str, delay: float | None = None) -> int:
        """Код відповіді без винятків — для перевірки, чи оголошення живе."""
        try:
            self._ensure()
        except FetchError:
            return 0
        self.limiter.wait(url, delay)
        code = 0
        try:
            with self._guarded(url):
                page = self._ctx.new_page()
                try:
                    resp = page.goto(url, wait_until="domcontentloaded",
                                     timeout=REQUEST_TIMEOUT * 1000)
                    code = resp.status if resp is not None else 0
                except Exception:
                    code = 0
                finally:
                    page.close()
        except Exception:
            code = 0
        ops.record_request(self.label, ok=0 < code < 400,
                           blocked=code in BLOCKING_CODES)
        return code

    def close(self) -> None:
        """Закриває браузер. Після тайм-ауту — просто добиває процес:
        ввічливе закриття мертвого драйвера саме може зависнути."""
        if self.dead:
            if pid := self._driver_pid():
                kill_tree(pid)
        else:
            try:
                with Watchdog(30, self._driver_pid, label="закриття браузера"):
                    for obj, meth in ((self._ctx, "close"), (self._browser, "close"),
                                      (self._pw, "stop")):
                        if obj is not None:
                            try:
                                getattr(obj, meth)()
                            except Exception:
                                pass
            except Exception:
                pass
        self._ctx = self._browser = self._pw = None

    def __enter__(self) -> "BrowserFetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
