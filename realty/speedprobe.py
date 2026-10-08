"""Короткий зонд швидкості робочого сайту: `cli.py speed probe` (Блок 2, D49).

Навіщо. Ціль власника — p95 відповіді сервера ≤300 мс на основних сторінках і в
спокої, і під час циклу збору. Заміри Етапу 0 самі навантажили слабкий
ноутбук: два зайві процеси сайту, ~1 ГБ пам'яті, цикл 146 тривав 60 хв (D45,
інцидент 2). Тому зонд — один процес python, одне з'єднання keep-alive до
сайту, що вже працює, на 127.0.0.1: адреси з config/speed.toml [probe] по
черзі, `repeats` разів, з паузою `pause_s` після кожного запиту (240 запитів
≈ 10 хв на фазу).

Що записується на кожен запит: час клієнта, час сервера з заголовка
Server-Timing, чи перебудовувався в цьому запиті знімок «Аналітики» (холодний
запит, `snapshot;dur=…`), розмір, чи йшов цикл і який крок, тиск на
процесор/пам'ять/диск (/proc/pressure, лише Linux) — щоб відрізнити повільний
код від зайнятої машини. Результат — JSON у logs/speed/ і зведення
українською; теплі й холодні запити — окремо (D49: на Fedora ≈0,7 с проти ≈5 с).

Час сервера в зонді включає шлях входу «Basic для скриптів» (перевірка блоку
адреси й пароля на кожен запит) — він дорожчий за куку браузера: на M4
+0,35 мс на запит (≈+3 мс на Fedora; до виправлення D49 було +1,8 мс і
DELETE в ops.db на кожен запит). Журнал часу сайту запити зонда не пише —
там лише браузери людей.

Безпека й побічні дії:
  * лише 127.0.0.1 / localhost / ::1 — пароль власника з .env (AUTH_USER /
    AUTH_PASSWORD) не йде нікуди, крім цієї машини;
  * вхід — наявний шлях «заголовок Basic для скриптів» (без заголовків
    браузера); жодних поблажок для локальних запитів у сайті немає й не
    додається: через тунель Cloudflare запити теж приходять із 127.0.0.1;
  * 401/403/429 — зонд зупиняється одразу: невдалі входи з 127.0.0.1 рахуються
    в ліміт спроб, і десяток поспіль заблокував би скрипти на 15 хв;
  * сторінки квартир (/property/…) ПРОПУСКАЮТЬСЯ: їх відкриття рахується як
    перегляд (від переглядів залежить черга перевірок), а без verify=0 —
    ще й перевіряє оголошення на сайтах джерел. Режиму «не рахувати» в сайту
    немає, тож час цих сторінок береться з журналу часу справжніх відкриттів
    (`cli.py speed report`, /api/status/speed).
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import configfiles, ops
from .config import ROOT, RUN_TIMEOUT

OUT_DIR = ROOT / "logs" / "speed"
STOP_CODES = (401, 403, 429)
_LOCAL_NAMES = {"localhost"}
_SERVER_TIMING = re.compile(r"(?:^|,)\s*app\s*;[^,]*?dur=([0-9.]+)")
_SNAPSHOT_TIMING = re.compile(r"(?:^|,)\s*snapshot\s*;[^,]*?dur=([0-9.]+)")


class ProbeRefused(ValueError):
    """Зонд не запускається (чужа адреса, немає пароля тощо)."""


# --- Підготовка ---------------------------------------------------------------------------


def check_base(base: str) -> str:
    """Лише локальна адреса й без логіна/пароля в ній.

    Пароль власника не має йти за межі машини. І не має потрапити в журнал:
    адреса пишеться у logs/speed/probe-*.json і у вивід, тож «http://u:p@…»
    поклала б пароль у файл, а httpx узяв би його замість AUTH_* з .env.
    Тому таку адресу відхиляємо, а в повідомленнях про відмову повної адреси
    не повторюємо — лише вузол.
    """
    try:
        parts = urlsplit(base)
        host = (parts.hostname or "").lower()
        has_login = parts.username is not None or parts.password is not None
    except ValueError:
        raise ProbeRefused("адреса не розбирається — потрібна http://127.0.0.1:ПОРТ") from None
    if parts.scheme != "http" or not host:
        raise ProbeRefused("потрібна адреса http://127.0.0.1:ПОРТ")
    if has_login:
        raise ProbeRefused(f"{host}: логін і пароль в адресі не потрібні — зонд бере "
                           f"AUTH_* з .env")
    local = host in _LOCAL_NAMES
    if not local:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
    if not local:
        raise ProbeRefused(f"{host}: зонд ходить лише на цю машину (127.0.0.1, localhost, ::1)")
    return base.rstrip("/")


def plan_urls(urls) -> tuple[list[str], list[str]]:
    """(що міряти, що пропущено). Сторінки квартир — пропускаються (див. шапку)."""
    keep, skipped = [], []
    for url in urls:
        (skipped if url.startswith("/property/") else keep).append(url)
    return keep, skipped


def credentials() -> tuple[str, str] | None:
    """Логін і пароль власника з оточення (.env читає realty.config)."""
    user = os.getenv("AUTH_USER", "").strip()
    password = os.getenv("AUTH_PASSWORD", "").strip()
    return (user, password) if user and password else None


def server_ms(header: str | None) -> float | None:
    if not header:
        return None
    m = _SERVER_TIMING.search(header)
    return float(m.group(1)) if m else None


def snapshot_ms(header: str | None) -> float | None:
    """Скільки сервер у цьому запиті перебудовував знімок; None — теплий запит."""
    if not header:
        return None
    m = _SNAPSHOT_TIMING.search(header)
    return float(m.group(1)) if m else None


def pressure() -> dict | None:
    """Тиск на ресурси за 10 с (PSI, «some avg10»), якщо ядро його дає (Linux)."""
    out = {}
    for name in ("cpu", "memory", "io"):
        try:
            text = Path(f"/proc/pressure/{name}").read_text()
        except OSError:
            return None
        m = re.search(r"some avg10=([0-9.]+)", text)
        out[name] = float(m.group(1)) if m else None
    return out


def cycle_now() -> tuple[bool, str | None]:
    try:
        return ops.current_cycle(RUN_TIMEOUT * 1.5)
    except Exception:                               # noqa: BLE001 — зонд не має падати
        return False, None


def wait_for_phase(phase: str, *, max_wait_s: float, poll_s: float,
                   sleep=time.sleep, clock=time.monotonic, reader=cycle_now) -> bool:
    """«cycle» — дочекатися циклу; «dedup» — кроку «дублі». False — не дочекались."""
    if phase == "idle":
        return True
    deadline = clock() + max_wait_s
    while True:
        active, step = reader()
        if active and (phase == "cycle" or (step or "").startswith("дублі")):
            return True
        if clock() >= deadline:
            return False
        sleep(poll_s)


# --- Прогін -------------------------------------------------------------------------------


def run_probe(client, urls: list[str], *, repeats: int, pause_s: float,
              sleep=time.sleep, clock=time.perf_counter, cycle=cycle_now,
              psi=pressure) -> dict:
    """Адреси по черзі `repeats` разів. `client` — httpx.Client (або TestClient)."""
    samples: list[dict] = []
    aborted = None
    for _ in range(repeats):
        for url in urls:
            active, step = cycle()
            started = clock()
            try:
                r = client.get(url)
                body = r.content
            except Exception as e:                  # noqa: BLE001 — рахуємо як збій
                samples.append({"url": url, "status": None, "error": type(e).__name__,
                                "client_ms": round((clock() - started) * 1000, 1),
                                "cycle_active": active, "cycle_step": step})
                sleep(pause_s)
                continue
            took = (clock() - started) * 1000
            if r.status_code in STOP_CODES:
                aborted = (f"{r.status_code} на {url}: зонд зупинено — невдалі входи з "
                           f"127.0.0.1 рахуються в ліміт спроб")
                break
            timing = r.headers.get("server-timing")
            samples.append({"url": url, "status": r.status_code,
                            "client_ms": round(took, 1),
                            "server_ms": server_ms(timing),
                            "snapshot_ms": snapshot_ms(timing),
                            "bytes": len(body), "at": ops.as_utc_iso(ops._now()),
                            "cycle_active": active, "cycle_step": step, "psi": psi()})
            sleep(pause_s)
        if aborted:
            break
    return {"samples": samples, "aborted": aborted}


def _pct(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(1, math.ceil(p / 100 * len(ordered))) - 1], 1)


def summarize(samples: list[dict], urls: list[str], target_ms: float) -> list[dict]:
    rows = []
    for url in urls:
        mine = [s for s in samples if s["url"] == url]
        ok = [s for s in mine if s.get("status") == 200]
        server = [s["server_ms"] for s in ok if s.get("server_ms") is not None]
        warm = [s["server_ms"] for s in ok if s.get("server_ms") is not None
                and s.get("snapshot_ms") is None]
        cold = [s["server_ms"] for s in ok if s.get("server_ms") is not None
                and s.get("snapshot_ms") is not None]
        client = [s["client_ms"] for s in ok]
        p95 = _pct(server, 95)
        rows.append({
            "url": url, "n": len(mine), "errors": len(mine) - len(ok),
            "server_p50": _pct(server, 50), "server_p95": p95,
            "server_max": round(max(server), 1) if server else None,
            # Теплі (знімок готовий) і холодні (знімок будувався в запиті) — окремо.
            "warm_p50": _pct(warm, 50), "warm_p95": _pct(warm, 95),
            "cold": len(cold), "cold_p50": _pct(cold, 50),
            "client_p50": _pct(client, 50), "client_p95": _pct(client, 95),
            "bytes": int(statistics.median([s["bytes"] for s in ok])) if ok else None,
            "in_cycle": sum(1 for s in mine if s.get("cycle_active")),
            "ok": p95 is not None and p95 <= target_ms and len(ok) == len(mine),
        })
    return rows


def render_probe(result: dict) -> str:
    lines = [f"Зонд швидкості: фаза «{result['phase']}», {result['base']}, "
             f"{result['repeats']} повторів, пауза {result['pause_s']} с",
             f"ціль: p95 сервера ≤ {result['target_ms']} мс; час сервера включає вхід "
             f"«Basic для скриптів» (дорожчий за куку браузера, D49)"]
    if result["skipped"]:
        lines.append("пропущено (рахуються як перегляди — див. `cli.py speed report`): "
                     + ", ".join(result["skipped"]))
    lines.append(f"{'адреса':<26}{'n':>4}{'збої':>6}{'сервер p50':>12}{'p95':>8}"
                 f"{'макс':>8}{'тепл. p95':>11}{'холодн.':>9}{'хол. p50':>10}"
                 f"{'клієнт p95':>12}{'КБ':>7}{'у циклі':>9}")
    for r in result["summary"]:
        def f(v):
            return "—" if v is None else f"{v:.0f}"
        mark = "✔" if r["ok"] else "✖"
        lines.append(f"{mark} {r['url']:<24}{r['n']:>4}{r['errors']:>6}{f(r['server_p50']):>12}"
                     f"{f(r['server_p95']):>8}{f(r['server_max']):>8}{f(r.get('warm_p95')):>11}"
                     f"{r.get('cold', 0):>9}{f(r.get('cold_p50')):>10}{f(r['client_p95']):>12}"
                     f"{f((r['bytes'] or 0) / 1024):>7}{r['in_cycle']:>9}")
    if result.get("aborted"):
        lines.append("ЗУПИНЕНО: " + result["aborted"])
    if result.get("file"):
        lines.append(f"усі виміри: {result['file']}")
    return "\n".join(lines)


def main(*, phase: str = "idle", base: str | None = None, repeats: int | None = None,
         as_json: bool = False, client=None) -> int:
    import httpx

    speed, digest = configfiles.load_with_hash("speed")
    cfg, target = speed.probe, speed.targets.server_p95_ms
    try:
        base = check_base(base or cfg.base_url)
    except ProbeRefused as e:
        print(f"ПОМИЛКА: {e}")
        return 2
    urls, skipped = plan_urls(cfg.urls)
    repeats = repeats or cfg.repeats
    if not wait_for_phase(phase, max_wait_s=cfg.phase_wait_max_min * 60,
                          poll_s=cfg.phase_poll_s):
        print(f"Фази «{phase}» не дочекались за {cfg.phase_wait_max_min:.0f} хв — "
              f"зонд не запускався.")
        return 3
    creds = credentials()
    own = client is None
    if own:
        client = httpx.Client(base_url=base, timeout=cfg.timeout_s, follow_redirects=False,
                              auth=creds, headers={"Accept": "*/*",
                                                   "User-Agent": "realty-speed-probe"})
    started = ops._now()
    try:
        raw = run_probe(client, urls, repeats=repeats, pause_s=cfg.pause_s)
    finally:
        if own:
            client.close()
    result = {"phase": phase, "base": base, "repeats": repeats, "pause_s": cfg.pause_s,
              "target_ms": target, "started_at": ops.as_utc_iso(started),
              "finished_at": ops.as_utc_iso(ops._now()),
              "config_hash": digest[:12],
              "skipped": skipped, "aborted": raw["aborted"],
              "summary": summarize(raw["samples"], urls, target), "samples": raw["samples"]}
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"probe-{phase}-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        result["file"] = str(path)
    except OSError as e:
        result["file"] = None
        print(f"УВАГА: результат не збережено ({e})")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"},
                     ensure_ascii=False, indent=1) if as_json else render_probe(result))
    return 1 if raw["aborted"] else 0


# --- Зведення з журналу часу (cli.py speed report) ----------------------------------------


def render_summary(data: dict) -> str:
    t = data["targets"]
    lines = [f"Швидкість сайту (ціль: p95 сервера ≤ {t['server_p95_ms']} мс, перехід ≤ "
             f"{t['tab_switch_ms']} мс), версія конфігу {data['config_hash']}",
             f"Сервер за {data['server']['window_h']:.0f} год "
             f"(у черзі на запис {data['server']['pending']}, відкинуто {data['server']['dropped']}):",
             f"  {'маршрут':<40}{'n':>6}{'p50':>8}{'p95':>8}{'p95 спокій':>12}{'p95 цикл':>10}"
             f"{'p95 тепл.':>11}{'холодн.':>9}{'хол. p50':>10}"]

    def f(v):
        return "—" if v is None else f"{v:.0f}"
    for r in data["server"]["routes"]:
        mark = "✔" if r["ok"] else "✖"
        lines.append(f"{mark} {r['method']} {r['route']:<36}{r['n']:>6}{f(r['p50']):>8}"
                     f"{f(r['p95']):>8}{f(r['idle']['p95']):>12}{f(r['cycle']['p95']):>10}"
                     f"{f(r['warm']['p95']):>11}{r['cold']['n']:>9}{f(r['cold']['p50']):>10}")
    if data["server"].get("truncated"):
        lines.append(f"  (лише {data['server']['rows']} найновіших запитів — стеля summary.max_rows)")
    rum = data["rum"]
    lines.append(f"Браузер за {rum['window_days']:.0f} дн.: переходів {rum['all']['n']}, на "
                 f"відкритому з'єднанні p95 {f(rum['reused']['p95'])} мс, з телефона "
                 f"p95 {f(rum['phone_reused']['p95'])} мс; кнопки p95 {f(rum['buttons']['p95'])} мс"
                 + (f"; понад ліміт маячків не записано {rum['over_limit']}"
                    if rum.get("over_limit") else "")
                 + (" (лише найновіші — стеля summary.max_rows)" if rum.get("truncated") else ""))
    if data["write_windows"]:
        lines.append("Вікна запису кроків циклу (найдовше / сума, мс):")
        for w in data["write_windows"]:
            lines.append(f"  {w['at']}  {(w['step'] or w['process'] or '?'):<34}"
                         f"{w['max_ms']:>9.0f}{w['total_ms']:>10.0f}  {w['txns']} тр.")
    return "\n".join(lines)


# --- Пріоритети служб (cli.py speed priorities) — лише читання ----------------------------


UNITS = ("realty-web.service", "realty-cycle.service", "realty-night.service")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def priorities() -> dict:
    """Що ДІЄ насправді: cgroup-ваги, nice, ionice, планувальник диска, RSS сайту.

    Лише читання файлів /proc і /sys та `systemctl --user show` — нічого не
    змінює (план Блоку 2, крок 10: пріоритети без sudo перевіряються за
    файлами cgroup). На Mac — порожньо з поясненням.
    """
    import shutil
    import subprocess

    out: dict = {"platform": os.uname().sysname, "units": {}, "disks": {}}
    if out["platform"] != "Linux" or not shutil.which("systemctl"):
        out["note"] = "лише Linux із systemd (Fedora)"
        return out
    uid = os.getuid()
    base = Path("/sys/fs/cgroup")
    out["user_controllers"] = _read(
        base / f"user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers")
    for unit in UNITS:
        try:
            shown = subprocess.run(["systemctl", "--user", "show", unit, "-p",
                                    "MainPID,ControlGroup,ActiveState"],
                                   capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        info: dict = {"state": props.get("ActiveState"), "pid": int(props.get("MainPID") or 0)}
        cg = props.get("ControlGroup") or ""
        if cg:
            for name in ("cpu.weight", "io.weight", "memory.low", "memory.high", "memory.max"):
                info[name] = _read(base / cg.lstrip("/") / name)
        pid = info["pid"]
        if pid:
            stat = _read(Path(f"/proc/{pid}/stat")) or ""
            fields = stat.rsplit(")", 1)[-1].split()
            info["nice"] = int(fields[16]) if len(fields) > 16 else None
            status = _read(Path(f"/proc/{pid}/status")) or ""
            m = re.search(r"VmRSS:\s+(\d+) kB", status)
            info["rss_mb"] = round(int(m.group(1)) / 1024, 1) if m else None
            info["oom_score_adj"] = _read(Path(f"/proc/{pid}/oom_score_adj"))
            if shutil.which("ionice"):
                try:
                    info["ionice"] = subprocess.run(["ionice", "-p", str(pid)],
                                                    capture_output=True, text=True,
                                                    timeout=5).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    info["ionice"] = None
        out["units"][unit] = info
    for disk in sorted(Path("/sys/block").glob("*")):
        if disk.name.startswith(("loop", "ram", "zram")):
            continue
        out["disks"][disk.name] = _read(disk / "queue" / "scheduler")
    out["meminfo"] = {k: v for k, v in re.findall(
        r"^(MemTotal|MemAvailable|SwapFree):\s+(\d+ kB)", _read(Path("/proc/meminfo")) or "", re.M)}
    out["psi"] = pressure()
    return out


def render_priorities(data: dict) -> str:
    if data.get("note"):
        return f"Пріоритети служб: {data['note']}."
    lines = [f"Контролери cgroup для служб користувача: {data.get('user_controllers') or '—'}"]
    for unit, info in data["units"].items():
        lines.append(f"{unit}: {info.get('state')}, PID {info.get('pid') or '—'}, "
                     f"nice {info.get('nice')}, ionice «{info.get('ionice') or '—'}», "
                     f"cpu.weight {info.get('cpu.weight') or '—'}, io.weight "
                     f"{info.get('io.weight') or '—'}, memory.low {info.get('memory.low') or '—'}, "
                     f"high {info.get('memory.high') or '—'}, max {info.get('memory.max') or '—'}, "
                     f"oom_score_adj {info.get('oom_score_adj')}, RSS {info.get('rss_mb')} МБ")
    for disk, sched in data["disks"].items():
        lines.append(f"диск {disk}: планувальник {sched}")
    lines.append(f"пам'ять: {data.get('meminfo')}, PSI: {data.get('psi')}")
    return "\n".join(lines)
