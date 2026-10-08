"""Контрольна вибірка Блоку 1 (`cli.py liveness sample`; D55 п. 7, хвиля W3, D58).

Головне: вибірка НІКОЛИ не змінює стану оголошень (ні listings, ні check_events, ні
listing_events) — лише ops.liveness_sample_*; той самий підпис, що й перевірка
актуальності; контрольне «знято» зупиняє джерело; понад fuse.share — критична тривога
без жодних автоматичних дій; «знято» в межах — підказка звичайній перевірці (відкладене
завдання «opened»). Мережа — підставна (liveness_kit.FakeNet).
"""
from __future__ import annotations

import configparser
import hashlib
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import FakeNet, add, db, olx_url, ria_page, ria_url  # noqa: E402,F401

from realty import configfiles, ops, runner, watchdog  # noqa: E402
from realty.liveness import sample  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
T = datetime(2026, 10, 14, 7, 20)                 # 10:20 за Києвом, середа
RIA = 34201000                                    # id DOM.RIA — 8 цифр, як у links.toml


@pytest.fixture
def env(db, tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.init_ops(force=True)
    monkeypatch.setattr(sample, "LOCK_PATH", tmp_path / "cycle.lock")
    monkeypatch.setattr(sample, "DISABLED_FLAG", tmp_path / "COLLECTOR_OFF")
    import realty.config as config
    monkeypatch.setattr(config, "enabled_sources", lambda: ["domria", "olx", "blago"])

    @contextmanager
    def scope():
        s = db()
        try:
            yield s
            s.commit()
        finally:
            s.close()
    return {"Session": db, "scope": scope, "tmp": tmp_path}


def _fill(Session, *, n=30, fresh=True, olx_removed=(), ria_removed=()):
    """n оголошень DOM.RIA й OLX (актуальні, якість ok) + неактуальне, «на перевірці» й Благо."""
    seen = T - timedelta(hours=2) if fresh else T - timedelta(days=5)
    for i in range(n):
        add(Session, ria_url(RIA + i), source="domria", last_seen=seen, property_id=500 + i)
        add(Session, olx_url(f"AbC{i:03d}"), source="olx", last_seen=seen,
            property_id=900 + i if i % 2 else None)
    add(Session, ria_url(RIA - 1), source="domria", is_active=False, last_seen=seen)
    add(Session, ria_url(RIA - 2), source="domria", quality_status="review", last_seen=seen)
    add(Session, "https://blagodeveloper.com/planning/1", source="blago", last_seen=seen)
    net = FakeNet({}, default=200)
    for i in range(n):
        archived = i in ria_removed
        net.responses[f"domria:{RIA + i}"] = (200, ria_page(RIA + i, archived=archived))
        if i in olx_removed:
            net.responses[f"olx:AbC{i:03d}"] = 410
    return net


def _fingerprint(Session) -> str:
    """Відбиток УСІХ колонок listings і кількостей check_events / listing_events."""
    h = hashlib.sha256()
    with Session() as s:
        for row in s.execute(text("SELECT * FROM listings ORDER BY id")):
            h.update(repr(tuple(row)).encode())
        for table in ("check_events", "listing_events"):
            h.update(f"{table}:{s.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar()}"
                     .encode())
    return h.hexdigest()


def _run(env, net, **kw):
    out: list[str] = []
    code = sample.run(fetcher=net, scope=env["scope"], now_fn=lambda: T, out=out.append, **kw)
    return code, "\n".join(out)


def _jobs():
    with ops.ops_session() as s:
        return [(j.kind, j.key, j.state, j.property_id)
                for j in s.scalars(select(ops.LookupCheck).order_by(ops.LookupCheck.id))]


def test_sample_never_changes_listing_state_and_flags_a_broken_signature(env):
    net = _fill(env["Session"], fresh=False, ria_removed={3}, olx_removed=set(range(9)))
    before = _fingerprint(env["Session"])
    code, out = _run(env, net, per_source=100)
    assert code == 0
    assert _fingerprint(env["Session"]) == before          # стану оголошень не змінено
    d = sample.last_run()
    v = d["verdicts"]
    # DOM.RIA: 1 із 30 (3,3% ≤ 5%) — гаразд; «знято» — підказка звичайній перевірці.
    assert v["domria"]["status"] == "pass" and v["domria"]["removed"] == 1
    assert v["domria"]["checked"] == 30 and v["domria"]["examples"][0]["hinted"] is True
    # OLX: 9 із 30 (30% > fuse.share 20% при n ≥ 20) — рішення власника, жодних підказок.
    assert v["olx"]["status"] == "fuse_share" and v["olx"]["removed"] == 9
    assert v["blago"]["status"] == "skipped"
    assert _jobs() == [("opened", "property:503", "deferred", 503)]
    # Неактуальне й «на перевірці» у вибірку не потрапили; GET лише для DOM.RIA.
    asked = {u for u, _, _ in net.calls}
    assert ria_url(RIA - 1) not in asked and ria_url(RIA - 2) not in asked
    assert all(m == "GET" for u, m, _ in net.calls if "dom.ria.com" in u)
    with ops.ops_session() as s:
        rows = s.scalars(select(ops.LivenessSampleCheck)).all()
    assert len(rows) == 60 and {r.kind for r in rows} == {"random"}
    removed = [r for r in rows if r.outcome == "removed"]
    assert {r.signature for r in removed} == {"ria_archive", "status_410"}
    assert all(r.last_seen is not None and r.url for r in removed)
    assert "КОНТРОЛЬНА ВИБІРКА" in out and "бачили в стрічці" in out
    keys = {a.key for a in watchdog.check_sample(ops._now())}
    assert keys == {"liveness-sample-share:olx"}


def test_canary_removed_stops_the_source_and_raises_a_critical_alert(env):
    net = _fill(env["Session"], fresh=True, ria_removed=set(range(30)))
    before = _fingerprint(env["Session"])
    _run(env, net, per_source=100)
    assert _fingerprint(env["Session"]) == before
    d = sample.last_run()
    v = d["verdicts"]
    assert v["domria"]["status"] == "canary" and v["domria"]["canary_removed"] >= 1
    canaries = configfiles.load("sample").run.canaries_per_host
    ria_calls = [u for u, _, _ in net.calls if "dom.ria.com" in u]
    assert len(ria_calls) == canaries                   # решту DOM.RIA не питали
    assert v["domria"]["stopped"] == 30 and v["domria"]["checked"] == 0
    assert v["olx"]["status"] == "pass"
    assert not [j for j in _jobs() if j[1].startswith("property:5")]   # підказок DOM.RIA немає
    alerts = {a.key: a for a in watchdog.check_sample(ops._now())}
    assert "liveness-sample-canary:domria" in alerts
    cfg = configfiles.load("alerts")
    assert watchdog.level_of("liveness-sample-canary:domria", cfg.levels) == "critical"


def test_canaries_come_from_the_existing_picker(env, monkeypatch):
    """Контрольні — `queue.canary_keys` за ім'ям (гілка запобіжника змінює саме його)."""
    from realty.liveness import queue

    asked = []
    real = queue.canary_keys

    def spy(u, cfg):
        got = real(u, cfg)
        asked.append(len(got))
        return got
    monkeypatch.setattr(queue, "canary_keys", spy)
    net = _fill(env["Session"], fresh=True)
    _run(env, net, per_source=5)
    assert asked and asked[0] > 0
    with ops.ops_session() as s:
        kinds = s.execute(select(ops.LivenessSampleCheck.kind, func.count())
                          .group_by(ops.LivenessSampleCheck.kind)).all()
    assert dict(kinds)["canary"] > 0 and dict(kinds)["random"] == 10


def test_hints_are_not_duplicated(env):
    net = _fill(env["Session"], fresh=False, ria_removed={3})
    _run(env, net, per_source=100)
    _run(env, net, per_source=100)
    assert _jobs() == [("opened", "property:503", "deferred", 503)]


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    monkeypatch.setenv(configfiles.ENV_DIR, str(target))
    return target


def test_busy_cycle_lock_gives_up_with_a_warning_and_no_requests(env, cfg_dir):
    p = cfg_dir / "sample.toml"
    p.write_text(p.read_text().replace("lock_wait_minutes = 30", "lock_wait_minutes = 0"))
    net = _fill(env["Session"])
    lock = runner.CycleLock(sample.LOCK_PATH)
    assert lock.acquire()
    try:
        code, out = _run(env, net)
    finally:
        lock.release()
    assert code == 0 and net.calls == [] and "замок" in out
    d = sample.last_run()
    assert d["status"] == "lock_timeout"
    alerts = watchdog.check_sample(ops._now())
    assert [a.key for a in alerts] == ["liveness-sample-skipped"]
    assert watchdog.level_of(alerts[0].key, configfiles.load("alerts").levels) == "warning"


def test_sample_holds_the_cycle_lock_while_checking(env):
    seen = []

    class Spy(FakeNet):
        def check(self, *a, **k):
            seen.append(runner.lock_busy(sample.LOCK_PATH) is not None
                        or not runner.CycleLock(sample.LOCK_PATH).acquire())
            return super().check(*a, **k)
    net = _fill(env["Session"])
    spy = Spy(net.responses)
    _run(env, spy, per_source=5)
    assert seen and all(seen)
    lock = runner.CycleLock(sample.LOCK_PATH)
    assert lock.acquire()                               # після прогону замок вільний
    lock.release()


def test_dry_run_plans_without_network_lock_or_writes(env):
    net = _fill(env["Session"])
    code, out = _run(env, net, dry_run=True)
    assert code == 0 and net.calls == [] and sample.last_run() is None
    assert "domria" in out and "вибрано" in out and "blago" in out and "пропуск" in out


def test_collector_off_means_no_requests(env):
    net = _fill(env["Session"])
    sample.DISABLED_FLAG.write_text("вимкнено")
    code, out = _run(env, net)
    assert code == 0 and net.calls == [] and sample.last_run()["status"] == "disabled"
    assert watchdog.check_sample(ops._now()) == []


def test_failure_is_recorded_and_raised(env, monkeypatch):
    net = _fill(env["Session"])

    def boom(*a, **k):
        raise RuntimeError("мережевий шар зламався")
    monkeypatch.setattr("realty.liveness.engine.run_items", boom)
    with pytest.raises(RuntimeError):
        _run(env, net)
    assert sample.last_run()["status"] == "failed"
    lock = runner.CycleLock(sample.LOCK_PATH)
    assert lock.acquire()                               # замок звільнено й після аварії
    lock.release()


def test_report_and_digest_lines(env):
    net = _fill(env["Session"], fresh=False, ria_removed={3})
    _run(env, net, per_source=100)
    d = sample.last_run()
    text = sample.render(d)
    assert "domria: ✅ гаразд — «знято» 1 із 30" in text
    assert "dom.ria.com/uk/" in text and "підказка звичайній перевірці" in text
    short = "\n".join(sample.render_short(d))
    assert short.startswith(f"🎯 Контрольна вибірка №{d['id']}")


def test_cli_sample_dry_run_and_report_in_a_separate_process(tmp_path):
    """Окремим процесом (як розгортання): `--dry-run` без мережі й запису, `--report`."""
    import os
    import subprocess

    env = {**os.environ, "OPS_DB_URL": f"sqlite:///{tmp_path / 'ops.db'}"}
    r = subprocess.run([sys.executable, "cli.py", "liveness", "sample", "--report"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "контрольної вибірки ще не було" in r.stdout, r.stderr[-800:]
    r = subprocess.run([sys.executable, "cli.py", "liveness", "sample", "--dry-run",
                        "--per-source", "3"], cwd=ROOT, capture_output=True, text=True,
                       timeout=300, env=env)
    assert r.returncode == 0 and "план (без мережі й запису)" in r.stdout, r.stderr[-800:]


def _unit(name: str) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(ROOT / "deploy" / "fedora" / "systemd" / name, encoding="utf-8")
    return p


def test_sample_units_schedule_priorities_and_install():
    svc, timer = _unit("realty-liveness-sample.service"), _unit("realty-liveness-sample.timer")
    assert svc["Service"]["ExecStart"].endswith("cli.py liveness sample")
    assert svc["Unit"]["OnFailure"] == "realty-alert@%n.service"
    assert timer["Timer"]["OnCalendar"] == "Wed *-*-* 10:20:00"
    assert timer["Timer"]["Persistent"] == "false"
    # Пріоритети — ті самі, що й у realty-lookup@ (їх install.sh пробує без root).
    lookup = _unit("realty-lookup@.service")["Service"]
    for k in ("Nice", "CPUWeight", "IOSchedulingClass", "IOSchedulingPriority",
              "OOMScoreAdjust", "MemoryHigh", "MemoryMax"):
        assert svc["Service"][k] == lookup[k], k
    install = (ROOT / "deploy" / "fedora" / "install.sh").read_text(encoding="utf-8")
    assert install.count("realty-liveness-sample.timer") >= 2
    # Стеля systemd: очікування замка + мережева стеля + запас < 1 год 45 хв (10:20 → 12:05).
    s = configfiles.load("sample").run
    assert s.lock_wait_minutes + s.max_minutes < 100
    assert svc["Service"]["TimeoutStartSec"] == "1h40min"
