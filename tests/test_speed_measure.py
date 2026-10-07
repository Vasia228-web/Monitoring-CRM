"""Вимірювання швидкості сайту (Блок 2, крок E2, D49).

Server-Timing і журнал часу, маячок браузера, зведення для власника, фоновий
пакетний запис, вікна транзакцій запису кроків циклу, зонд. Перевірено також
те, чого вимір робити НЕ має: писати IP чи рядок браузера, рахувати запити
незнайомців і скриптів, показувати час сервера незнайомцям, ходити кудись,
крім 127.0.0.1, класти пароль в адресу зонда, рахувати перегляди карток,
нараховувати невдалі входи з 127.0.0.1 (через тунель звідти приходять усі),
писати без меж (маячок і зведення мають стелі), перевіряти схему ops.db на
кожен запит.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles, ops, runner  # noqa: E402
from realty.web import sessions  # noqa: E402
from realty.web.app import app  # noqa: E402

# Нові модулі Блоку 2 імпортуються всередині тестів, а не тут: на старому коді
# тести мають падати на поведінці (немає заголовка, немає змінної в кроці), а
# не всім файлом на ImportError.


class _Lazy:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr):
        import importlib
        return getattr(importlib.import_module(self._name), attr)


perf = _Lazy("realty.web.perf")
speedprobe = _Lazy("realty.speedprobe")
txnwatch = _Lazy("realty.txnwatch")


def DeferredWriter(*a, **k):  # noqa: N802 — як клас
    from realty.web.deferred import DeferredWriter as W
    return W(*a, **k)


def _drain() -> None:
    try:
        perf.WRITER.take()
    except ImportError:
        pass

OWNER_U, OWNER_PW = "vasia", "пароль власника 1"
FRIEND_U, FRIEND_PW = "druh", "druh-pass-2"
SITE = "https://mojkvartiry.test"
TIMING = re.compile(r"^app;dur=\d+(\.\d+)?(, snapshot;dur=\d+(\.\d+)?)?$")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Окрема ops.db, два облікові записи, порожній буфер журналу."""
    from realty import backup, dedup_audit, dedup_sample  # noqa: F401 — таблиці ops

    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(engine)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER_U)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    monkeypatch.setenv("FRIEND_USER", FRIEND_U)
    monkeypatch.setenv("FRIEND_PASSWORD", FRIEND_PW)
    try:                                   # ліміт маячків — свій на кожен тест
        from realty.web import perf as _perf
        if hasattr(_perf, "RateCap"):
            monkeypatch.setattr(_perf, "RUM_CAP", _perf.RateCap())
    except ImportError:
        pass
    _drain()
    yield engine
    _drain()


def speed_config(tmp_path, monkeypatch, changes: dict[str, str]):
    """Копія config/ з правками speed.toml: {"секція.ключ": "нове значення TOML"}.

    Через REALTY_CONFIG_DIR — так само, як сайт читає справжній конфіг: нова
    тека — нове перше читання, без підміни функцій.
    """
    import shutil
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    path = target / "speed.toml"
    text_ = path.read_text()
    for dotted, value in changes.items():
        section, key = dotted.split(".")
        start = text_.index(f"\n[{section}]\n")
        m = re.compile(rf"^{re.escape(key)} = .*$", re.M).search(text_, start)
        assert m is not None, f"у [{section}] немає ключа {key}"
        text_ = text_[:m.start()] + f"{key} = {value}" + text_[m.end():]
    path.write_text(text_)
    monkeypatch.setenv(configfiles.ENV_DIR, str(target))
    return configfiles.load("speed")


def client() -> TestClient:
    return TestClient(app, base_url=SITE, client=("127.0.0.1", 50000), follow_redirects=False)


def logged_in(user: str, pw: str, ip: str) -> TestClient:
    c = client()
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"},
               headers={"CF-Connecting-IP": ip, "Accept": "text/html"})
    assert r.status_code == 303, r.text[:200]
    return c


def _rows(table: str) -> list[dict]:
    return [row for t, row in perf.WRITER.take() if t == table]


# --- Server-Timing і журнал часу ----------------------------------------------------------


