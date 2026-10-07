"""Юніти systemd: сайт має пріоритет над збором — лише засобами без root (Блок 2, D50).

Умова власника: «якщо під час циклу сайт гальмує через пам'ять чи процесор —
сайт має пріоритет над збором». Сервіси — користувацькі (systemd --user, sudo
немає), тож у юнітах лише те, що діє без root і не зламає старт служби;
решта (делегування io, memory.low) — дія власника, описана в журналі.
"""
from __future__ import annotations

import configparser
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "deploy" / "fedora" / "systemd"
sys.path.insert(0, str(ROOT))


def _section(name: str, section: str) -> dict[str, str]:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(UNITS / name, encoding="utf-8")
    return dict(p[section])


def _service(name: str) -> dict[str, str]:
    return _section(name, "Service")


def _seconds(span: str) -> float:
    """Проміжок часу systemd («3min», «180s», «180», «2min 30s») → секунди."""
    units = {"": 1, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600, "hr": 3600}
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", span.strip())
    assert parts, span
    return sum(float(n) * units[u] for n, u in parts)


def test_web_is_favoured_over_the_cycle():
    web, cycle = _service("realty-web.service"), _service("realty-cycle.service")
    assert int(web["CPUWeight"]) > int(cycle["CPUWeight"])
    assert (web["IOSchedulingClass"], cycle["IOSchedulingClass"]) == ("best-effort",) * 2
    assert int(web["IOSchedulingPriority"]) < int(cycle["IOSchedulingPriority"])
    assert int(cycle["Nice"]) > int(web.get("Nice", "0"))
    # За нестачі пам'яті ядро вбиває Chromium збору, а не сайт.
    assert int(cycle["OOMScoreAdjust"]) > 0 and "OOMScoreAdjust" not in web
    # Ліміти пам'яті циклу — без змін.
    assert (cycle["MemoryHigh"], cycle["MemoryMax"]) == ("1800M", "2600M")


def test_only_directives_that_work_without_root():
    """IOWeight (контролер io не делеговано), MemoryLow (memory.low у батьківських
    групах) і від'ємні Nice/OOMScoreAdjust без root не діють — їх у юнітах немає."""
    for unit in UNITS.glob("*.service"):
        svc = _service(unit.name)
        assert "IOWeight" not in svc and "MemoryLow" not in svc, unit.name
        assert int(svc.get("Nice", "0")) >= 0, unit.name
        assert int(svc.get("OOMScoreAdjust", "0")) >= 0, unit.name
        assert svc.get("IOSchedulingClass", "best-effort") in ("best-effort", "idle"), unit.name


def test_background_jobs_yield_to_the_web():
    web = int(_service("realty-web.service")["CPUWeight"])
    for name in ("realty-identity.service", "realty-backup.service",
                 "realty-dedup-sample.service", "realty-lookup@.service"):
        svc = _service(name)
        assert int(svc["CPUWeight"]) < web and int(svc["IOSchedulingPriority"]) == 7, name


def test_lookup_worker_template_runs_one_job_with_a_time_limit():
    from realty import configfiles

    svc = _service("realty-lookup@.service")
    assert svc["Type"] == "oneshot"
    assert svc["ExecStart"].endswith("cli.py lookup check --job %i")
    # Після job_timeout_s сайт перестає чекати завдання — systemd зупиняє процес тоді ж.
    open_check = configfiles.load("speed").open_check
    assert _seconds(svc["TimeoutStartSec"]) == open_check.job_timeout_s
    assert open_check.drain_budget_s < open_check.job_timeout_s
    assert "MemoryMax" in svc
    # Екземпляри, що впали (тайм-аут, OOM), не накопичуються в `--failed`.
    assert _section("realty-lookup@.service", "Unit")["CollectMode"] == "inactive-or-failed"


PRIORITY = ("Nice", "CPUWeight", "IOSchedulingClass", "IOSchedulingPriority",
            "OOMScoreAdjust", "MemoryHigh", "MemoryMax")


def _probes() -> list[dict[str, str]]:
    """Набори `-p K=V` кожного пробного `probe …` в install.sh."""
    text = (ROOT / "deploy" / "fedora" / "install.sh").read_text(encoding="utf-8")
    block = text[text.index("if probe "):text.index("; then", text.index("if probe "))]
    return [dict(re.findall(r"-p (\w+)=(\S+)", chunk))
            for chunk in re.split(r"&&\s*probe", block.replace("\\\n", " "))]


def test_install_probes_every_priority_set_before_installing_units():
    """Пріоритети без root можуть дати EPERM — тоді служба не стартувала б зовсім.
    Тож install.sh пробує саме ті налаштування, що в юнітах сайту, циклу й
    процесу перевірки (рецензія: realty-lookup@ не пробувався)."""
    probes = _probes()
    for name, keys in (("realty-web.service", PRIORITY[:5]),
                       ("realty-cycle.service", PRIORITY[:5]),
                       ("realty-lookup@.service", PRIORITY)):
        svc = _service(name)
        want = {k: svc[k] for k in keys if k in svc}
        assert any(all(p.get(k) == v for k, v in want.items()) for p in probes), (name, want)
