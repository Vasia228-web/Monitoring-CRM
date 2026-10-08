"""Тривоги на два рівні й щоденне зведення (рішення власника 08.10, D55 п. 6; хвиля W3, D58).

Критичне — одразу й з першим рядком «🚨 КРИТИЧНО»; попередження — лише стан сторожа і
щоденне зведення «📋»; кожна тривога, яку сторож може видати, має ЯВНИЙ рівень у
config/alerts.toml; зведення — раз на місцеву добу, з повтором після невдачі; впала
служба (`alert unit-failed`) — одразу, без спаму й без секретів. Мережі немає: сайт,
журнал і Telegram — підставні.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import backup, configfiles, digest, ops, watchdog  # noqa: E402,F401

# 12:00 UTC = 15:00 за Києвом (EEST): зведення (08:40) і перевірка цілісності (07:40) вже
# «пора».
NOW = datetime(2026, 10, 9, 12, 0)


@pytest.fixture
def cfg():
    return configfiles.load("alerts")


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(engine)
    monkeypatch.setattr(watchdog, "enabled_sources", lambda: ["domria", "olx"])
    monkeypatch.setattr(watchdog, "check_verify_blocks", lambda now: [])
    monkeypatch.setattr(watchdog, "PUBLIC_URL_PATH", tmp_path / "public_url")
    monkeypatch.setattr(watchdog, "OUTBOX_PATH", tmp_path / "outbox.json")
    monkeypatch.setattr(watchdog, "UNITS_PATH", tmp_path / "units.json")
    monkeypatch.setattr(watchdog, "_header", lambda: "[test]")
    sent: list[str] = []
    state = tmp_path / "alerts.json"

    def run(now, **kw):
        return watchdog.run(now, kw.pop("send", sent.append), state, **kw)

    with ops.ops_session() as s:          # свіжий успішний цикл — тиші немає
        s.add(ops.CycleRecord(status="ok", started_at=NOW - timedelta(hours=1),
                              finished_at=NOW - timedelta(minutes=20), kept=100))
    return {"sent": sent, "run": run, "state": lambda: watchdog.load_state(state),
            "path": state, "tmp": tmp_path}


def _backup(status, at, offsite=None, message=None, restored=1):
    with ops.ops_session() as s:
        s.add(backup.BackupRecord(status=status, created_at=at, offsite=offsite,
                                  message=message, restored_ok=restored, size=6_000_000))


# --- Рівні ------------------------------------------------------------------------------------


def test_level_of_takes_the_longest_prefix_and_defaults_to_critical(cfg):
    lv = cfg.levels
    assert watchdog.level_of("drop:domria", lv) == "critical"
    assert watchdog.level_of("blocks:olx", lv) == "warning"
    assert watchdog.level_of("night-blocked:olx.ua", lv) == "warning"
    assert watchdog.level_of("night-hold:olx.ua", lv) == "critical"
    # «watchdog» — попередження, але впала перевірка критичного — критичне.
    assert watchdog.level_of("watchdog:check_dedup", lv) == "warning"
    assert watchdog.level_of("watchdog:check_silence", lv) == "critical"
    # Префікс — лише цілим словом: «drop» не робить «dropbox» відомим.
    assert watchdog.level_of("dropbox", {"drop": "warning"}) == "critical"
    # Невідома тривога — критичне (обережний бік).
    assert watchdog.level_of("щось-нове:x", lv) == "critical"
    assert watchdog.level_of("anything", {}) == "critical"


def _keys_in_code() -> set[str]:
    """Префікси ключів, які сторож видає: перший аргумент Alert(…) і рядкові літерали
    з дефісом (ключі в кортежах, як у check_dedup і check_site)."""
    src = (ROOT / "realty" / "watchdog.py").read_text(encoding="utf-8")
    found = set(re.findall(r'Alert\(\s*f?"([a-z][a-z0-9-]*)', src))
    found |= set(re.findall(r'(?<![\w.{/-])"([a-z]+(?:-[a-z0-9]+)+)(?=[":])', src))
    return found


def test_every_alert_key_in_code_is_listed_and_has_an_explicit_level(cfg):
    in_code = _keys_in_code()
    # Не ключі тривог: імена файлів і юнітів (без дефіса-ключа такого вигляду їх немає).
    unknown = sorted(in_code - set(watchdog.ALERT_KEYS) - {"unit-failed"})
    assert not unknown, f"ключі в коді, яких немає в watchdog.ALERT_KEYS: {unknown}"
    implicit = [k for k in watchdog.ALERT_KEYS if k not in cfg.levels]
    assert not implicit, f"без явного рядка в config/alerts.toml [levels]: {implicit}"


def test_every_failing_check_has_an_explicit_level(cfg, env, monkeypatch):
    """Кожна перевірка, що впала, дає watchdog:<перевірка> — і для кожної є явний рівень;
    перевірки критичного — критичні (інакше аварія сховалась би в зведенні)."""
    names = [n for n in dir(watchdog) if n.startswith("check_")]

    def broken(name):
        def check(*a, **k):
            raise RuntimeError("тестова поломка")
        check.__name__ = name
        return check
    for n in names:
        monkeypatch.setattr(watchdog, n, broken(n))
    keys = {a.key for a in watchdog.collect(NOW, {})}
    assert {f"watchdog:{n}" for n in names} == keys
    for key in keys:
        assert any(key == p or key.startswith(p + ":") for p in cfg.levels), key
    for n in ("check_silence", "check_backup", "check_site", "check_db_integrity",
              "check_sources", "check_liveness", "check_night", "check_sample"):
        assert watchdog.level_of(f"watchdog:{n}", cfg.levels) == "critical", n


def test_owner_mapping_of_d55(cfg):
    """D55 п. 6: збір стоїть, бекапу немає ніде, база пошкоджена, сайт не відкривається,
    нічна робота впала — критичні; одне сховище, підозрілі склеювання, райони — ні."""
    lv = cfg.levels
    for key in ("silence", "drop:olx", "backup-none", "db-integrity:realty", "site-local",
                "site-public", "night-failed", "unit-failed:realty-night.service",
                "liveness-fuse:domria", "liveness-sample-canary:domria"):
        assert watchdog.level_of(key, lv) == "critical", key
    for key in ("backup-partial", "dedup-suspicious", "dedup-complex", "places-would-change",
                "low:olx", "verify-blocks:olx", "night-blocked:olx.ua", "night-late"):
        assert watchdog.level_of(key, lv) == "warning", key


# --- Надсилання -------------------------------------------------------------------------------


def test_critical_is_sent_at_once_and_looks_different(env):
    _backup("failed", NOW - timedelta(hours=2), message="rclone: x; telegram: y", restored=1)
    rep = env["run"](NOW)
    assert rep["sent"] == ["backup-none"]
    msg = env["sent"][0]
    assert msg.splitlines()[0] == watchdog.CRITICAL_HEAD
    assert not msg.startswith(digest.DIGEST_HEAD)
    assert "👉 Що робити:" in msg and "CHECKPOINTS.md" in msg
    assert "локальна копія є" in msg


def test_warning_is_recorded_not_sent_and_resolution_is_remembered(env):
    _backup("ok", NOW - timedelta(hours=2), offsite="telegram:1",
            message="rclone: couldn't fetch token")
    rep = env["run"](NOW)
    assert rep["sent"] == [] and rep["warned"] == ["backup-partial"]
    st = env["state"]()["backup-partial"]
    assert st["level"] == "warning" and st["sent"] == 0 and "token" in st["text"]
    assert st["since"] == NOW.isoformat() and st["last_seen"] == NOW.isoformat()
    later = NOW + timedelta(hours=1)
    env["run"](later)
    assert env["state"]()["backup-partial"]["since"] == NOW.isoformat()
    assert env["state"]()["backup-partial"]["last_seen"] == later.isoformat()
    _backup("ok", NOW + timedelta(hours=2), offsite="telegram:2, rclone:gdrive:x")
    rep = env["run"](NOW + timedelta(hours=3))
    assert rep["resolved"] == ["backup-partial"]
    assert env["sent"] == []                     # попередження не шле і «відновилось»
    gone = env["state"]()["_resolved"]
    assert gone[-1]["key"] == "backup-partial" and gone[-1]["level"] == "warning"


def test_old_backup_key_continues_without_false_recovery(env):
    """Перейменування backup → backup-none: надіслана тривога не «відновлюється» хибно."""
    env["path"].write_text(json.dumps({"backup": {"since": NOW.isoformat(),
                                                  "last_sent": NOW.isoformat(), "sent": 1}}))
    _backup("failed", NOW - timedelta(hours=1), message="немає жодного місця поза машиною",
            restored=0)
    rep = env["run"](NOW + timedelta(minutes=30))
    assert rep["resolved"] == [] and rep["held"] == ["backup-none"]
    assert not any("Відновилось" in m for m in env["sent"])


def test_broken_alerts_config_makes_everything_critical(env, monkeypatch):
    def broken():
        raise configfiles.ConfigError("config/alerts.toml: зламано")
    monkeypatch.setattr(watchdog, "_alerts_cfg", broken)
    _backup("ok", NOW - timedelta(hours=2), offsite="telegram:1", message="rclone: x")
    rep = env["run"](NOW, digest=True)
    assert "backup-partial" in rep["sent"] and "watchdog:alerts-config" in rep["sent"]
    assert rep["digest"] is None


def test_backup_integrity_failure_is_a_db_integrity_alarm(env):
    _backup("failed", NOW - timedelta(hours=1), restored=0,
            message="RuntimeError: копія не пройшла integrity_check: row 5 missing")
    rep = env["run"](NOW)
    assert set(rep["sent"]) == {"backup-none", "db-integrity:backup"}


# --- Нові перевірки ---------------------------------------------------------------------------


def test_site_alarm_only_after_two_failed_runs_and_recovers(env, monkeypatch):
    (env["tmp"] / "public_url").write_text("https://mojkvartiry.test")
    monkeypatch.setattr(watchdog, "PUBLIC_URL_PATH", env["tmp"] / "public_url")
    asked = []
    up = {"http://127.0.0.1:8000/healthz": False, "https://mojkvartiry.test/healthz": True}

    def probe(url, timeout):
        asked.append(url)
        return up[url], "HTTP 200" if up[url] else "ConnectError"
    monkeypatch.setattr(watchdog, "SITE_PROBE", probe)
    assert env["run"](NOW)["active"] == []                       # перший невдалий — мовчимо
    assert set(asked) == set(up)
    rep = env["run"](NOW + timedelta(minutes=30))
    assert rep["sent"] == ["site-local"]
    assert "ConnectError" in env["sent"][0] and "realty-web" in env["sent"][0]
    up["http://127.0.0.1:8000/healthz"] = True
    rep = env["run"](NOW + timedelta(hours=1))
    assert rep["resolved"] == ["site-local"] and "Відновилось" in env["sent"][-1]
    # Перезапуск на 15 с між двома перевірками — не тривога: лічильник скинуто.
    up["https://mojkvartiry.test/healthz"] = False
    env["run"](NOW + timedelta(hours=2))
    up["https://mojkvartiry.test/healthz"] = True
    assert env["run"](NOW + timedelta(hours=2, minutes=30))["active"] == []


def test_integrity_runs_once_per_local_day_after_its_time(env, monkeypatch):
    calls = []

    def check(path, deadline):
        calls.append(path.name if path else None)
        return "ok" if path is None or path.name != "realty.db" or len(calls) < 3 else \
            "*** in database main *** Page 7: btreeInitPage() returns error code 11"
    monkeypatch.setattr(watchdog, "INTEGRITY_CHECK", check)
    early = datetime(2026, 10, 9, 4, 30)          # 07:30 за Києвом — ще не час (07:40)
    env["run"](early)
    assert calls == []
    env["run"](datetime(2026, 10, 9, 4, 45))      # 07:45 — раз
    env["run"](datetime(2026, 10, 9, 5, 15))      # той самий день — не вдруге
    assert len(calls) == 2
    # Північ UTC (03:00 за Києвом) — ще та сама місцева доба після 07:40? Ні: нова доба,
    # але до 07:40 — не час.
    env["run"](datetime(2026, 10, 10, 0, 30))
    assert len(calls) == 2
    rep = env["run"](datetime(2026, 10, 10, 4, 41))   # наступна доба — пошкоджено
    assert len(calls) == 4 and "db-integrity:realty" in rep["sent"]
    assert "db-integrity:ops" not in rep["active"]
    assert any("btreeInitPage" in m and m.startswith(watchdog.CRITICAL_HEAD)
               for m in env["sent"])


def test_quick_check_reads_only_and_reports(tmp_path):
    import sqlite3
    import time

    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (a)")
    con.execute("CREATE INDEX ix_t ON t (a)")
    con.executemany("INSERT INTO t VALUES (?)", [(f"рядок {i}",) for i in range(20000)])
    con.commit()
    con.close()
    before = db.read_bytes()
    assert watchdog._quick_check(db, time.monotonic() + 30) == "ok"
    assert watchdog._quick_check(tmp_path / "немає.db", time.monotonic() + 30) == "missing"
    assert watchdog._quick_check(db, time.monotonic() - 1) == "interrupted"
    assert db.read_bytes() == before
    (tmp_path / "bad.db").write_bytes(b"not a database at all" * 100)
    assert watchdog._quick_check(tmp_path / "bad.db", time.monotonic() + 30) not in (
        "ok", "interrupted", "missing")


def _src_run(source, started, inserted, updated=0):
    with ops.ops_session() as s:
        s.add(ops.RunRecord(source=source, mode="fresh", status="ok", started_at=started,
                            finished_at=started + timedelta(minutes=3), kept=inserted + updated,
                            inserted=inserted, updated=updated, requests_ok=50))


def test_low_new_listings_is_a_warning_but_early_stop_is_not(env):
    # Звичайно ~60 нових на добу (8 циклів), сьогодні — 6 (10%) при записаних оновленнях.
    for d in range(1, 8):
        for c in range(8):
            _src_run("domria", NOW - timedelta(days=d, hours=3 * c + 1), inserted=8, updated=20)
    for c in range(8):
        _src_run("domria", NOW - timedelta(hours=3 * c + 1), inserted=1 if c < 6 else 0,
                 updated=20)
        _src_run("olx", NOW - timedelta(hours=3 * c + 1), inserted=0, updated=5)
    rep = env["run"](NOW)
    assert "low:domria" in rep["warned"] and rep["sent"] == []
    assert "low:olx" not in rep["active"]          # без історії — без висновку


# --- Щоденне зведення -------------------------------------------------------------------------


def test_digest_is_sent_once_per_local_day_and_retried_after_failure(env, cfg):
    _backup("ok", NOW - timedelta(hours=7), offsite="telegram:1", message="rclone: token")
    # 08:30 за Києвом — ще рано.
    rep = env["run"](datetime(2026, 10, 9, 5, 30), digest=True)
    assert rep["digest"] is None
    fails = []

    def broken(text):
        if text.startswith(digest.DIGEST_HEAD):
            fails.append(text)
            raise RuntimeError("Telegram недоступний")
        env["sent"].append(text)
    rep = env["run"](datetime(2026, 10, 9, 5, 45), digest=True, send=broken)
    assert rep["digest"]["sent"] is False and fails
    assert any("зведення" in e for e in rep["errors"])
    rep = env["run"](datetime(2026, 10, 9, 6, 15), digest=True)       # повтор — вдався
    assert rep["digest"]["sent"] is True and rep["digest"]["date"] == "2026-10-09"
    text = env["sent"][-1]
    assert text.startswith(f"{digest.DIGEST_HEAD} · 09.10 (пт)")
    assert "backup-partial" in text
    for h in (1, 5, 12):                          # решта доби (і північ UTC о 03:00) — ні
        assert env["run"](datetime(2026, 10, 9, 6, 15) + timedelta(hours=h),
                          digest=True)["digest"] is None
    # Наступна місцева доба після 08:40 — нове.
    rep = env["run"](datetime(2026, 10, 10, 5, 41), digest=True)
    assert rep["digest"]["sent"] is True and rep["digest"]["date"] == "2026-10-10"


def test_digest_day_follows_local_not_utc_midnight(cfg):
    state = {}
    # 23:30 UTC 09.10 = 02:30 10.10 за Києвом: до 08:40 — не пора, хоча UTC-доба інша.
    assert digest.due(datetime(2026, 10, 9, 23, 30), state, cfg) is None
    # 22:00 UTC 09.10 = 01:00 10.10 — теж ні; 06:00 UTC 10.10 = 09:00 — так, за 10.10.
    assert digest.due(datetime(2026, 10, 10, 6, 0), state, cfg) == "2026-10-10"
    state["_digest"] = {"date": "2026-10-10"}
    assert digest.due(datetime(2026, 10, 10, 20, 59), state, cfg) is None   # 23:59 місцевого
    assert digest.due(datetime(2026, 10, 10, 21, 1), state, cfg) is None    # 00:01 11.10
    assert digest.due(datetime(2026, 10, 11, 5, 40), state, cfg) == "2026-10-11"


def test_digest_sections_survive_errors_and_report_all_clear(env, cfg, monkeypatch):
    def broken(ctx):
        raise ValueError("зламано навмисно")
    monkeypatch.setattr(digest, "SECTIONS", [("зламаний", broken), *digest.SECTIONS])
    text = digest.build(NOW, {}, cfg)
    assert "розділ «зламаний» не зібрано: ValueError" in text
    assert "✅ Усе гаразд" in text and "🔄 Цикли: 1" in text
    assert "💾 Бекапів ще не було" in text


def test_digest_lists_active_and_resolved_warnings_and_ongoing_criticals(env, cfg):
    state = {
        "dedup-suspicious": {"level": "warning", "since": (NOW - timedelta(hours=5)).isoformat(),
                             "text": "🧩 Зведення квартир: підозрілих 80, звичайно ~20"},
        "liveness-fuse:domria": {"level": "critical", "sent": 1,
                                 "since": (NOW - timedelta(hours=30)).isoformat(), "text": "x"},
        "_resolved": [{"key": "night-late", "level": "warning",
                       "since": (NOW - timedelta(hours=9)).isoformat(),
                       "resolved": (NOW - timedelta(hours=8)).isoformat()},
                      {"key": "low:olx", "level": "warning",
                       "since": (NOW - timedelta(hours=60)).isoformat(),
                       "resolved": (NOW - timedelta(hours=50)).isoformat()}],
    }
    text = digest.build(NOW, state, cfg)
    assert "⚠️ Попередження: активних 1, зникло 1" in text
    assert "dedup-suspicious" in text and "night-late" in text
    assert "low:olx" not in text                       # зникло поза вікном
    assert "🚨 Критичні, що досі тривають: liveness-fuse:domria" in text
    assert "Усе гаразд" not in text


def test_digest_is_cut_with_a_tail(cfg):
    lines = [f"рядок {i} " + "x" * 80 for i in range(200)]
    text = digest.fit(lines, cfg.digest.max_chars)
    assert len(text) <= cfg.digest.max_chars
    assert re.search(r"… ще \d+ рядк", text.splitlines()[-1])


def test_digest_backups_section_shows_each_storage(env, cfg):
    _backup("ok", NOW - timedelta(hours=40), offsite="rclone:gdrive:r, telegram:1")
    _backup("ok", NOW - timedelta(hours=6), offsite="telegram:2",
            message="rclone: couldn't fetch token: invalid_grant")
    text = "\n".join(digest.section_backups(digest.Ctx(NOW, NOW - timedelta(hours=24), {}, cfg)))
    assert "Telegram — ✅ 6 год тому" in text
    assert "Google Drive (rclone) — ❌ couldn't fetch token" in text and "40 год тому" in text


def test_digest_night_section_uses_evidence_summary_when_present(env, cfg, monkeypatch):
    from realty.night import report as night_report

    with ops.ops_session() as s:
        s.add(ops.NightRun(night_date="2026-10-09", window="04:10", status="partial",
                           started_at=NOW - timedelta(hours=9),
                           per_host=json.dumps({"dom.ria.com": {"keys": 40, "delisted": 3,
                                                                "restored": 1, "repaired": 2,
                                                                "unknown": 4}}),
                           lanes=json.dumps({"olx.ua": {"requests": 9, "stopped": "blocks"}}),
                           plan=json.dumps({"onetime_keys": 100}),
                           per_tier=json.dumps({"legacy_404": {"alive": 30, "removed": 10}})))
    ctx = digest.Ctx(NOW, NOW - timedelta(hours=24), {}, cfg)
    text = "\n".join(digest.section_night(ctx))
    assert "dom.ria.com: перевірено 40, знято 3, повернуто 1, полагоджено 2" in text
    assert "olx.ua" in text and "зупинено блокуваннями" in text
    assert "у плані 100 ключів, перевірено 40, лишилось ≈ 60" in text
    monkeypatch.setattr(night_report, "evidence_summary",
                        lambda session: ["докази типу продавця: OLX 120 із 400"], raising=False)
    assert "OLX 120 із 400" in "\n".join(digest.section_night(ctx))


def test_digest_shows_the_daily_integrity_result(env, cfg):
    state = {"_integrity": {"date": "2026-10-09", "at": (NOW - timedelta(hours=4)).isoformat(),
                            "results": {"realty": {"result": "ok", "seconds": 1.32},
                                        "ops": {"result": "interrupted", "seconds": 120.0}}}}
    text = digest.build(NOW, state, cfg)
    assert "🧪 Цілісність бази" in text and "realty ok (1.3 с)" in text
    assert "ops НЕ ВСТИГЛА" in text


def test_register_adds_a_section(monkeypatch):
    monkeypatch.setattr(digest, "SECTIONS", list(digest.SECTIONS))
    digest.register("тест", lambda ctx: ["рядок"], before="цикли")
    names = [n for n, _ in digest.SECTIONS]
    assert names.index("тест") == names.index("цикли") - 1


# --- Впала служба -----------------------------------------------------------------------------


def test_unit_failed_sends_at_once_then_counts_without_spam(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(watchdog, "_header", lambda: "[test]")
    monkeypatch.setattr(watchdog, "JOURNAL_TAIL", lambda unit, n: ["Traceback …", "Error: x"])
    sent = []
    units = tmp_path / "units.json"
    t0 = NOW
    assert watchdog.unit_failed("realty-cycle.service", now=t0, send=sent.append, cfg=cfg,
                                path=units) == 0
    assert len(sent) == 1 and sent[0].startswith(watchdog.CRITICAL_HEAD)
    assert "realty-cycle.service" in sent[0] and "Error: x" in sent[0]
    assert "journalctl --user -u realty-cycle.service" in sent[0]
    for m in (5, 10, 20):                     # Restart=on-failure: падає знову й знову
        watchdog.unit_failed("realty-cycle.service", now=t0 + timedelta(minutes=m),
                             send=sent.append, cfg=cfg, path=units)
    assert len(sent) == 1
    watchdog.unit_failed("realty-night.service", now=t0 + timedelta(minutes=21),
                         send=sent.append, cfg=cfg, path=units)
    assert len(sent) == 2                     # інша служба — окремо
    watchdog.unit_failed("realty-cycle.service", now=t0 + timedelta(minutes=61),
                         send=sent.append, cfg=cfg, path=units)
    assert len(sent) == 3 and "ще 3 раз" in sent[2]
    assert json.loads(units.read_text())["realty-cycle.service"]["count"] == 5


def test_unit_failed_never_raises_and_queues_for_the_watchdog(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(watchdog, "_header", lambda: "[test]")

    def down(text):
        raise RuntimeError("Telegram недоступний")
    box = tmp_path / "outbox.json"
    assert watchdog.unit_failed("realty-backup.service", now=NOW, send=down, cfg=cfg,
                                path=tmp_path / "u.json", outbox=box) == 1
    assert json.loads(box.read_text())[0]["key"] == "unit-failed:realty-backup.service"
    # Навіть зовсім зламане (неможливий шлях) — код 1, а не виняток.
    assert watchdog.unit_failed("x.service", now=NOW, send=down, cfg=cfg,
                                path=Path("/dev/null/немає/u.json")) == 1
    assert watchdog.unit_failed("bad name; rm -rf /", now=NOW, send=lambda t: None, cfg=cfg,
                                path=tmp_path / "u2.json") == 0
    sent = []
    errors: list[str] = []
    assert watchdog.flush_outbox(sent.append, errors, box) == ["unit-failed:realty-backup.service"]
    assert "із запізненням" in sent[0] and json.loads(box.read_text()) == []


def test_watchdog_flushes_the_outbox_first(env):
    watchdog.queue_outbox("unit-failed:realty-web.service", "🚨 КРИТИЧНО\nвпала", NOW)
    rep = env["run"](NOW)
    assert rep["outbox"] == ["unit-failed:realty-web.service"] and "впала" in env["sent"][0]


def test_journal_lines_are_scrubbed_of_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAH-very-secret-token-value-xxxxxxxxx")
    monkeypatch.setenv("AUTH_PASSWORD", "пароль-власника")
    for line in ("Error: https://api.telegram.org/bot123456789:AAH-very-secret-token-value-"
                 "xxxxxxxxx/sendMessage", "login failed for password=пароль-власника",
                 "Authorization: Basic dmFzaWE6cGFzc3dvcmQxMjM0NTY3ODkwYWJjZGVmZ2hpams=",
                 "token 987654321:ZZZ_another_token_from_elsewhere_yyyyyyyyy"):
        out = watchdog._scrub(line)
        assert "secret-token" not in out and "пароль-власника" not in out, out
        assert "ZZZ_another" not in out and "dmFzaWE6" not in out, out
    assert watchdog._scrub("File \"/home/u/realty/realty/runner.py\", line 12") == \
        "File \"/home/u/realty/realty/runner.py\", line 12"


def test_alert_unit_template_and_on_failure_hooks():
    import configparser

    units = ROOT / "deploy" / "fedora" / "systemd"
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str
    p.read(units / "realty-alert@.service", encoding="utf-8")
    assert p["Service"]["ExecStart"].endswith("cli.py alert unit-failed %i")
    assert "OnFailure" not in p["Unit"]                     # без петлі
    for name in ("realty-cycle.service", "realty-night.service", "realty-backup.service",
                 "realty-web.service", "realty-liveness-sample.service"):
        q = configparser.ConfigParser(strict=False, interpolation=None)
        q.optionxform = str
        q.read(units / name, encoding="utf-8")
        assert q["Unit"].get("OnFailure") == "realty-alert@%n.service", name


# --- Контрольні ключі перевірки актуальності (гілка запобіжника, D58) ------------------------


def _lrun(at, *, kind="cycle", fuse=None, report=None, plan=None):
    with ops.ops_session() as s:
        s.add(ops.LivenessRun(kind=kind, status="ok", started_at=at, finished_at=at,
                              fuse=json.dumps(fuse) if fuse is not None else None,
                              report=json.dumps(report) if report is not None else None,
                              per_tier=json.dumps({"plan": plan, "verdicts": {}})
                              if plan is not None else None))


def test_genuine_canary_removal_is_a_warning(env, cfg):
    _lrun(NOW - timedelta(hours=2), fuse={"trips": [], "canary_genuine": [
        {"key": "domria:123", "url": "https://dom.ria.com/uk/x-123.html"}]})
    _lrun(NOW - timedelta(hours=30), fuse={"canary_genuine": [{"host": "olx.ua"}]})  # давно
    _lrun(NOW - timedelta(hours=1), kind="night", report={"canary_genuine": []})
    keys = {a.key: a.text for a in watchdog.check_canaries(NOW)}
    assert list(keys) == ["liveness-canary-genuine:dom.ria.com"]
    assert "x-123" in keys["liveness-canary-genuine:dom.ria.com"]
    assert watchdog.level_of("liveness-canary-genuine:dom.ria.com", cfg.levels) == "warning"


def test_no_canary_planned_three_cycles_in_a_row_is_a_warning(env, cfg):
    hosts = configfiles.load("liveness").hosts
    full = {h: {"canary": s.canaries_per_run} for h, s in hosts.items() if s.checkable}
    for i in range(3):
        _lrun(NOW - timedelta(hours=3 * i + 1), plan=full)
    assert watchdog.check_canaries(NOW) == []
    no_olx = {**full, "olx.ua": {"sweep": 400}}
    for i in range(2):
        _lrun(NOW + timedelta(minutes=i + 1), plan=no_olx)
    assert watchdog.check_canaries(NOW + timedelta(hours=1)) == []         # лише 2 поспіль
    _lrun(NOW + timedelta(minutes=5), plan=no_olx)
    keys = [a.key for a in watchdog.check_canaries(NOW + timedelta(hours=1))]
    assert keys == ["liveness-no-canary:olx.ua"]
    assert watchdog.level_of(keys[0], cfg.levels) == "warning"


def test_canary_checks_skip_runs_without_the_new_fields(env):
    _lrun(NOW - timedelta(hours=1), fuse={"trips": []})
    with ops.ops_session() as s:
        s.add(ops.LivenessRun(kind="cycle", status="ok", started_at=NOW, per_tier="не json"))
    assert watchdog.check_canaries(NOW) == []


def test_broken_outbox_or_state_does_not_stop_critical_alerts(env, monkeypatch):
    def broken(*a, **k):
        raise OSError("диск переповнено")
    monkeypatch.setattr(watchdog, "flush_outbox", broken)
    env["path"].write_text(json.dumps({"_resolved": [{"key": "x", "resolved": "не дата"}, 5],
                                       "night-late": {"since": NOW.isoformat(), "sent": 0,
                                                      "level": "warning"}}))
    _backup("failed", NOW - timedelta(hours=1), message="немає жодного місця поза машиною",
            restored=0)
    rep = env["run"](NOW)
    assert rep["sent"] == ["backup-none"] and any("черга" in e for e in rep["errors"])
    assert rep["resolved"] == ["night-late"]
    assert [r["key"] for r in env["state"]()["_resolved"]] == ["night-late"]