def test_server_timing_is_for_logged_in_pages_and_healthz_only(env):
    anon = client()
    assert "server-timing" in anon.get("/healthz").headers, "заголовка Server-Timing немає"
    assert TIMING.match(anon.get("/healthz").headers["server-timing"])
    # Незнайомцю з інтернету час сервера не показуємо: ні на 401, ні на
    # сторінці входу, ні на невдалому вході, ні на редиректі до входу.
    for r in (anon.get("/api/stats"), anon.get("/login"),
              anon.get("/analytics", headers={"Accept": "text/html"}),
              anon.post("/login", data={"username": OWNER_U, "password": "не той",
                                        "next": "/"},
                        headers={"CF-Connecting-IP": "203.0.113.60"})):
        assert r.status_code in (200, 303, 401), r.status_code
        assert "server-timing" not in r.headers, (r.request.url, r.headers["server-timing"])
    for user, pw, ip in ((OWNER_U, OWNER_PW, "203.0.113.61"), (FRIEND_U, FRIEND_PW, "203.0.113.59")):
        c = logged_in(user, pw, ip)
        for url in ("/api/stats", "/analytics"):
            r = c.get(url)
            assert r.status_code == 200
            assert TIMING.match(r.headers["server-timing"]), r.headers.get("server-timing")
            assert r.headers["x-robots-tag"] == "noindex, nofollow"      # не загубився


def test_dev_mode_without_login_keeps_server_timing(env, monkeypatch):
    for var in ("AUTH_USER", "AUTH_PASSWORD", "FRIEND_USER", "FRIEND_PASSWORD"):
        monkeypatch.delenv(var)
    assert TIMING.match(client().get("/api/stats").headers["server-timing"])


def test_requests_are_logged_by_route_template_and_role_without_ip(env):
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.62")
    friend = logged_in(FRIEND_U, FRIEND_PW, "203.0.113.63")
    _drain()                                               # вхід — не те, що міряємо
    assert owner.get("/api/properties/999999999/prices").status_code == 404
    assert friend.get("/api/stats").status_code == 200
    rows = _rows("web_timings")
    assert [(r["route"], r["role"], r["status"]) for r in rows] == [
        ("/api/properties/{property_id}/prices", "owner", 404),
        ("/api/stats", "friend", 200)]
    for r in rows:
        assert r["ms"] > 0 and r["method"] == "GET" and len(r["config_hash"]) == 12
        assert r["cycle_active"] in (True, False)
    # У таблиці немає й місця для IP чи рядка браузера.
    columns = {c["name"] for c in inspect(env).get_columns("web_timings")}
    assert not columns & {"ip", "user_agent", "path", "query"}


def test_strangers_and_open_paths_are_not_logged(env):
    anon = client()
    assert anon.get("/api/stats").status_code == 401
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/nope/").status_code in (401, 404)
    assert _rows("web_timings") == []


def test_status_polls_are_sampled(env, monkeypatch):
    """Сторінка стану опитує /api/status кожні 5 с — у журнал іде кожне 12-те."""
    from realty.web import status as status_mod
    monkeypatch.setattr(status_mod, "build_status", lambda: {"ok": True})
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.64")
    perf.WRITER.take()
    n = configfiles.get("speed").timings.sample_polls
    for _ in range(2 * n):
        assert owner.get("/api/status").status_code == 200
    assert len([r for r in _rows("web_timings") if r["route"] == "/api/status"]) == 2


def test_cycle_state_marks_the_log(env, monkeypatch):
    monkeypatch.setattr(ops, "current_cycle", lambda max_age_s: (True, "дублі"))
    perf.CYCLE.refresh()
    try:
        owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.65")
        perf.WRITER.take()
        owner.get("/api/stats")
        (row,) = _rows("web_timings")
        assert row["cycle_active"] is True and row["cycle_step"] == "дублі"
    finally:
        perf.CYCLE.active, perf.CYCLE.step = False, None


def test_current_cycle_reads_the_running_cycle_and_the_step(env):
    assert ops.current_cycle(3600) == (False, None)
    cid = ops.start_cycle("schedule")
    ops.beat(f"{ops.STEP_NOTE}дублі", busy=True)
    assert ops.current_cycle(3600) == (True, "дублі")
    ops.finish_cycle(cid, status="ok")
    assert ops.current_cycle(3600) == (False, None)


