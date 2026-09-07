"""Дашборд стану: схема API, ручні тригери, ізоляція телеметрії."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from realty import ops
from realty.config import SOURCES
from realty.web import status as status_mod
from realty.web.app import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Тести не пишуть у каталог проєкту: логи запусків ідуть у тимчасовий."""
    monkeypatch.setattr(status_mod, "ROOT", tmp_path)
    status_mod._jobs.clear()
    yield
    status_mod._jobs.clear()


# --- ізоляція телеметрії ------------------------------------------------------

def test_telemetry_lives_in_its_own_database():
    """Вимога ТЗ: логування дашборда не засмічує основну базу."""
    from sqlalchemy import inspect

    from realty.db import engine as main_engine

    ops.init_ops()
    main_tables = set(inspect(main_engine).get_table_names())
    ops_tables = set(inspect(ops.engine).get_table_names())

    assert {"runs", "heartbeat"} <= ops_tables
    assert not ({"runs", "heartbeat"} & main_tables), "телеметрія потрапила в основну базу"
    assert "listings" not in ops_tables, "оголошення потрапили в базу телеметрії"
    assert str(ops.engine.url) != str(main_engine.url)


# --- схема API ----------------------------------------------------------------

def test_status_page_renders(client):
    r = client.get("/status")
    assert r.status_code == 200
    assert "Стан системи" in r.text
    for name in SOURCES:                     # кнопка на кожне джерело
        assert f'data-run="{name}"' in r.text
    assert 'data-run="all"' in r.text        # і глобальна


def test_api_status_schema(client):
    d = client.get("/api/status").json()
    assert {"generated_at", "worker", "sources", "quality", "llm", "runs", "jobs"} <= set(d)

    w = d["worker"]
    assert {"state", "beat_at", "age_min", "heartbeats", "running_now",
            "idle_after_min", "down_after_min"} <= set(w)
    assert w["state"] in {"active", "idle", "down"}

    assert {s["name"] for s in d["sources"]} == set(SOURCES)
    for s in d["sources"]:
        assert {"collected", "expected", "remaining", "coverage", "success_rate",
                "blocked_24h", "new_24h", "last_success", "running"} <= set(s)
        assert isinstance(s["collected"], int)
        if s["success_rate"] is not None:
            assert 0 <= s["success_rate"] <= 100

    q = d["quality"]
    assert q["listings"] >= q["properties"] >= 0
    assert q["merged"] == q["listings"] - q["properties"]

    for window in ("all_time", "last_24h"):
        assert {"calls", "in_tokens", "out_tokens", "cost_usd"} <= set(d["llm"][window])


def test_api_runs_endpoint(client):
    r = client.get("/api/status/runs?limit=5")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# --- ручні тригери ------------------------------------------------------------

class _FakeProc:
    """Підміна процесу: тест не має запускати справжній збір."""

    def __init__(self, *a, **kw):
        self.pid = 4242
        self._alive = True

    def poll(self):
        return None if self._alive else 0


def test_manual_trigger_starts_background_process(client, monkeypatch):
    launched = {}

    def fake_popen(cmd, **kw):
        launched["cmd"] = cmd
        launched["cwd"] = kw.get("cwd")
        return _FakeProc()

    monkeypatch.setattr(status_mod.subprocess, "Popen", fake_popen)
    r = client.post("/api/status/run", json={"source": "olx"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["pid"] == 4242

    cmd = launched["cmd"]
    assert "scrape" in cmd and "--sources" in cmd and "olx" in cmd
    # Прогін має бути позначений як ручний — інакше телеметрія збреше про джерело запуску.
    assert cmd[cmd.index("--trigger") + 1] == "manual"


def test_global_trigger_runs_every_source(client, monkeypatch):
    launched = {}
    monkeypatch.setattr(status_mod.subprocess, "Popen",
                        lambda cmd, **kw: (launched.setdefault("cmd", cmd), _FakeProc())[1])
    assert client.post("/api/status/run", json={"source": "all"}).status_code == 200
    assert "--sources" not in launched["cmd"], "глобальний запуск не має обмежувати джерела"


def test_unknown_source_is_rejected(client):
    r = client.post("/api/status/run", json={"source": "хтозна-що"})
    assert r.status_code == 400 and r.json()["ok"] is False


def test_second_run_of_the_same_source_is_refused(client, monkeypatch):
    monkeypatch.setattr(status_mod.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    assert client.post("/api/status/run", json={"source": "lun"}).status_code == 200
    second = client.post("/api/status/run", json={"source": "lun"})
    assert second.status_code == 409, "два збори того самого джерела одночасно"
    assert "lun" in client.get("/api/status").json()["jobs"]


# --- здоров'я воркера ---------------------------------------------------------

def test_worker_state_reflects_silence(monkeypatch):
    ops.init_ops()
    ops.beat("тест", busy=False)
    assert ops.worker_health()["state"] in {"idle", "active"}

    old = ops._now() - timedelta(minutes=ops.DOWN_AFTER_MIN + 30)
    with ops.ops_session() as s:
        hb = s.get(ops.Heartbeat, 1)
        hb.beat_at, hb.busy = old, False
    health = ops.worker_health()
    assert health["state"] == "down"
    assert health["alert"], "мовчання воркера має давати сигнал тривоги"

    ops.beat("відновлено", busy=False)       # повертаємо стан для інших тестів
    assert ops.worker_health()["state"] != "down"


def test_run_record_captures_request_counters():
    ops.take_counts()
    run_id = ops.start_run("тест-джерело", trigger="manual")
    ops.record_request("тест-джерело", ok=True)
    ops.record_request("тест-джерело", ok=False, blocked=True)
    counts = ops.take_counts("тест-джерело")
    ops.finish_run(run_id, status="ok", requests_ok=counts["ok"],
                   requests_failed=counts["failed"], requests_blocked=counts["blocked"])

    with ops.ops_session() as s:
        run = s.get(ops.RunRecord, run_id)
        assert run.requests_ok == 1 and run.requests_failed == 1
        assert run.requests_blocked == 1 and run.finished_at is not None
        s.delete(run)


def test_success_rate_is_computed_from_runs():
    ops.init_ops()
    run_id = ops.start_run("проба-успіху", trigger="manual")
    ops.finish_run(run_id, status="ok", requests_ok=9, requests_failed=1)
    stats = ops.source_stats(24)["проба-успіху"]
    assert stats["success_rate"] == 90.0
    with ops.ops_session() as s:
        s.delete(s.get(ops.RunRecord, run_id))


def test_timestamps_carry_a_timezone(client):
    """Час у базі зберігається в UTC без зони; віддавати його так само не можна —
    браузер прочитає мітку як місцеву й покаже похибку в кілька годин."""
    d = client.get("/api/status").json()
    stamps = [d["generated_at"], d["worker"]["beat_at"]]
    stamps += [s["last_success"] for s in d["sources"]]
    stamps += [r["started_at"] for r in d["runs"]]
    present = [t for t in stamps if t]
    assert present, "немає жодної мітки часу для перевірки"
    for t in present:
        assert t.endswith("+00:00") or t.endswith("Z"), f"мітка без зони: {t}"


def test_tests_do_not_write_into_the_project(tmp_path, monkeypatch):
    """Регресія: підмінений Popen усе одно створював logs/manual-*.log у проєкті."""
    monkeypatch.setattr(status_mod.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    TestClient(app).post("/api/status/run", json={"source": "domria"})
    assert (tmp_path / "logs" / "manual-domria.log").exists()
    assert not (Path(__file__).resolve().parent.parent / "logs" / "manual-domria.log").exists()
