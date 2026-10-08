"""Нічний диригент — рецензія E9 (D53): запобіжник, замок бази, дедлайни, записи, звірка.

Той самий `Conductor` і та сама `Lane`, віртуальний час і мережа (night_kit). Кожен тест —
поведінка, якої до рецензії не було:
  * повторна перевірка незастосованого «живе» (ярус held_return) рахується в запобіжнику;
  * «database is locked» на пакеті не валить ніч — результати йдуть у наступний пакет;
  * зупинка смуги блокуваннями — у записі ночі одразу (і коли диригент потім падає);
  * бекап не запускається під замком, якщо смуг однаково не буде; останній пакет — лише
    якщо до межі замка ≥ 2 хв; жорстка зупинка за release_lock − 60 с;
  * процес перевірки при відкритті, що вже йде, — дочекатися, перш ніж питати сайти;
  * запис ночі закривається завжди (SIGTERM під час очікування замка, збій після замка);
  * звірка «актуальних після» — з тим, що дописав дозбір identity, решта — «НЕЗВІРЕНО»;
  * спрацювання запобіжника — з номером пакета й тим, що вже застосовано до нього;
  * вікно з M2/M3 бекапиться за onetime_max_age_hours, стале — за max_age_hours.
"""
from __future__ import annotations

import dataclasses
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, add, clean_fuse, db, events, get, olx_url, ria_page, ria_url, rieltor_url,
)
from night_kit import (  # noqa: E402,F401
    FakeClock, TimedNet, VirtualLauncher, clean_night, local_epoch, night_env, scope_of, utc_of,
)

from realty import configfiles, ops  # noqa: E402
from realty.liveness import policy  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse", "clean_night")

T0 = local_epoch(2026, 10, 9, 1, 10)
GONE = datetime(2026, 9, 1)


def _cfgs(mode="literal", *, jobs=None, lanes=None):
    lcfg = policy.load()
    lcfg = dataclasses.replace(lcfg, fuse=dataclasses.replace(lcfg.fuse, mode=mode))
    ncfg = configfiles.load("night")
    if jobs:
        ncfg = dataclasses.replace(ncfg, jobs=dataclasses.replace(ncfg.jobs, **jobs))
    if lanes:
        ncfg = dataclasses.replace(ncfg, lanes=dataclasses.replace(ncfg.lanes, **lanes))
    return lcfg, ncfg


def _night(Session, tmp_path, net, *, t0=None, mode="literal", clock=None, launcher=None,
           identity_fn=None, hang=(), jobs=None, lanes=None, raises=None, **env_kw):
    from realty.night.conductor import Conductor

    lcfg, ncfg = _cfgs(mode, jobs=jobs, lanes=lanes)
    master = clock or FakeClock(t0 or T0)
    timed = net if isinstance(net, TimedNet) else TimedNet(net)
    launcher = launcher or VirtualLauncher(master, timed, lcfg, identity_fn=identity_fn,
                                           hang=hang)
    env, calls = night_env(tmp_path, scope_of(Session), master, launcher, **env_kw)
    c = Conductor(ncfg=ncfg, nhash="n" * 16, lcfg=lcfg, lhash="l" * 16, env=env)
    if raises is not None:
        with pytest.raises(raises):
            c.run()
        return None, timed, master, calls
    return c.run(), timed, master, calls


def _row(run_id=None) -> dict:
    from realty.night import report

    return report.runs(1, run_id=run_id)[0]


def _seed_reseen_olx(Session, n, prefix="10Rs"):
    """Зняті старим кодом (без події removed), знову бачені в стрічці — M2 onetime_reseen."""
    return {f"{prefix}{i:03d}": add(Session, olx_url(f"{prefix}{i:03d}"), source="olx",
                                    external_id=f"{prefix}{i:03d}", is_active=False,
                                    delisted_at=GONE, last_seen=datetime(2026, 9, 25))
            for i in range(n)}


# --- Запобіжник: незастосоване «живе», перевірене знову ----------------------------------------