def test_script_requests_are_not_logged_and_do_not_write_ops_db(env):
    """Зонд входить заголовком Basic на КОЖЕН запит. Його запити не мають
    змішуватися з переглядами людей у журналі часу, а сам вхід — відкривати
    транзакцію запису в ops.db (досі кожен робив DELETE FROM auth_blocks,
    який під час циклу чекав на записи кроків). Поведінка входу та сама."""
    c = client()
    c.auth = (OWNER_U, OWNER_PW)
    assert c.get("/api/stats").status_code == 200          # прогрів: схема ops.db
    _drain()
    statements: list[str] = []

    def on_exec(conn, cursor, statement, params, context, executemany):
        statements.append(statement.split()[0].upper())
    event.listen(env, "before_cursor_execute", on_exec)
    try:
        for _ in range(5):
            r = c.get("/api/stats")
            assert r.status_code == 200 and TIMING.match(r.headers["server-timing"])
    finally:
        event.remove(env, "before_cursor_execute", on_exec)
    assert "SELECT" in statements                        # вхід справді перевірявся
    assert [st for st in statements if st in ("DELETE", "INSERT", "UPDATE")] == []
    assert _rows("web_timings") == [], "запити скрипта потрапили в журнал часу"
    # Успішний вхід, як і досі, знімає невдалі спроби з адреси.
    sessions.register_failure("127.0.0.1", None, "test", OWNER_U)
    assert c.get("/api/stats").status_code == 200
    with ops.ops_session() as s:
        assert s.get(sessions.AuthBlock, "127.0.0.1") is None
    # А браузер (кука) — пишеться.
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.79")
    _drain()
    assert owner.get("/api/stats").status_code == 200
    assert [r["role"] for r in _rows("web_timings")] == ["owner"]


def test_friend_request_timings_can_be_switched_off_in_config(env, tmp_path, monkeypatch):
    """Чи писати друга — рішення власника (D49): одна правка timings.roles."""
    speed_config(tmp_path, monkeypatch, {"timings.roles": '["owner"]'})
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.73")
    friend = logged_in(FRIEND_U, FRIEND_PW, "203.0.113.74")
    _drain()
    assert friend.get("/api/stats").status_code == 200
    assert friend.get("/analytics").status_code == 200
    assert owner.get("/api/stats").status_code == 200
    assert [r["role"] for r in _rows("web_timings")] == ["owner"]


