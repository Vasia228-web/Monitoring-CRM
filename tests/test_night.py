"""Нічний диригент `cli.py night` (E9, D53): симульована ніч у віртуальному часі.

Той самий `Conductor` і та сама смуга (`night.lane.Lane`), що й у роботі; змінено
лише годинник (FakeClock), процеси смуг (VirtualLane — у процесі тесту, кожна зі своїм
часом) і мережу (FakeNet за ключем «сайт:id»). На коді до E9 нічного диригента немає
(старий realty-identity лише дозбирав ознаки) — тести падають.

Що доводять:
  * ніч іде в межах дедлайнів: жодного запиту після stop_requests, замок звільнено до
    release_lock, навіть якщо смуга зависла (примусова зупинка);
  * темп кожного хоста — старт-до-старту ≥ policy.pace(mode="night"), хости паралельно;
  * результати застосовуються пакетами раз на ~15 хв ПІСЛЯ запобіжника, з подіями;
    покоління кешу «lists» — після кожного пакета, «analytics» — у кінці вікна;
  * M2 повертає лише тих, кого жива перевірка показала живими; 410 лишається знятим;
  * запобіжник: literal тримає джерело, де «знято» > 20% перевірених у пакеті, — нічого
    не знято й не повернуто, решту ключів смуга вже не питає; tiered — уже зняті рядки
    M2 поза частками;
  * бекап не вдався — нічого не пишемо; замок не звільнився — вікно пропускаємо;
  * блокування зупиняють смугу до кінця ночі; дві ночі поспіль — хост чекає рішення;
  * один запит на ключ за ніч (друге вікно не питає тих, кого пробувало перше);
  * last_seen не пишеться (D43).
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, add, clean_fuse, db, events, get, olx_url, ria_page, ria_url, rieltor_url,
)
from night_kit import (  # noqa: E402,F401
    FakeClock, TimedNet, VirtualLauncher, clean_night, local_epoch, night_env, scope_of,
)

from realty import configfiles, ops  # noqa: E402
from realty.liveness import policy  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse", "clean_night")

T0 = local_epoch(2026, 10, 9, 1, 10)                  # старт таймера (місцевий час)
GONE = datetime(2026, 9, 1)


def _cfgs(mode="literal", **jobs):
    lcfg = policy.load()
    lcfg = dataclasses.replace(lcfg, fuse=dataclasses.replace(lcfg.fuse, mode=mode))
    ncfg = configfiles.load("night")
    if jobs:
        ncfg = dataclasses.replace(ncfg, jobs=dataclasses.replace(ncfg.jobs, **jobs))
    return lcfg, ncfg


def _run(db, tmp_path, net, *, t0=T0, mode="literal", budget_min=None, hang=(), due=False,
         backup=None, identity_fn=None, jobs=None, lcfg=None):
    from realty.night.conductor import Conductor

    lc, ncfg = _cfgs(mode, **(jobs or {}))
    lcfg = lcfg or lc
    master = FakeClock(t0)
    timed = net if isinstance(net, TimedNet) else TimedNet(net)
    launcher = VirtualLauncher(master, timed, lcfg, identity_fn=identity_fn, hang=hang)
    env, calls = night_env(tmp_path, scope_of(db), master, launcher, due=due, backup=backup)
    res = Conductor(ncfg=ncfg, nhash="n" * 16, lcfg=lcfg, lhash="l" * 16, env=env,
                    budget_min=budget_min).run()
    return res, timed, master, calls, ncfg, lcfg


def _night_row(res) -> dict:
    from realty.night import report

    return report.runs(1, run_id=res["night_run_id"])[0]


def _last_seen_sum(db):
    from realty.models import Listing

    with db() as s:
        return s.execute(select(func.count(), func.max(Listing.last_seen),
                                func.sum(func.julianday(Listing.last_seen)))).one()


def _gens():
    from realty import webcache

    return webcache.read()


def test_simulated_night_runs_within_deadlines_in_batches_with_events(db, tmp_path):
    # OLX M2: 40 зняті старим кодом, знову бачені: 32 живі, 8 — 410.
    reseen = {}
    for i in range(40):
        tok = f"10Ms{i:03d}"
        reseen[tok] = add(db, olx_url(tok), source="olx", external_id=tok, is_active=False,
                          delisted_at=GONE, last_seen=datetime(2026, 9, 25))
    # rieltor M3: 600 актуальних копій LUN без перевірки новим підписом — 30 хв роботи
    # при 3,0 с; стеля --budget-min 20 зупинить смугу раніше.
    for i in range(600):
        add(db, rieltor_url(5000000 + i), source="lun", external_id=f"r{i}")
    # DOM.RIA M3: 30 ключів, 3 — банер «видалено» (10% < 20%).
    for i in range(30):
        add(db, ria_url(34500000 + i), source="domria", external_id=str(34500000 + i))

    def olx(url, method):
        n = int(url.rsplit("ID10Ms", 1)[1].split(".")[0])
        return 410 if n % 5 == 0 else 200

    def ria(url, method):
        rid = int(url.rsplit("-", 1)[1].split(".")[0])
        return 200, ria_page(rid, archived=rid % 10 == 0)

    responses = {f"olx:{t}": olx for t in reseen}
    responses.update({f"domria:{34500000 + i}": ria for i in range(30)})
    before_seen = _last_seen_sum(db)
    gens0 = _gens()
    res, net, master, calls, ncfg, lcfg = _run(db, tmp_path, FakeNet(responses, default=200),
                                               budget_min=20)
    assert res["status"] == "ok", res
    row = _night_row(res)
    stop_at = T0 + 20 * 60
    # Жодного запиту після стелі; темп кожного хоста — не швидший за нічний.
    assert net.log and max(t for _h, t, _u, _m in net.log) < stop_at
    for host in {h for h, *_ in net.log}:
        times = sorted(t for h, t, _u, _m in net.log if h == host)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert min(gaps, default=99) >= policy.pace(lcfg, host, "night") - 1e-6, host
    # rieltor не встиг усе — це норма (M3 — кілька вікон); решта — наступного вікна.
    lanes = row["lanes"]
    assert lanes["rieltor.ua"]["stopped"] == "deadline"
    assert 0 < lanes["rieltor.ua"]["not_reached"] < 600
    assert lanes["rieltor.ua"]["requests"] == pytest.approx(20 * 60 / 3.0, abs=2)
    # Замок звільнено до release_lock; перетинів із циклом немає.
    assert row["lock_released_at"] <= row["release_lock_at"]
    assert (master.time() - T0) < (local_epoch(2026, 10, 9, 2, 55) - T0)
    # Пакети раз на 15 хв (+ останній), кожен — після запобіжника.
    batches = row["batches"]
    assert len(batches) >= 2
    assert all(b["trips"] == [] for b in batches)
    # M2: 32 живі повернулись із подією, 8 із 410 лишились знятими (без нової події).
    returned = [e for e in events(db) if e.kind == "returned"]
    assert len(returned) == 32 and {e.reason for e in returned} == {"onetime_reseen"}
    for tok, lid in reseen.items():
        n = int(tok[4:])
        assert get(db, lid).is_active is (n % 5 != 0), tok
    # DOM.RIA: 3 зняті за банером — з подією; решта живі.
    removed = [e for e in events(db) if e.kind == "removed"]
    assert len(removed) == 3 and {e.reason for e in removed} == {"ria_archive"}
    # Звірка запису ночі: актуальні після = до + повернуто − знято; події = підсумки.
    per = row["per_host"]
    assert sum(d["restored"] for d in per.values()) == 32
    assert sum(d["delisted"] for d in per.values()) == 3
    assert row["active_after"] == row["active_before"] + 32 - 3
    # D43: last_seen не пишеться ніким, крім збору.
    assert _last_seen_sum(db) == before_seen
    # Покоління кешу: «lists» — після кожного пакета, «analytics» — у кінці вікна.
    gens = _gens()
    assert gens["lists"] - gens0["lists"] >= len(batches)
    assert gens["analytics"] - gens0["analytics"] == 1
    assert calls["backup"] == 0                         # бекап свіжий — не потрібен
    # Рядок ops.liveness_runs (kind «night») зі зведенням для /status.
    with ops.ops_session() as s:
        lrun = s.get(ops.LivenessRun, res["liveness_run_id"])
        assert lrun.kind == "night" and lrun.status == "ok" and lrun.report
        assert lrun.returned == 32 and lrun.removed == 3


def test_second_window_does_not_ask_the_same_keys_again(db, tmp_path):
    """Відповідь «не визначено» (500) у вікні 01:10 — у вікні 04:10 тієї самої ночі ключ
    не питаємо вдруге, хоч графік повторів (3 год) уже минув."""
    for i in range(5):
        add(db, rieltor_url(6000000 + i), source="lun", external_id=f"s{i}")
    net = TimedNet(FakeNet(default=500))
    _run(db, tmp_path, net)
    first = len(net.log)
    assert first == 5
    _run(db, tmp_path, net, t0=local_epoch(2026, 10, 9, 4, 10))
    assert len(net.log) == first                       # жодного нового запиту
    _run(db, tmp_path, net, t0=local_epoch(2026, 10, 10, 1, 10))   # наступна ніч — так
    assert len(net.log) == 2 * first


def test_literal_fuse_holds_the_source_and_the_lane_stops_asking(db, tmp_path):
    """DOM.RIA: 30% «знято» в пакеті (literal, n ≥ 20) — запобіжник; нічого не знято,
    і після пакета смуга вже не питає ключів утриманого сайту."""
    ids = [add(db, ria_url(34600000 + i), source="domria", external_id=str(34600000 + i))
           for i in range(2000)]

    def ria(url, method):
        rid = int(url.rsplit("-", 1)[1].split(".")[0])
        return 200, ria_page(rid, archived=rid % 10 < 3)

    net = FakeNet({f"domria:{34600000 + i}": ria for i in range(2000)})
    res, timed, *_ = _run(db, tmp_path, net)
    assert res["status"] == "ok"
    assert [t["source"] for t in res["trips"]] and "domria" in {t["source"] for t in res["trips"]}
    from realty.liveness import fuse

    assert "domria" in fuse.held_sources()
    assert not [e for e in events(db) if e.kind == "removed"]
    assert all(get(db, lid).is_active for lid in ids[:50])
    row = _night_row(res)
    lane = row["lanes"]["dom.ria.com"]
    # Перший пакет — через 15 хв (~900 запитів по 1,0 с); далі смуга лише пропускала.
    assert lane["skipped_held"] > 0 and lane["requests"] < 1000
    assert lane["requests"] + lane["skipped_held"] + lane["not_reached"] == 2000


def test_literal_counts_already_removed_m2_rows(db, tmp_path):
    """M2 DOM.RIA: зняті рядки, жива перевірка — усі ще «знято» (індекс запізнюється,
    Етап 0: 0 із 10). literal — запобіжник (кожен перевірений рядок рахується, і вже
    зняті теж — D52); власник знімає його на /status. tiered — тест нижче."""
    for i in range(30):
        add(db, ria_url(34700000 + i), source="domria", external_id=str(34700000 + i),
            is_active=False, delisted_at=GONE, last_seen=datetime(2026, 9, 25))
    for i in range(30):
        add(db, ria_url(34710000 + i), source="domria", external_id=str(34710000 + i))

    def ria(url, method):
        rid = int(url.rsplit("-", 1)[1].split(".")[0])
        return 200, ria_page(rid, archived=rid < 34710000)

    resp = {f"domria:{34700000 + i}": ria for i in range(30)}
    resp.update({f"domria:{34710000 + i}": ria for i in range(30)})
    res, *_ = _run(db, tmp_path, FakeNet(resp), mode="literal")
    assert "domria" in {t["source"] for t in res["trips"]}
    assert [e for e in events(db) if e.kind in ("removed", "returned")] == []


def test_tiered_mode_lets_m2_through(db, tmp_path):
    for i in range(30):
        add(db, ria_url(34800000 + i), source="domria", external_id=str(34800000 + i),
            is_active=False, delisted_at=GONE, last_seen=datetime(2026, 9, 25))
    alive = [add(db, ria_url(34810000 + i), source="domria", external_id=str(34810000 + i),
                 is_active=False, delisted_at=GONE, last_seen=datetime(2026, 9, 25))
             for i in range(3)]

    def ria(url, method):
        rid = int(url.rsplit("-", 1)[1].split(".")[0])
        return 200, ria_page(rid, archived=rid < 34810000)

    resp = {f"domria:{34800000 + i}": ria for i in range(30)}
    resp.update({f"domria:{34810000 + i}": ria for i in range(3)})
    res, *_ = _run(db, tmp_path, FakeNet(resp), mode="tiered")
    assert res["trips"] == []
    assert all(get(db, lid).is_active for lid in alive)        # живі M2 повернулись
    assert len([e for e in events(db) if e.kind == "returned"]) == 3


def test_backup_failure_means_no_writes_tonight(db, tmp_path):
    lid = add(db, olx_url("10Bk001"), source="olx", external_id="b", is_active=False,
              delisted_at=GONE, last_seen=datetime(2026, 9, 25))
    net = TimedNet(FakeNet(default=200))
    res, net, _m, calls, *_ = _run(db, tmp_path, net, due=True,
                                   backup=lambda t: {"status": "failed", "message": "диск"})
    assert res["status"] == "backup_failed" and calls["backup"] == 1
    assert net.log == [] and get(db, lid).is_active is False and events(db) == []
    row = _night_row(res)
    assert row["backup"]["status"] == "failed" and row["lock_released_at"] is not None
    from realty.runner import CycleLock

    assert CycleLock(tmp_path / "cycle.lock").acquire()          # замок відпущено


def test_fresh_backup_is_taken_when_due_and_then_the_night_runs(db, tmp_path):
    lid = add(db, olx_url("10Bk002"), source="olx", external_id="b", is_active=False,
              delisted_at=GONE, last_seen=datetime(2026, 9, 25))
    res, _n, _m, calls, *_ = _run(db, tmp_path, FakeNet(default=200), due=True)
    assert calls["backup"] == 1 and res["status"] == "ok"
    assert get(db, lid).is_active is True


def test_waits_for_the_cycle_lock_and_gives_up_before_too_little_is_left(db, tmp_path):
    from realty.night.conductor import Conductor
    from realty.runner import CycleLock

    add(db, olx_url("10Lk001"), source="olx", external_id="l")
    cycle = CycleLock(tmp_path / "cycle.lock")
    assert cycle.acquire()                                       # цикл ще йде
    try:
        lcfg, ncfg = _cfgs()
        master = FakeClock(T0)
        net = TimedNet(FakeNet(default=200))
        env, calls = night_env(tmp_path, scope_of(db), master,
                               VirtualLauncher(master, net, lcfg), due=True)
        res = Conductor(ncfg=ncfg, lcfg=lcfg, env=env).run()
    finally:
        cycle.release()
    assert res["status"] == "lock_timeout"
    assert set(master.sleeps) == {ncfg.lock.poll_seconds}
    # Чекали, поки до stop_requests (02:47) лишалось ≥ 10 хв.
    assert master.time() <= local_epoch(2026, 10, 9, 2, 37)
    assert calls["backup"] == 0 and net.log == []


def test_a_hung_lane_is_killed_and_the_lock_released_before_the_cycle(db, tmp_path):
    for i in range(3):
        add(db, rieltor_url(6100000 + i), source="lun", external_id=f"h{i}")
    add(db, olx_url("10Hg001"), source="olx", external_id="o")
    res, net, master, *_ = _run(db, tmp_path, FakeNet(default=200), hang={"rieltor.ua"})
    assert res["status"] == "partial"
    row = _night_row(res)
    assert row["lanes"]["rieltor.ua"]["killed"] is True
    ncfg = configfiles.load("night")
    stop = local_epoch(2026, 10, 9, 2, 47)
    assert master.time() <= stop + ncfg.lanes.kill_grace_seconds + ncfg.lanes.poll_seconds
    assert row["lock_released_at"] <= row["release_lock_at"]
    assert res["late"] is False


def test_blocks_stop_the_lane_for_the_night_and_twice_in_a_row_wait_for_the_owner(db, tmp_path):
    for i in range(40):
        add(db, rieltor_url(6200000 + i), source="lun", external_id=f"x{i}")
    for i in range(10):
        add(db, ria_url(34960000 + i), source="domria", external_id=str(34960000 + i))
    ident = []

    def identity(source, stop_at, gate=None):
        ident.append(source)
        return {"done": {source: 0}}

    net = TimedNet(FakeNet(default=403))
    res, *_ = _run(db, tmp_path, net, identity_fn=identity)
    row = _night_row(res)
    assert res["status"] == "partial"
    assert row["lanes"]["rieltor.ua"]["stopped"] == "blocks"
    assert row["lanes"]["rieltor.ua"]["requests"] == 5         # run.max_consecutive_blocks
    assert row["lanes"]["dom.ria.com"]["stopped"] == "blocks"
    assert "domria" not in ident and "lun" in ident             # дозбір заблокованого — ні
    # Друге вікно тієї самої ночі — без смуги rieltor.
    n = len(net.log)
    res2, *_ = _run(db, tmp_path, net, t0=local_epoch(2026, 10, 9, 4, 10))
    plan = _night_row(res2)["plan"]["hosts"]["rieltor.ua"]
    assert "блокування цієї ночі" in (plan["skipped"] or "")
    assert len([1 for h, *_ in net.log[n:] if h == "rieltor.ua"]) == 0
    # Наступна ніч — знову блокування: хост чекає рішення власника.
    _run(db, tmp_path, net, t0=local_epoch(2026, 10, 10, 1, 10))
    with ops.ops_session() as s:
        hold = s.get(ops.NightHold, "rieltor.ua")
        assert hold is not None and hold.state == "held"
    res4, *_ = _run(db, tmp_path, net, t0=local_epoch(2026, 10, 11, 1, 10))
    assert "чекає рішення" in (_night_row(res4)["plan"]["hosts"]["rieltor.ua"]["skipped"] or "")
    from realty.night.conductor import unhold

    assert unhold("rieltor.ua", by="test") is True
    res5, *_ = _run(db, tmp_path, net, t0=local_epoch(2026, 10, 12, 1, 10))
    assert _night_row(res5)["plan"]["hosts"]["rieltor.ua"]["skipped"] is None


def test_identity_runs_last_in_its_host_lane(db, tmp_path):
    """Дозбір identity — у смузі свого хоста ПІСЛЯ перевірок (Блок 1 > дозбір), до
    stop_requests; окремого таймера й замка в нього більше немає."""
    add(db, ria_url(34900001), source="domria", external_id="34900001")
    seen = []
    timed = TimedNet(FakeNet({"domria:34900001": (200, ria_page(34900001))}))

    def identity(source, stop_at, gate=None):
        # Скільки запитів своєї смуги вже видано, коли почався дозбір.
        seen.append((source, stop_at, len([1 for h, *_ in timed.log if h == "dom.ria.com"])))
        return {"done": {source: 7}, "left": {source: 0}}

    res, timed, master, *_ = _run(db, tmp_path, timed, identity_fn=identity)
    assert sorted(s for s, *_ in seen) == ["domria", "flombu", "lun"]
    assert [n for s, _, n in seen if s == "domria"] == [1]      # після перевірки ключа
    assert all(stop == local_epoch(2026, 10, 9, 2, 47) for _, stop, _n in seen)
    row = _night_row(res)
    assert row["identity"]["dom.ria.com"]["report"]["done"] == {"domria": 7}


def test_disabled_and_outside_window(db, tmp_path):
    from realty.night.conductor import Conductor

    add(db, olx_url("10Off01"), source="olx", external_id="o")
    lcfg, ncfg = _cfgs()
    master = FakeClock(local_epoch(2026, 10, 9, 14, 0))
    net = TimedNet(FakeNet(default=200))
    env, _ = night_env(tmp_path, scope_of(db), master, VirtualLauncher(master, net, lcfg))
    assert Conductor(ncfg=ncfg, lcfg=lcfg, env=env).run()["status"] == "outside_window"
    (tmp_path / "COLLECTOR_OFF").write_text("базу перенесено")
    master.t = T0
    assert Conductor(ncfg=ncfg, lcfg=lcfg, env=env).run()["status"] == "disabled"
    assert net.log == []


def test_capture_fills_only_empty_evidence_from_the_same_response(db, tmp_path):
    """Докази Блоків 3/4 — з тіла тієї самої відповіді DOM.RIA (жодного зайвого запиту),
    лише туди, де порожньо."""
    lid = add(db, ria_url(34950001), source="domria", external_id="34950001",
              place_raw={"ria_district": "вже є"})
    page = ria_page(34950001, extra_realty={"district_name_uk": "Пасічна", "district_id": 7,
                                            "user_id": 42})
    res, timed, *_ = _run(db, tmp_path, FakeNet({"domria:34950001": (200, page)}))
    row = get(db, lid)
    assert row.place_raw["ria_district"] == "вже є"             # не переписано
    assert row.place_raw["ria_district_id"] == 7
    assert row.seller_profile == "ria:42"
    assert len([1 for h, *_ in timed.log if h == "dom.ria.com"]) == 1


def test_conductor_failure_stops_the_lanes_and_releases_the_lock(db, tmp_path, monkeypatch):
    """Диригент упав посеред ночі (тут — запис пакета): смуги зупинено, замок відпущено,
    запис ночі — «failed» (а не «running» назавжди)."""
    from realty.liveness import apply as lv_apply
    from realty.runner import CycleLock

    for i in range(400):
        add(db, rieltor_url(6300000 + i), source="lun", external_id=f"f{i}")

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(lv_apply, "apply_outcomes", boom)
    from realty.night.conductor import Conductor

    lcfg, ncfg = _cfgs()
    master = FakeClock(T0)
    launcher = VirtualLauncher(master, TimedNet(FakeNet(default=200)), lcfg)
    env, _ = night_env(tmp_path, scope_of(db), master, launcher)
    with pytest.raises(RuntimeError):
        Conductor(ncfg=ncfg, lcfg=lcfg, env=env).run()
    assert launcher.lanes["rieltor.ua"].killed                   # смугу зупинено
    with ops.ops_session() as s:
        row = s.scalars(select(ops.NightRun).order_by(ops.NightRun.id.desc())).first()
        assert row.status == "failed" and "database is locked" in row.message
        assert row.lock_released_at is not None
    assert CycleLock(tmp_path / "cycle.lock").acquire()