@pytest.mark.parametrize("mode", ["literal", "tiered"])
def test_recheck_of_unapplied_returns_counts_in_the_fuse(db, tmp_path, mode):
    """30 змішаних ключів OLX: актуальний рядок olx + копія LUN, знята новим кодом; остання
    відповідь новим підписом — «живе» (не застосована, поки lun тримався; власник відпустив).
    Уночі сайт відповідає 410 на кожен (напр., зламане правило CDN): 100% «знято» з n = 30.
    Свіжого «знято» власник не бачив — запобіжник рахує ці перевірки й тримає olx (до
    рецензії ярус «held» був поза частками в обох режимах — 30 з 30 знято без оцінки)."""
    from realty.models import CheckEvent, ListingEvent

    olx_ids = []
    for i in range(30):
        tok = f"10Mx{i:03d}"
        url = olx_url(tok)
        olx_ids.append(add(db, url, source="olx", external_id=tok))
        lun = add(db, url, source="lun", external_id=f"lun{i}", is_active=False,
                  delisted_at=GONE, last_seen=datetime(2026, 8, 30))
        with db() as s:
            s.add(ListingEvent(listing_id=lun, at=GONE, kind="removed", reason="status_410",
                               source="lun"))
            for lid in (olx_ids[-1], lun):
                s.add(CheckEvent(listing_id=lid, checked_at=datetime(2026, 10, 8, 10, 0),
                                 code=200, alive=True, reason="sweep", signature="alive"))
            s.commit()
    res, timed, *_ = _night(db, tmp_path, FakeNet(default=410), mode=mode)
    assert _row(res["night_run_id"])["plan"]["hosts"]["olx.ua"]["tiers"] == {"held_return": 30}
    assert "olx" in {t["source"] for t in res["trips"]}
    assert all(get(db, lid).is_active for lid in olx_ids)
    assert not [e for e in events(db) if e.kind == "removed" and e.listing_id in olx_ids]


# --- Замок бази: пакет відкладено, ніч триває -------------------------------------------------


