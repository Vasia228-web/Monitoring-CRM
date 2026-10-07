"""Інструмент рівності сторінок (scripts/page_equality.py) сам не має брехати.

Він — єдиний доказ вимоги власника «вміст сторінок до і після збігається»
(Блок 2). Тут перевірено: нормалізація прибирає лише мінливе й ідемпотентна;
заморожений годинник не ламає isinstance; на тій самій копії повторний рендер
дає 0 розбіжностей, а підмінений вміст розбіжність дає; чужа копія бази —
відмова, а не «усе збіглось».
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "page_equality.py"


def _module():
    spec = importlib.util.spec_from_file_location("page_equality_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pe = _module()


# --- Нормалізація -------------------------------------------------------------------------


def test_json_volatile_keys_are_masked_everything_else_kept():
    body = json.dumps({"generated_at": "2026-10-07T09:00:01+00:00",
                       "worker": {"age_min": 57.2, "state": "idle"},
                       "runs": [{"started_at": "2026-10-07T06:05:51"}],
                       "count": 3}).encode()
    out = json.loads(pe.normalize(body, "application/json"))
    assert out["generated_at"] == pe.PLACEHOLDER
    # Вік серцебиття — від даних і замороженого «зараз», тож детермінований:
    # маска сховала б саме застарілий кеш, який порівняння має ловити.
    assert out["worker"] == {"age_min": 57.2, "state": "idle"}
    assert out["runs"][0]["started_at"] == "2026-10-07T06:05:51"     # дані — не чіпаємо
    assert out["count"] == 3


def test_html_rules_touch_only_their_fragment():
    html = ('<p>Ціна 50 000</p><div class="updated">оновлено сьогодні</div>'
            '<!--rum--><script>beacon()</script><!--/rum--><b>3 дні тому</b>').encode()
    out = pe.normalize(html, "text/html; charset=utf-8").decode()
    # «оновлено сьогодні» лишається: при замороженому «зараз» воно стале.
    assert out == ('<p>Ціна 50 000</p><div class="updated">оновлено сьогодні</div>'
                   '<b>3 дні тому</b>')


def test_network_attempt_during_render_is_reported_not_swallowed(capsys):
    """Код, що проковтнув заблоковану спробу (запасне значення замість справжнього),
    не має тихо потрапити в еталон: спроби з журналу заборони — це відмова."""
    class Guard:
        def __init__(self, items):
            self.items = items

        def attempts(self):
            return list(self.items)

    assert pe._network_attempts({"_ng": Guard([])}) == []
    assert capsys.readouterr().out == ""
    got = pe._network_attempts({"_ng": Guard(["httpx: https://bank.gov.example/x"])})
    assert got == ["httpx: https://bank.gov.example/x"]
    assert "рендер намагався вийти в мережу" in capsys.readouterr().out


@pytest.mark.parametrize("ctype,body", [
    ("application/json", b'{"generated_at": "x", "a": [1, 2.5, "\\u0456"]}'),
    ("text/html; charset=utf-8", '<div class="updated">оновлено вчора</div>'.encode()),
    ("text/plain", "Потрібна авторизація".encode()),
])
def test_normalization_is_idempotent(ctype, body):
    once = pe.normalize(body, ctype)
    assert pe.normalize(once, ctype) == once


def test_frozen_clock_keeps_isinstance_and_returns_real_datetimes():
    now = dt.datetime(2026, 10, 7, 8, 0, 0)
    fdt, fdate, shim = pe._frozen_classes(now)
    assert fdt.utcnow() == now
    assert fdt.now(dt.timezone.utc) == now.replace(tzinfo=dt.timezone.utc)
    real = dt.datetime(2026, 1, 1, 12, 0)
    assert isinstance(real, fdt) and isinstance(real.date(), fdate)
    built = fdt(2026, 1, 2)
    assert type(built) is dt.datetime                      # конструктор дає справжню дату
    assert type(fdt.fromisoformat("2026-01-02T03:04:05")) is dt.datetime
    assert shim.datetime is fdt and shim.timedelta is dt.timedelta


# --- Знімок і порівняння на копії тестової бази ---------------------------------------------


def _run(*args, timeout=300):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    """Невеликий еталон із копії тестової бази (conftest): по 2 адреси на групу."""
    db = Path(os.environ["DB_URL"].removeprefix("sqlite:///"))
    ops = Path(os.environ["OPS_DB_URL"].removeprefix("sqlite:///"))
    if not db.exists() or db.stat().st_size == 0:
        pytest.skip("у тестовій копії немає даних")
    base = tmp_path_factory.mktemp("page-eq")
    out = base / "golden"
    r = _run("snapshot", "--db", str(db), "--ops", str(ops), "--out", str(out),
             "--work", str(base / "work"), "--properties", "3", "--in-progress", "3",
             "--limit-per-group", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    return {"db": db, "ops": ops, "out": out, "base": base}


def test_snapshot_writes_manifest_with_hashes(golden):
    m = json.loads((golden["out"] / "manifest.json").read_text())
    assert m["totals"]["urls"] == len(m["urls"]) > 10
    assert m["environment"]["pythonhashseed"] == "0"
    assert m["environment"]["import_failed"] == []
    assert not any(k.startswith("_") for k in m["environment"])
    assert m["network_attempts"] == []
    # З якого коду й з якими порогами знято еталон.
    assert len(m["code"]["app_diff_sha256"]) == 64 and isinstance(m["code"]["app_dirty"], list)
    from realty import configfiles
    assert set(m["inputs"]["config"]) == set(configfiles.SCHEMAS)
    assert all(len(v) == 64 for v in m["inputs"]["config"].values())
    for e in m["urls"]:
        body = gzip.decompress((golden["out"] / e["file"]).read_bytes())
        assert hashlib.sha256(body).hexdigest() == e["sha256"]
        assert "verify=0" in e["url"] or not e["url"].startswith("/property/")


def test_rerender_is_equal_and_tampering_is_caught(golden):
    out = golden["out"]
    m = json.loads((out / "manifest.json").read_text())
    victim = next(e for e in m["urls"] if e["group"] == "list")
    path = out / victim["file"]
    original = path.read_bytes()
    text = gzip.decompress(original).decode()
    assert "<table" in text or "<tr" in text
    path.write_bytes(gzip.compress(text.replace("<tr", "<tr data-x", 1).encode(), mtime=0))
    try:
        r = _run("compare", "--db", str(golden["db"]), "--ops", str(golden["ops"]),
                 "--golden", str(out), "--work", str(golden["base"] / "work"),
                 "--order", "reverse")
    finally:
        path.write_bytes(original)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"різних: 1 з {len(m['urls'])}" in r.stdout
    assert victim["url"] in r.stdout and "data-x" in r.stdout


def test_compare_refuses_another_database(golden):
    r = _run("compare", "--db", str(golden["ops"]), "--ops", str(golden["ops"]),
             "--golden", str(golden["out"]), "--work", str(golden["base"] / "work"))
    assert r.returncode == 2 and "не та, з якої знято еталон" in r.stdout