def test_ops_schema_is_checked_once_per_process(env):
    """init_ops() — один раз на процес: не 3 PRAGMA на кожну з 11 таблиць ops.db
    на кожен запит того, хто ввійшов, і 11 разів на /api/status."""
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.77")
    assert owner.get("/api/stats").status_code == 200       # тут схема перевірена
    checks: list[str] = []

    def on_exec(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("PRAGMA") and "table_" in statement:
            checks.append(statement)
    event.listen(env, "before_cursor_execute", on_exec)
    try:
        for url in ("/api/stats", "/api/status", "/api/auth/blocks"):
            assert owner.get(url).status_code == 200, url
    finally:
        event.remove(env, "before_cursor_execute", on_exec)
    assert checks == [], f"схема ops.db перевіряється на запит: {len(checks)} PRAGMA"
    # Інший рушій (як у тестах) — своя перевірка, таблиці створюються.
    other = create_engine(f"sqlite:///{env.url.database}.other", future=True)
    real = ops.engine
    ops.engine = other
    try:
        ops.init_ops()
        assert "web_timings" in inspect(other).get_table_names()
    finally:
        ops.engine = real


def test_cold_snapshot_is_reported_apart_from_warm(env):
    """Холодний запит (знімок «Аналітики» будується в ньому) видно в
    Server-Timing, у журналі часу й у зонді — окремо від теплих (D49)."""
    from realty.analytics import cache
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.78")
    cache.invalidate()
    _drain()
    try:
        cold = owner.get("/analytics")
        warm = owner.get("/analytics")
    finally:
        cache.invalidate()
    assert cold.status_code == warm.status_code == 200
    assert TIMING.match(cold.headers["server-timing"]) and TIMING.match(warm.headers["server-timing"])
    assert speedprobe.snapshot_ms(cold.headers["server-timing"]) is not None
    assert speedprobe.snapshot_ms(warm.headers["server-timing"]) is None
    assert speedprobe.server_ms(cold.headers["server-timing"]) >= \
        speedprobe.snapshot_ms(cold.headers["server-timing"])
    rows = _rows("web_timings")
    assert [(r["route"], r["snapshot_ms"] is not None) for r in rows] == [
        ("/analytics", True), ("/analytics", False)]
    samples = [{"url": "/analytics", "status": 200, "client_ms": ms + 1, "server_ms": ms,
                "snapshot_ms": snap, "bytes": 1, "cycle_active": False}
               for ms, snap in ((500.0, 480.0), (70.0, None), (72.0, None))]
    (row,) = speedprobe.summarize(samples, ["/analytics"], 300)
    assert (row["cold"], row["cold_p50"], row["warm_p50"], row["warm_p95"]) == (1, 500.0, 70.0, 72.0)


# --- Маячок браузера ----------------------------------------------------------------------


BEACON = {"route": "/property/123", "nav": "navigate", "reused": True, "ttfb": 120.4,
          "dcl": 610, "load": 905.6, "transfer": 12000, "server": 35.55, "proto": "h2",
          "device": "mobile",
          "res": [["/api/properties/5/processing", 300.2], ["/api/rum", 5],
                  ["/nope", 4], ["/api/listings/7/status", "x"]]}


def test_rum_beacon_is_stored_for_both_roles_without_ip(env):
    for user, pw, ip, role in ((OWNER_U, OWNER_PW, "203.0.113.66", "owner"),
                               (FRIEND_U, FRIEND_PW, "203.0.113.67", "friend")):
        c = logged_in(user, pw, ip)
        # Як шле браузер: sendBeacon із рядком — це text/plain (Blob типу
        # application/json Chrome не пропускає, див. _rum.html).
        r = c.post("/api/rum", content=json.dumps(BEACON),
                   headers={"Origin": SITE, "Content-Type": "text/plain;charset=UTF-8"})
        assert r.status_code == 204, r.text
        (row,) = _rows("web_rum")
        assert row["route"] == "/property/{property_id}" and row["role"] == role
        assert (row["nav_type"], row["reused"], row["load_ms"], row["ttfb_ms"], row["device"]) \
            == ("navigate", True, 906, 120, "mobile")
        # Лише шаблони /api/ і лише правдоподібні числа; сам маячок — не «кнопка».
        assert json.loads(row["resources"]) == [["/api/properties/{property_id}/processing", 300]]
    columns = {c["name"] for c in inspect(env).get_columns("web_rum")}
    assert not columns & {"ip", "user_agent", "path"}


def test_rum_beacon_is_checked_like_any_other_post(env):
    assert client().post("/api/rum", content=json.dumps(BEACON),
                         headers={"Origin": SITE}).status_code == 401
    c = logged_in(OWNER_U, OWNER_PW, "203.0.113.68")
    # Без Origin (чужа сторінка не підробить його) — same-origin, як для кнопок.
    assert c.post("/api/rum", content=json.dumps(BEACON)).status_code == 403
    assert c.post("/api/rum", content=json.dumps(BEACON),
                  headers={"Origin": "https://evil.example"}).status_code == 403
    for bad in (b"not json", b"[1, 2]", json.dumps({**BEACON, "route": "/nope"}).encode(),
                json.dumps({**BEACON, "load": -1}).encode()):
        assert c.post("/api/rum", content=bad, headers={"Origin": SITE}).status_code == 400
    limit = configfiles.get("speed").rum.max_body_bytes
    big = json.dumps({**BEACON, "pad": "x" * limit}).encode()
    assert c.post("/api/rum", content=big, headers={"Origin": SITE}).status_code == 413
    assert _rows("web_rum") == []


def test_rum_beacons_are_capped_per_role(env, monkeypatch):
    """Вкрадена кука друга чи сторінка в циклі не пише тисячі рядків на 30 днів:
    не більше rum.max_per_min маячків на хвилину від ролі (D49)."""
    from realty.web import perf as perf_mod
    monkeypatch.setattr(perf_mod, "RUM_CAP", perf_mod.RateCap(clock=lambda: 0.0))  # одна хвилина
    cap = configfiles.get("speed").rum.max_per_min
    friend = logged_in(FRIEND_U, FRIEND_PW, "203.0.113.75")
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.76")
    body = json.dumps(BEACON)
    headers = {"Origin": SITE, "Content-Type": "text/plain;charset=UTF-8"}
    codes = {friend.post("/api/rum", content=body, headers=headers).status_code
             for _ in range(1000)}
    assert codes == {204}                      # браузер нічого не помічає
    rows = _rows("web_rum")
    assert len(rows) == cap and {r["role"] for r in rows} == {"friend"}
    assert perf_mod.RUM_CAP.dropped == 1000 - cap
    # Квота власника — своя: потоп від друга її не забирає.
    assert owner.post("/api/rum", content=body, headers=headers).status_code == 204
    assert [r["role"] for r in _rows("web_rum")] == ["owner"]
    # Вікно ковзне: за хвилину приймається знову.
    now = [0.0]
    capper = perf.RateCap(clock=lambda: now[0])
    assert [capper.allow("friend", 3) for _ in range(4)] == [True, True, True, False]
    now[0] += 59.9
    assert not capper.allow("friend", 3)
    now[0] += 0.2
    assert capper.allow("friend", 3) and capper.dropped == 2


def test_rum_block_is_on_pages_only_for_logged_in_roles(env, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("pe", ROOT / "scripts" / "page_equality.py")
    pe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pe)
    for user, pw, ip in ((OWNER_U, OWNER_PW, "203.0.113.69"), (FRIEND_U, FRIEND_PW, "203.0.113.70")):
        html = logged_in(user, pw, ip).get("/analytics", headers={"Accept": "text/html"}).text
        assert html.count("<!--rum-->") == 1 and 'sendBeacon("/api/rum",JSON.stringify(' in html
        assert "new Blob" not in html                 # див. _rum.html: Chrome його відкидає
        # Порівняння сторінок «до/після» цей блок вирізає повністю.
        normalized = pe.normalize(html.encode(), "text/html").decode()
        assert "<!--rum-->" not in normalized and "/api/rum" not in normalized
    for var in ("AUTH_USER", "AUTH_PASSWORD", "FRIEND_USER", "FRIEND_PASSWORD"):
        monkeypatch.delenv(var)
    html = client().get("/analytics").text           # без входу (розробка) — ані байта
    assert "<!--rum-->" not in html and "/api/rum" not in html


# --- Зведення для власника ----------------------------------------------------------------


def test_speed_summary_is_owner_only_and_counts_percentiles(env):
    now = ops._now()
    with ops.ops_session() as s:
        for ms in range(1, 101):                  # 1..100 мс: p50 = 50, p95 = 95
            s.add(ops.WebTiming(at=now - timedelta(minutes=5), route="/", method="GET",
                                status=200, ms=float(ms), cycle_active=ms > 80, role="owner"))
        s.add(ops.WebTiming(at=now - timedelta(days=3), route="/old", method="GET",
                            status=200, ms=1.0, role="owner"))
        s.add(ops.WebRum(at=now, route="/", nav_type="navigate", reused=True, load_ms=900,
                         device="mobile", role="friend",
                         resources=json.dumps([["/api/stats", 200]])))
        s.add(ops.WriteWindow(process="dedup", step="дублі", max_ms=900.0, total_ms=1500.0,
                              txns=3))
        s.add(ops.WebTiming(at=now, route="/analytics", method="GET", status=200, ms=500.0,
                            snapshot_ms=480.0, role="owner"))
        s.add(ops.WebTiming(at=now, route="/analytics", method="GET", status=200, ms=70.0,
                            role="owner"))
    friend = logged_in(FRIEND_U, FRIEND_PW, "203.0.113.71")
    assert friend.get("/api/status/speed").status_code == 403
    owner = logged_in(OWNER_U, OWNER_PW, "203.0.113.72")
    data = owner.get("/api/status/speed").json()
    assert data["targets"] == {"server_p95_ms": 300, "tab_switch_ms": 1500, "button_ms": 1500}
    (row,) = [r for r in data["server"]["routes"] if r["route"] == "/"]
    assert (row["n"], row["p50"], row["p95"], row["max"], row["ok"]) == (100, 50.0, 95.0, 100.0, True)
    assert row["cycle"]["n"] == 20 and row["idle"]["n"] == 80
    assert not [r for r in data["server"]["routes"] if r["route"] == "/old"]   # за межею доби
    assert data["rum"]["phone_reused"] == {"n": 1, "p50": 900, "p95": 900, "max": 900}
    assert data["rum"]["buttons"]["n"] == 1 and data["rum"]["recent"][0]["ok"] is True
    assert data["write_windows"][0]["step"] == "дублі"
    (analytics,) = [r for r in data["server"]["routes"] if r["route"] == "/analytics"]
    assert (analytics["warm"]["p50"], analytics["cold"]["n"], analytics["cold"]["p50"]) == \
        (70.0, 1, 500.0)
    assert data["server"]["truncated"] is False and data["rum"]["truncated"] is False


def test_speed_summary_reads_at_most_max_rows(env, tmp_path, monkeypatch):
    """Зведення читає не більше summary.max_rows найновіших рядків кожного
    журналу — і колонки, а не об'єкти (300 тис. рядків маячка коштували +0,5 ГБ)."""
    cfg = speed_config(tmp_path, monkeypatch, {"summary.max_rows": "50"})
    now = ops._now()
    with ops.ops_session() as s:
        for i in range(51):                       # i = 0 — найновіший
            at = now - timedelta(seconds=i)
            s.add(ops.WebTiming(at=at, route="/", method="GET", status=200, ms=float(i + 1),
                                role="owner"))
            s.add(ops.WebRum(at=at, route="/", load_ms=100 + i, reused=True, device="mobile",
                             role="owner"))
    statements: list[tuple[str, tuple]] = []

    def on_exec(conn, cursor, statement, params, context, executemany):
        statements.append((" ".join(statement.split()), tuple(params or ())))
    event.listen(env, "before_cursor_execute", on_exec)
    try:
        data = perf.summary()
    finally:
        event.remove(env, "before_cursor_execute", on_exec)
    (route,) = data["server"]["routes"]
    assert (route["n"], route["max"]) == (50, 50.0)          # найновіші 50, найстаріший — ні
    assert data["server"]["truncated"] is True and data["rum"]["truncated"] is True
    assert (data["rum"]["all"]["n"], data["rum"]["all"]["max"]) == (50, 149)
    assert len(data["rum"]["recent"]) == cfg.summary.recent
    reads = [(q, p) for q, p in statements if q.startswith("SELECT")
             and ("FROM web_timings" in q or "FROM web_rum" in q)]
    assert len(reads) == 3
    for q, params in reads:
        assert "LIMIT" in q and (50 in params or cfg.summary.recent in params), (q, params)


# --- Фоновий запис ------------------------------------------------------------------------


def _settings(**over):
    base = configfiles.load("speed")
    deferred = dataclasses.replace(base.deferred, **{k: v for k, v in over.items()
                                                      if hasattr(base.deferred, k)})
    timings = dataclasses.replace(base.timings, **{k: v for k, v in over.items()
                                                    if hasattr(base.timings, k)})
    return dataclasses.replace(base, deferred=deferred, timings=timings)


def _timing(**over):
    return {"at": ops._now(), "route": "/", "method": "GET", "status": 200, "ms": 5.0,
            "bytes": 10, "cycle_active": False, "cycle_step": None, "role": "owner",
            "config_hash": "abc", **over}


def _count(engine, table="web_timings") -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def test_writer_keeps_rows_while_ops_db_is_locked(env, tmp_path):
    cfg = _settings(busy_timeout_ms=50)
    w = DeferredWriter(settings=lambda: cfg)
    for i in range(3):
        w.add("web_timings", _timing(ms=float(i)))
    blocker = sqlite3.connect(tmp_path / "ops.db", timeout=0)
    blocker.execute("BEGIN IMMEDIATE")              # чужий запис тримає базу
    try:
        assert w.flush() == 0
        assert w.pending() == 3 and w.failed_flushes == 1
    finally:
        blocker.rollback()
        blocker.close()
    assert w.flush() == 3 and w.pending() == 0
    assert _count(env) == 3
    # Короткий тайм-аут — лише на свою транзакцію: з'єднання в пулі знову чекає 30 с.
    with env.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == ops.BUSY_TIMEOUT_MS


def test_writer_buffer_has_a_ceiling(env):
    w = DeferredWriter(settings=lambda: _settings(max_buffer_rows=5))
    for i in range(8):
        w.add("web_timings", _timing(ms=float(i)))
    assert w.pending() == 5 and w.dropped == 3
    assert [row["ms"] for _, row in w.take()] == [3.0, 4.0, 5.0, 6.0, 7.0]   # найновіші


def test_writer_flushes_on_schedule_and_on_shutdown(env):
    now = [0.0]
    cfg = _settings()
    w = DeferredWriter(settings=lambda: cfg, clock=lambda: now[0])
    w.add("web_timings", _timing())
    w.run_once()                                   # ще не час (і прибирання — один раз)
    assert _count(env) == 0 and w.pending() == 1
    now[0] += cfg.timings.flush_s
    w.run_once()
    assert _count(env) == 1 and w.pending() == 0
    w.start()
    w.add("web_timings", _timing())
    w.stop()                                       # зупинка сайту — буфер дописано
    assert _count(env) == 2


def test_cleanup_drops_only_rows_older_than_retention(env):
    cfg = configfiles.load("speed")
    old = ops._now() - timedelta(days=cfg.timings.retention_days + 1)
    with ops.ops_session() as s:
        s.add(ops.WebTiming(**_timing(at=old)))
        s.add(ops.WebTiming(**_timing()))
        s.add(ops.WebRum(at=ops._now() - timedelta(days=cfg.rum.retention_days - 1),
                         route="/", load_ms=1))
    assert DeferredWriter().cleanup() == 1
    assert _count(env) == 1 and _count(env, "web_rum") == 1


# --- Вікна транзакцій запису --------------------------------------------------------------


def test_txn_watch_measures_the_write_window(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
    watch = txnwatch.Watch("dedup", "дублі").attach(engine)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT count(*) FROM t")).scalar()    # читання — не вікно
            conn.commit()
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t VALUES (1)"))
            time.sleep(0.2)                     # тримаємо блокування запису ≥200 мс
        Session = sessionmaker(bind=engine)
        with Session() as s:                    # закрили без commit — теж кінець вікна
            s.execute(text("UPDATE t SET x = 2"))
    finally:
        watch.detach(engine)
    row = watch.row()
    assert row["txns"] == 2 and watch.committed == 1
    assert row["max_ms"] >= 200 and row["max_sql"].startswith("INSERT INTO t")
    assert row["process"] == "dedup" and row["step"] == "дублі"


def test_cli_step_with_txn_watch_leaves_a_row(env, tmp_path):
    ops_db = tmp_path / "ops.db"
    out = subprocess.run(
        [sys.executable, "cli.py", "config", "check"], cwd=ROOT, capture_output=True, text=True,
        timeout=120, env={**os.environ, "TXN_WATCH": "1", "TXN_WATCH_STEP": "перевірка конфігів",
                          "OPS_DB_URL": f"sqlite:///{ops_db}",
                          "DB_URL": f"sqlite:///{tmp_path / 'main.db'}"})
    assert out.returncode == 0, out.stdout + out.stderr
    with ops.ops_session() as s:
        (row,) = s.scalars(select(ops.WriteWindow)).all()
    assert (row.process, row.step, row.txns) == ("config check", "перевірка конфігів", 0)


def test_cycle_passes_txn_watch_to_steps_and_beats_each_step(env, tmp_path, monkeypatch):
    notes = []
    real_beat = ops.beat
    monkeypatch.setattr(ops, "beat", lambda note=None, busy=None: (notes.append(note),
                                                                   real_beat(note, busy)))
    out = tmp_path / "env.txt"
    code = ("import os, pathlib; pathlib.Path(%r).write_text("
            "os.environ.get('TXN_WATCH', '') + '|' + os.environ.get('TXN_WATCH_STEP', ''))" % str(out))
    step = runner.Step("крок-тест", [sys.executable, "-c", code], 60)
    runner.run_cycle(steps=[step], lock_path=tmp_path / "cycle.lock",
                     disabled_flag=tmp_path / "OFF")
    assert out.read_text() == "1|крок-тест"
    assert f"{ops.STEP_NOTE}крок-тест" in notes


# --- Зонд ---------------------------------------------------------------------------------


@pytest.mark.parametrize("base,ok", [
    ("http://127.0.0.1:8000", True), ("http://localhost:8000/", True), ("http://[::1]:8000", True),
    ("http://127.0.0.2:8000", True),
    ("https://127.0.0.1:8000", False), ("http://mojkvartiry.link", False),
    ("http://127.0.0.1.evil.example:8000", False), ("http://10.0.0.5:8000", False),
    ("http://100.114.183.106:8000", False),
    ("http://u:p@127.0.0.1:8000", False), ("http://vasia:secret@localhost:8000", False),
    ("http://:secret@127.0.0.1:8000", False),
])
def test_probe_goes_only_to_this_machine(base, ok):
    if ok:
        assert speedprobe.check_base(base) == base.rstrip("/")
    else:
        with pytest.raises(speedprobe.ProbeRefused) as refused:
            speedprobe.check_base(base)
        # Пароль з адреси не має потрапити ні в повідомлення, ні далі в журнал.
        assert "secret" not in str(refused.value) and ":p@" not in str(refused.value)


def test_probe_measures_shared_and_owner_pages_and_skips_flat_pages(env, monkeypatch):
    from realty.db import SessionLocal
    from realty.models import Listing

    cfg = configfiles.load("speed").probe
    urls, skipped = speedprobe.plan_urls(cfg.urls)
    assert skipped and all(u.startswith("/property/") for u in skipped)
    assert not [u for u in urls if u.startswith("/property/")]
    with SessionLocal() as s:
        views_before = s.scalar(select(func.coalesce(func.sum(Listing.views), 0)))
    c = client()
    c.auth = (OWNER_U, OWNER_PW)                # шлях «Basic для скриптів», як у зонда
    res = speedprobe.run_probe(c, urls, repeats=1, pause_s=0, sleep=lambda s: None,
                               cycle=lambda: (False, None), psi=lambda: None)
    assert res["aborted"] is None
    assert [s["url"] for s in res["samples"]] == urls
    assert all(s["status"] == 200 and s["server_ms"] is not None for s in res["samples"]), \
        [(s["url"], s["status"]) for s in res["samples"]]
    with SessionLocal() as s:
        assert s.scalar(select(func.coalesce(func.sum(Listing.views), 0))) == views_before
    rows = speedprobe.summarize(res["samples"], urls, 300)
    assert {r["url"] for r in rows} == set(urls) and all(r["n"] == 1 for r in rows)


def test_probe_stops_at_the_first_refused_login(env):
    c = client()
    c.auth = (OWNER_U, "не той пароль")
    res = speedprobe.run_probe(c, ["/", "/analytics", "/status"], repeats=5, pause_s=0,
                               sleep=lambda s: None, cycle=lambda: (False, None),
                               psi=lambda: None)
    assert res["samples"] == [] and res["aborted"].startswith("401")
    with ops.ops_session() as s:
        block = s.get(sessions.AuthBlock, "127.0.0.1")
    assert block is not None and block.failures == 1        # одна, а не десятки


def test_probe_main_writes_its_result_outside_the_project(env, tmp_path, monkeypatch, capsys):
    import realty.speedprobe as speedprobe
    monkeypatch.setattr(speedprobe, "OUT_DIR", tmp_path / "speed")
    c = client()
    c.auth = (OWNER_U, OWNER_PW)
    monkeypatch.setattr(speedprobe, "run_probe", lambda client, urls, **k: {
        "samples": [{"url": u, "status": 200, "client_ms": 5.0, "server_ms": 4.0,
                     "bytes": 100, "cycle_active": False} for u in urls], "aborted": None})
    assert speedprobe.main(phase="idle", repeats=1, client=c) == 0
    (saved,) = (tmp_path / "speed").glob("probe-idle-*.json")
    data = json.loads(saved.read_text())
    assert data["skipped"] and data["summary"][0]["ok"] is True
    assert "Зонд швидкості" in capsys.readouterr().out


def test_wait_for_phase_follows_the_cycle_heartbeat():
    states = iter([(False, None), (True, "збір: olx"), (True, "дублі")])
    sleeps = []
    assert speedprobe.wait_for_phase("dedup", max_wait_s=100, poll_s=10,
                                     sleep=sleeps.append, clock=lambda: 0.0,
                                     reader=lambda: next(states))
    assert sleeps == [10, 10]
    now = [0.0]

    def tick(s):
        now[0] += s
    assert not speedprobe.wait_for_phase("cycle", max_wait_s=30, poll_s=10, sleep=tick,
                                         clock=lambda: now[0], reader=lambda: (False, None))