def test_locked_database_defers_the_batch_and_the_night_goes_on(db, tmp_path, monkeypatch):
    """Інший процес (дозбір identity DOM.RIA у старому вигляді, будь-хто) тримає замок
    запису SQLite довше за busy_timeout саме на межі пакета: пакет повторюється, далі його
    результати йдуть у НАСТУПНИЙ пакет — ніч не «failed», смуги не вбито, і підсумки ночі
    дорівнюють подіям у базі (раніше — OperationalError, усі смуги вбито, ніч «failed»)."""
    from realty.liveness import apply as lv_apply

    monkeypatch.setattr(lv_apply, "LOCKED_RETRY_WAIT_S", 0.0, raising=False)
    reseen = _seed_reseen_olx(db, 30)
    for i in range(400):                       # rieltor M3: 20 хв роботи — пакет о 15-й хв
        add(db, rieltor_url(6_400_000 + i), source="lun", external_id=f"lk{i}")
    path = db.kw["bind"].url.database
    short = sessionmaker(bind=create_engine(f"sqlite:///{path}", future=True,
                                            connect_args={"timeout": 0.05}),
                         expire_on_commit=False, future=True)
    blocker = sqlite3.connect(path, timeout=0.05, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")                         # замок запису — у «чужого»
    real = lv_apply.apply_outcomes
    calls = {"n": 0}

    def apply_outcomes(*a, **kw):
        calls["n"] += 1
        try:
            return real(*a, **kw)
        finally:
            if calls["n"] == 1:                                # «чужий» закінчив після пакета 1
                blocker.execute("ROLLBACK")
                blocker.close()

    monkeypatch.setattr(lv_apply, "apply_outcomes", apply_outcomes)
    res, *_ = _night(short, tmp_path, FakeNet(default=200))
    assert res["status"] in ("ok", "partial") and res["unapplied"] == 0
    row = _row(res["night_run_id"])
    first = row["batches"][0]
    assert "locked" in (first["error"] or "") and first["pending"] > 0
    assert first["returned"] == 0 and first["rows"] == 0
    assert row["batches"][1]["retried"] == first["pending"]
    assert "mem_available_mb" in first
    returned = [e for e in events(db) if e.kind == "returned"]
    assert len(returned) == 30 == sum(d["restored"] for d in row["per_host"].values())
    assert all(get(db, lid).is_active for lid in reseen.values())
    assert row["active_after"] == row["active_before"] + 30


# --- Блокування — у записі ночі одразу ----------------------------------------------------------


def test_a_block_reaches_the_night_record_before_the_conductor_fails(db, tmp_path, monkeypatch):
    """Смугу rieltor зупинили блокування на першій хвилині, а диригент упав на пакеті о 15-й:
    запис ночі однаково каже «blocks» (сторож — тривога, вікно 04:10 — без хоста). Раніше
    зупинка потрапляла в запис лише в кінці вікна і губилась разом з аварією."""
    from realty.liveness import apply as lv_apply

    for i in range(40):
        add(db, rieltor_url(6_500_000 + i), source="lun", external_id=f"b{i}")
    for i in range(1200):                                      # DOM.RIA — 20 хв роботи
        add(db, ria_url(34_500_000 + i), source="domria", external_id=str(34_500_000 + i))

    real = lv_apply.apply_outcomes
    broken = {"on": True}

    def apply_outcomes(*a, **kw):
        if broken["on"]:
            raise RuntimeError("диск")
        return real(*a, **kw)

    monkeypatch.setattr(lv_apply, "apply_outcomes", apply_outcomes)
    net = TimedNet(FakeNet(default=lambda url, m: 403 if "rieltor.ua" in url else 200))
    _night(db, tmp_path, net, raises=RuntimeError)
    row = _row()
    assert row["status"] == "failed"
    assert row["lanes"]["rieltor.ua"]["stopped"] == "blocks"
    broken["on"] = False
    n = len(net.log)
    res2, *_ = _night(db, tmp_path, net, t0=local_epoch(2026, 10, 9, 4, 10))
    plan = _row(res2["night_run_id"])["plan"]["hosts"]["rieltor.ua"]
    assert "блокування цієї ночі" in (plan["skipped"] or "")
    assert not [1 for h, *_ in net.log[n:] if h == "rieltor.ua"]


# --- Дедлайни замка ------------------------------------------------------------------------------


def test_no_backup_under_the_lock_when_no_lane_will_run(db, tmp_path):
    """Замок узято о 02:40, а до stop_requests (02:47) менше за min_work (10 хв): смуг не
    буде — і бекапу під замком теж (раніше бекап ішов, займаючи замок без жодної смуги)."""
    add(db, olx_url("10Lt001"), source="olx", external_id="lt")
    res, net, _m, calls = _night(db, tmp_path, FakeNet(default=200),
                                 t0=local_epoch(2026, 10, 9, 2, 40), due=True)
    assert res["status"] == "ok" and calls["backup"] == 0 and net.log == []


class _SlowStopLauncher(VirtualLauncher):
    """Зависла смуга, примусова зупинка якої триває `stop_s` (KILL_GRACE, повільний диск)."""

    def __init__(self, *a, stop_s: float, **kw) -> None:
        super().__init__(*a, **kw)
        self.stop_s = stop_s

    def start(self, host, plan_path, out_path):
        lane = super().start(host, plan_path, out_path)
        real_stop = lane.stop

        def stop():
            self.master.sleep(self.stop_s)
            real_stop()

        lane.stop = stop
        return lane


def test_final_batch_is_skipped_when_the_lock_deadline_is_near(db, tmp_path):
    """Зависла смуга зупинялась 6 хв: о 02:54 до звільнення замка (02:55) < 2 хв — останній
    пакет не застосовуємо (запис на «busy» SQLite міг би перейти межу); ці ключі не
    позначені «пробували» — наступне вікно їх візьме."""
    lid = add(db, olx_url("10Fz001"), source="olx", external_id="fz", is_active=False,
              delisted_at=GONE, last_seen=datetime(2026, 9, 25))
    for i in range(3):
        add(db, rieltor_url(6_600_000 + i), source="lun", external_id=f"fz{i}")
    lcfg, _ = _cfgs()
    master = FakeClock(T0)
    timed = TimedNet(FakeNet(default=200))
    launcher = _SlowStopLauncher(master, timed, lcfg, hang={"rieltor.ua"}, stop_s=6 * 60)
    res, *_ = _night(db, tmp_path, timed, clock=master, launcher=launcher,
                     lanes={"batch_minutes": 200})
    assert get(db, lid).is_active is False and get(db, lid).last_attempt is None
    assert res["unapplied"] == 1 and "не застосовано 1" in (res["message"] or "")
    row = _row(res["night_run_id"])
    assert row["lock_released_at"] <= row["release_lock_at"]


def test_hard_stop_alarm_raises_before_the_lock_deadline():
    """Справжній годинник: за release_lock − 60 с — SIGALRM → LockDeadline (BaseException:
    жоден `except Exception` по дорозі не ковтає) — смуги зупиняються, замок звільняється."""
    from realty.night.conductor import Env, LockDeadline

    disarm = Env().arm_hard_stop(time.time() + 0.2)
    try:
        with pytest.raises(LockDeadline):
            time.sleep(5)
    finally:
        disarm()
    assert not issubclass(LockDeadline, Exception)


class _ReleasingClock(FakeClock):
    """Годинник, на якому процес перевірки при відкритті закінчує через `after` секунд."""

    def __init__(self, t0, lock, after) -> None:
        super().__init__(t0)
        self.t0, self.lock, self.after = t0, lock, after
        self.released_at = None

    def sleep(self, s: float) -> None:
        super().sleep(s)
        if self.released_at is None and self.t - self.t0 >= self.after:
            self.lock.release()
            self.released_at = self.t


def test_lanes_wait_for_a_running_open_card_check(db, tmp_path):
    """Процес перевірки при відкритті (realty-lookup@) стартував до того, як ніч узяла
    замок, і ще питає сайти: смуги стартують лише після нього (інакше на rieltor.ua — два
    потоки по 3,0 с, тобто 1,5 с)."""
    from realty.runner import CycleLock

    add(db, rieltor_url(6_700_001), source="lun", external_id="dr")
    drain = CycleLock(tmp_path / "lookup.lock")
    assert drain.acquire()
    clock = _ReleasingClock(T0, drain, after=40)
    try:
        res, net, *_ = _night(db, tmp_path, FakeNet(default=200), clock=clock)
    finally:
        drain.release()
    assert clock.released_at is not None and net.log
    assert min(t for _h, t, *_ in net.log) >= clock.released_at
    assert _row(res["night_run_id"])["plan"]["drain_waited_s"] >= 40


# --- Запис ночі закривається завжди ------------------------------------------------------------


class _TermClock(FakeClock):
    def sleep(self, s: float) -> None:
        super().sleep(s)
        if len(self.sleeps) >= 2:
            raise SystemExit(143)                              # systemctl stop під час очікування


def test_sigterm_while_waiting_for_the_lock_closes_the_record(db, tmp_path):
    from realty.runner import CycleLock

    cycle = CycleLock(tmp_path / "cycle.lock")
    assert cycle.acquire()
    try:
        _night(db, tmp_path, FakeNet(default=200), clock=_TermClock(T0), raises=SystemExit)
    finally:
        cycle.release()
    row = _row()
    assert row["status"] == "failed" and row["finished_at"] is not None
    assert "SystemExit" in row["message"]


def test_failure_after_the_lock_still_closes_both_records(db, tmp_path, monkeypatch):
    """Збій уже без замка (ops.db зайнята, утримання хостів): запис ночі закрито зі статусом
    вікна й причиною, рядок ops.liveness_runs — «failed» (раніше обидва — «running»)."""
    from realty.night.conductor import Conductor

    lid = add(db, olx_url("10Af001"), source="olx", external_id="af", is_active=False,
              delisted_at=GONE, last_seen=datetime(2026, 9, 25))

    def broken(self, win):
        raise RuntimeError("ops.db зайнята")

    monkeypatch.setattr(Conductor, "_open_holds", broken)
    res, *_ = _night(db, tmp_path, FakeNet(default=200))
    assert get(db, lid).is_active is True                      # дані записано до збою
    row = _row(res["night_run_id"])
    assert row["status"] == "ok" and row["finished_at"] is not None
    assert "після звільнення замка: RuntimeError" in row["message"]
    with ops.ops_session() as s:
        assert s.get(ops.LivenessRun, res["liveness_run_id"]).status == "failed"


# --- Звірка ночі ---------------------------------------------------------------------------------


def _identity_writer(Session, n_new, n_prices, n_reported):
    """Дозбір identity LUN — як прохід стрічки: дописує рядки й події ціни (звичайний збір)."""
    from realty.models import Listing, PriceEvent

    def identity(source, stop_at, gate=None):
        if source != "lun":
            return {"done": {source: "не потрібно"}}
        with Session() as s:
            for i in range(n_new):
                s.add(Listing(source="lun", external_id=f"feed{i}", original_url=f"https://f/{i}",
                              is_active=True, last_seen=datetime(2026, 10, 8)))
            s.flush()
            first = s.scalar(select(func.min(Listing.id)).where(Listing.external_id == "feed0"))
            for _ in range(n_prices):
                s.add(PriceEvent(listing_id=first, source="lun", price=1.0, price_usd=1.0))
            s.commit()
        return {"done": {"lun": "ok"}, "written": {"lun": {"listings": n_reported,
                                                          "price_events": n_prices}}}

    return identity


def test_night_reconciles_with_what_the_identity_feed_pass_wrote(db, tmp_path):
    from realty.night import report

    _seed_reseen_olx(db, 5)
    res, *_ = _night(db, tmp_path, FakeNet(default=200),
                     identity_fn=_identity_writer(db, 3, 2, 3))
    row = _row(res["night_run_id"])
    text = report.render_run(row)
    assert row["totals"]["after"]["listings"] - row["totals"]["before"]["listings"] == 3
    assert "повернуто 5, знято 0, нових від дозбору identity 3" in text
    assert "подій ціни: +2" in text and "звірка: збіглось" in text
    assert "НЕЗВІРЕНО" not in text


def test_unexplained_changes_are_shown_not_absorbed(db, tmp_path):
    """Дописано 4, а дозбір звітує про 3: залишок — «НЕЗВІРЕНО», а не «інші записи», що
    сходились би за побудовою."""
    from realty.night import report

    _seed_reseen_olx(db, 5)
    res, *_ = _night(db, tmp_path, FakeNet(default=200),
                     identity_fn=_identity_writer(db, 4, 0, 3))
    text = report.render_run(_row(res["night_run_id"]))
    assert "НЕЗВІРЕНО: актуальних +1, рядків +1" in text


# --- Спрацювання запобіжника в пакеті, після вже застосованого ----------------------------------


def test_trip_names_its_batch_and_what_was_applied_before(db, tmp_path):
    """Пакет (1 хв тут, 15 хв уночі) — «прогін» запобіжника. OLX: у пакеті 1 повернуто
    ~20, у пакеті 2 — «знято» понад 20%: звіт не каже «нічого не знято й не повернуто» за
    ніч, а «з пакета 2 … до того в цьому вікні: знято 0, повернуто N»."""
    from realty.night import report

    reseen = _seed_reseen_olx(db, 25)
    for i in range(40):
        add(db, olx_url(f"10Tb{i:03d}"), source="olx", external_id=f"tb{i}")
    resp = {f"olx:{tok}": 200 for tok in reseen}
    res, *_ = _night(db, tmp_path, FakeNet(resp, default=410), lanes={"batch_minutes": 1})
    trip = next(t for t in res["trips"] if t["source"] == "olx" and t.get("new"))
    row = _row(res["night_run_id"])
    first = row["batches"][0]["returned"]
    assert 0 < first < 25 and row["batches"][0]["trips"] == []
    assert trip["batch"] == 2 and trip["before"] == {"delisted": 0, "restored": first}
    text = report.render_run(row)
    assert "з пакета 2 (" in text
    assert f"до того в цьому вікні: знято 0, повернуто {first}" in text
    assert "нічого не знято й не повернуто" not in text


# --- Бекап на старті: вікно з M2/M3 ----------------------------------------------------------------


def test_window_with_onetime_work_backs_up_when_the_last_backup_is_3h_old(db, tmp_path):
    """Бекапи лягають у кінці циклів раз на ~21 год: за правилом «старший за 20 год» ніч
    майже ніколи не бекапилась, і перші ночі M2/M3 писали б на бекапі до 20 год. Вікно з
    M2/M3 — бекап, якщо останній успішний старший за onetime_max_age_hours (3 год)."""
    add(db, ria_url(34_800_001), source="domria", external_id="34800001")    # M3: без відповіді
    res, _n, _m, calls = _night(db, tmp_path, FakeNet(default=200), backup_age_hours=5)
    assert calls["backup"] == 1
    row = _row(res["night_run_id"])
    assert row["backup"]["rule"] == "onetime_max_age_hours" and row["plan"]["onetime_keys"] == 1


def test_steady_window_keeps_the_20_hour_rule(db, tmp_path):
    """Лише контрольні (ключ уже має відповідь новим підписом, свіжо бачений) — бекап 5-годинної
    давності свіжий: окремого бекапу немає."""
    from realty.models import CheckEvent

    lid = add(db, ria_url(34_800_002), source="domria", external_id="34800002",
              last_seen=utc_of(T0 - 3600))
    with db() as s:
        s.add(CheckEvent(listing_id=lid, checked_at=utc_of(T0 - 7200), code=200, alive=True,
                         reason="sweep", signature="alive"))
        s.commit()
    res, _n, _m, calls = _night(db, tmp_path, FakeNet(default=200), backup_age_hours=5)
    row = _row(res["night_run_id"])
    assert row["plan"]["hosts"]["dom.ria.com"]["tiers"] == {"canary": 1}
    assert calls["backup"] == 0 and row["backup"]["due"] is False
