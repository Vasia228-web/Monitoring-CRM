"""Рендер OLX у Chromium для нічної смуги olx.ua — Блоки 3/4 (E11, D60).

Той самий Playwright Chromium, що й у збору OLX (fetcher.BrowserFetcher), але для ночі:
  * код відповіді без винятків (401/403/429 — у блокування смуги, 410/404 — «не вдалось»),
    кешу на диску немає (відрендерена сторінка містить ім'я продавця — не зберігаємо):
    `--disk-cache-size` у launch_args і власний каталог профілю, який стирається на
    старті браузера й на закритті (і після вбивства сторожем — на наступному старті);
  * один браузер, послідовно; зображення, відео й шрифти не вантажимо (лише розмітка:
    менше пам'яті й трафіку), перезапуск кожні restart_every рендерів;
  * стеля одного рендера — render_timeout_seconds (сторож убиває браузер і він
    перезапускається на наступному рендері);
  * пам'ять — перед кожним рендером: MemAvailable машини (3,7 ГБ на Fedora; Chromium до
    814 МБ) і запас до memory.high власної cgroup (юніт realty-night: MemoryHigh 1800M) —
    менше за min_mem_available_mb — рендери стоять до кінця вікна.
Паузу старт-до-старту й дедлайн тримає смуга (night.lane, ворота), не цей клас.
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import ops
from ..config import DATA_DIR, REQUEST_TIMEOUT, USER_AGENT
from ..fetcher import BLOCKING_CODES, Watchdog, kill_tree

log = logging.getLogger(__name__)

# Каталог профілю нічного Chromium (кеш, cookie, сховище сторінок OLX з іменами
# продавців): стирається на старті браузера й на закритті. Не /tmp: на Fedora це tmpfs,
# тобто та сама оперативна пам'ять, якої рендерам і так бракує.
PROFILE_DIR = DATA_DIR / "night-chromium"
# Ввічливе закриття браузера (перезапуск кожні restart_every) — під сторожем стільки с.
CLOSE_TIMEOUT_S = 30


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


def _cgroup_value(path: Path) -> int | None:
    """Число з файла cgroup v2 (memory.current, memory.high); «max» чи немає — None."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text or text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def cgroup_headroom_mb(proc_cgroup: Path = Path("/proc/self/cgroup"),
                       root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """Запас до memory.high (якщо не задано — memory.max) власної cgroup v2 і її предків,
    МБ: найменший серед рівнів, де межу задано. Chromium живе в cgroup юніта
    realty-night (MemoryHigh=1800M): машина може мати вільну пам'ять, а юніт — ні, і
    тоді ядро душить усю смугу (рецензія E11, 08.10). None — cgroup v2 немає (macOS) чи
    межі не задано ніде."""
    try:
        lines = proc_cgroup.read_text().splitlines()
    except OSError:
        return None
    rel = next((ln.split(":", 2)[2] for ln in lines if ln.startswith("0::")), None)
    if rel is None:
        return None
    best: int | None = None
    d = root / rel.lstrip("/")
    while True:
        limit = _cgroup_value(d / "memory.high")
        if limit is None:
            limit = _cgroup_value(d / "memory.max")
        current = _cgroup_value(d / "memory.current")
        if limit is not None and current is not None:
            room = max(0, limit - current) // (1024 * 1024)
            best = room if best is None else min(best, room)
        if d == root or root not in d.parents:
            break
        d = d.parent
    return best


def render_headroom_mb() -> int | None:
    """Скільки пам'яті є для рендера, МБ: менше з MemAvailable машини й запасу до
    memory.high власної cgroup; None — невідомо обидва (macOS)."""
    vals = [v for v in (mem_available_mb(), cgroup_headroom_mb()) if v is not None]
    return min(vals) if vals else None


