"""Фоновий розклад інкрементального збору (launchd на macOS, cron як запасний).

Планувальник запускає `scripts/run_incremental.sh`, тобто той самий
`cli.py scrape` у режимі `fresh`: джерела, що віддають найновіші оголошення
першими, зупиняються на першій сторінці без новинок, тож регулярний прогін
коштує лічені хвилини.
"""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from .config import ROOT

LABEL = "com.yavasia.realty.incremental"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
RUNNER = ROOT / "scripts" / "run_incremental.sh"
DEFAULT_INTERVAL = 3 * 3600  # кожні 3 години

# Власні логи launchd пишемо поза каталогом проєкту: якщо проєкт лежить у
# ~/Desktop чи ~/Documents, доступ до нього агенту може блокувати TCC — і тоді
# лог усередині проєкту теж не створиться, а причину збою не видно.
AGENT_LOG_DIR = Path.home() / "Library" / "Logs" / "realty"


def build_plist(interval: int = DEFAULT_INTERVAL) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(RUNNER)],
        "WorkingDirectory": str(ROOT),
        "StartInterval": interval,
        # Один прогін одразу після завантаження — щоб не чекати перший інтервал.
        "RunAtLoad": True,
        "StandardOutPath": str(AGENT_LOG_DIR / "launchd.out.log"),
        "StandardErrorPath": str(AGENT_LOG_DIR / "launchd.err.log"),
        "ProcessType": "Background",
        # Не будити машину заради збору: пропущений інтервал надолужиться.
        "LowPriorityIO": True,
    }


# macOS не пускає фонові агенти в ці каталоги без Full Disk Access: launchd
# отримує «Operation not permitted» ще до запуску скрипта.
TCC_PROTECTED = ("Desktop", "Documents", "Downloads")


def tcc_warning() -> str | None:
    """Попередження, якщо проєкт лежить у каталозі, закритому для агентів."""
    try:
        rel = ROOT.relative_to(Path.home())
    except ValueError:
        return None
    if not rel.parts or rel.parts[0] not in TCC_PROTECTED:
        return None
    return (
        f"Проєкт лежить у ~/{rel.parts[0]} — macOS (TCC) не пускає туди фонові\n"
        f"агенти launchd, і розклад мовчки не працюватиме. Два виходи:\n"
        f"  1) перенести проєкт поза ~/{rel.parts[0]}, наприклад у ~/realty;\n"
        f"  2) видати Full Disk Access для /bin/bash у Системних параметрах →\n"
        f"     Конфіденційність і безпека → Повний доступ до диска.\n"
        f"Без цього працює лише вбудований воркер: python cli.py schedule worker"
    )


def install(interval: int = DEFAULT_INTERVAL) -> str:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(build_plist(interval), fh)
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)],
                   capture_output=True, check=False)
    r = subprocess.run(["launchctl", "load", str(PLIST_PATH)],
                       capture_output=True, text=True)
    if r.returncode:
        return f"не вдалося завантажити: {r.stderr.strip() or r.stdout.strip()}"
    msg = (f"розклад увімкнено: кожні {interval // 3600} год "
           f"({interval} с)\n  plist: {PLIST_PATH}\n  лог:   {ROOT / 'logs' / 'scheduler.log'}")
    if warn := tcc_warning():
        msg += "\n\nУВАГА\n" + warn
    return msg


def uninstall() -> str:
    if not PLIST_PATH.exists():
        return "розклад не встановлено"
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)],
                   capture_output=True, check=False)
    PLIST_PATH.unlink()
    return f"розклад вимкнено, {PLIST_PATH} видалено"


def status() -> str:
    if not PLIST_PATH.exists():
        return "розклад не встановлено (python cli.py schedule install)"
    r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    if r.returncode:
        return f"plist є ({PLIST_PATH}), але агент не завантажений"
    fields = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip().strip('"')] = v.strip().rstrip(";").strip()
    exit_code = int(fields.get("LastExitStatus", 0) or 0)
    lines = []
    log = ROOT / "logs" / "scheduler.log"
    if log.exists():
        lines = [ln for ln in log.read_text(errors="ignore").splitlines() if ln.strip()]
    out = [f"розклад активний",
           f"  PID: {fields.get('PID', 'не запущений зараз')}",
           f"  останній код виходу: {exit_code}"]
    if lines:
        out.append(f"  останнє в лозі: {lines[-1][:96]}")
    err = AGENT_LOG_DIR / "launchd.err.log"
    if exit_code and err.exists() and err.stat().st_size:
        out.append("  помилка агента: " + err.read_text(errors="ignore").strip()[-200:])
    return "\n".join(out)


def cron_line(interval_hours: int = 3) -> str:
    """Запасний варіант для систем без launchd."""
    return f"0 */{interval_hours} * * * cd {ROOT} && /bin/bash {RUNNER}"


def worker(interval: int = DEFAULT_INTERVAL, runs: int | None = None) -> int:
    """Вбудований періодичний воркер.

    Працює в межах сесії, з якої запущений, тому не впирається в TCC: права
    успадковуються від термінала. Це робочий варіант, поки не вирішено питання
    з розташуванням проєкту або Full Disk Access.
    """
    import logging
    import time

    from .pipeline import Pipeline

    log = logging.getLogger("worker")
    done = 0
    while runs is None or done < runs:
        started = time.monotonic()
        try:
            report = Pipeline(mode="fresh").run()
            log.info("Прогін завершено: +%d нових, %d оновлено, %.0f с",
                     report.inserted, report.updated, time.monotonic() - started)
            print(report.render())
        except KeyboardInterrupt:
            log.info("Зупинено користувачем")
            return 0
        except Exception:
            log.exception("Прогін впав — чекаємо наступного інтервалу")
        done += 1
        if runs is not None and done >= runs:
            break
        log.info("Наступний прогін через %d хв", interval // 60)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Зупинено користувачем")
            return 0
    return 0