class NightRenderer:
    def __init__(self, rcfg, *, label: str = "olx", profile_dir: Path | None = None) -> None:
        self.cfg = rcfg
        self.label = label
        self.renders = 0
        self._since_start = 0
        self._pw = self._browser = self._ctx = None
        self.profile_dir = Path(profile_dir or PROFILE_DIR)

    def _driver_pid(self) -> int | None:
        try:
            return self._pw._impl_obj._connection._transport._proc.pid
        except Exception:                               # noqa: BLE001
            return None

    def launch_allowance(self) -> float:
        """Скільки секунд до рендера може піти на (пере)запуск браузера: смуга додає це до
        запасу дедлайну (рецензія E11, 08.10) — рендер із перезапуском не має перейти
        stop_requests + kill_grace. Запуск — під сторожем render_timeout_seconds,
        ввічливе закриття — під CLOSE_TIMEOUT_S."""
        if self._ctx is None:
            return float(self.cfg.render_timeout_seconds)
        if self._since_start >= self.cfg.restart_every:
            return float(self.cfg.render_timeout_seconds + CLOSE_TIMEOUT_S)
        return 0.0

    def _wipe_profile(self) -> None:
        shutil.rmtree(self.profile_dir, ignore_errors=True)

    def _ensure(self) -> None:
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright

        # Профіль, що лишився після вбивства (сторож, kill смуги, OOM), — стерти до старту.
        self._wipe_profile()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        dog = Watchdog(self.cfg.render_timeout_seconds, self._driver_pid, label="нічний браузер")
        try:
            with dog:
                # Власний профіль (launch_persistent_context): тимчасовий профіль
                # launch() лежить у /tmp і після вбивства драйвера там і лишається.
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=True,
                    args=[*self.cfg.launch_args,
                          f"--disk-cache-dir={self.profile_dir / 'cache'}"],
                    locale="uk-UA", timezone_id="Europe/Kyiv",
                    viewport={"width": 1366, "height": 900}, user_agent=USER_AGENT)
                blocked = set(self.cfg.block_resources)
                if blocked:
                    def route(r):
                        if r.request.resource_type in blocked:
                            r.abort()
                        else:
                            # fallback, а не continue_: інші обробники (сторож мережі в
                            # тестах) теж бачать запит; без них — те саме, що continue_.
                            r.fallback()

                    self._ctx.route("**/*", route)
                ms = int(min(REQUEST_TIMEOUT, self.cfg.render_timeout_seconds) * 1000)
                self._ctx.set_default_timeout(ms)
                self._ctx.set_default_navigation_timeout(ms)
        except Exception:
            if dog.fired:
                # Запуск завис і сторож убив драйвер: далі — лише зупинка (не ввічливе
                # закриття, воно на мертвому драйвері крутиться вічно).
                self.close(dead=True)
                raise TimeoutError("сторож убив браузер під час запуску") from None
            raise
        if dog.fired:
            self.close(dead=True)
            raise TimeoutError("сторож убив браузер під час запуску")
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
                    # Сторож уже вбив драйвер: будь-який виклик Playwright після цього
                    # (і page.close()) крутиться вічно на 100% процесора — у циклі
                    # очікування мертвого диспетчера (рецензія E11, 08.10; перевірено на
                    # справжньому Chromium). Закриває все close(dead=True).
                    if not dog.fired:
                        try:
                            page.close()
                        except Exception:                # noqa: BLE001
                            pass
            if dog.fired:
                # Операція повернулась у ту ж мить, коли сторож спрацював: браузер уже
                # вбитий — як помилка рендера (наступний рендер підніме новий браузер).
                raise TimeoutError("сторож убив браузер")
        except Exception as e:                           # noqa: BLE001 — відповідь, не виняток
            fired = (dog is not None and dog.fired) or isinstance(e, TimeoutError)
            if self._pw is not None:
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
            # Екземпляр Playwright треба зупинити й після вбивства: інакше його цикл
            # подій лишається «запущеним» у цьому потоці, і наступний
            # sync_playwright().start() падає («Sync API inside the asyncio loop») —
            # тоді кожен рендер до кінця вікна — помилка (рецензія E11, 08.10).
            # stop() мертвого драйвера не чекає: транспорт уже закрито (перевірено на
            # справжньому Chromium, tests/test_night_render_real.py).
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:                        # noqa: BLE001
                    log.warning("нічний браузер: Playwright після вбивства не зупинився",
                                exc_info=True)
        else:
            try:
                with Watchdog(CLOSE_TIMEOUT_S, self._driver_pid,
                              label="закриття нічного браузера"):
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
        self._wipe_profile()
