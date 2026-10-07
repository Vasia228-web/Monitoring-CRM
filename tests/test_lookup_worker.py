"""Процес перевірки при відкритті квартири: один за раз, поступається циклу збору,
поважає COLLECTOR_OFF, скидає кеші сайту лише за зміною видимого (Блок 2, E5, D50).

Виправлення після рецензії Блоку 2. Що було на попередньому коді:
  * 8 різних квартир, відкритих підряд, давали 8 одночасних процесів
    `cli.py lookup check` (~80 МБ кожен на машині з 3,7 ГБ) і паралельні запити
    до тих самих хостів з незалежними паузами;
  * завдання йшло поруч із кроком перевірки циклу (подвійний темп на хости), а
    під 40-секундним блокуванням кроку «дублі» робило запити, чекало 31 с і
    закінчувалось «failed: database is locked» — результат губився;
  * кожна перевірка, що лише позначила спробу (last_attempt, check_events),
    збільшувала покоління «lists» при виході процесу — скидала всі кеші списків;
  * зняте оголошення лишалось «ще в продажу» в знімку «Аналітики» до кінця циклу.

Швидкість і паралельність перевіряються лічильниками (запусків, запитів,
поколінь), а не секундоміром.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from realty import runner  # noqa: E402
from test_web_speed import _mod, _run_jobs, site  # noqa: E402,F401 — фікстура сайту


class Fetcher:
    """Замість HTTP: код за частиною адреси (типово 200). Рахує запити."""

    codes: dict[str, int] = {}
    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    def probe(self, url, delay=0.0):
        Fetcher.calls.append(url)
        for part, code in Fetcher.codes.items():
            if part in url:
                return code
        return 200

    def close(self):
        pass


@pytest.fixture
def worker(site, monkeypatch, tmp_path):
    """Сайт + процес перевірки на тій самій синтетичній базі; запуск процесу записується."""
    c, Session, engine = site
    verify = _mod("realty.verify")
    import realty.db as dbmod

    Fetcher.calls, Fetcher.codes = [], {}
    monkeypatch.setattr(verify, "Fetcher", Fetcher)

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(verify, "session_scope", scope)
    livecheck = _mod("realty.web.livecheck")
    opened = _mod("realty.lookup.opened")
    launched: list[int] = []
    monkeypatch.setattr(livecheck, "launch", lambda job: launched.append(job) or "test")
    # Свої замки й прапорець — щоб тест, який тримає замок, не заважав іншим.
    monkeypatch.setattr(opened, "CYCLE_LOCK", tmp_path / "cycle.lock")
    monkeypatch.setattr(opened, "DRAIN_LOCK", tmp_path / "lookup.lock")
    monkeypatch.setattr(opened, "DISABLED_FLAG", tmp_path / "COLLECTOR_OFF")
    return SimpleNamespace(client=c, Session=Session, engine=engine, launched=launched,
                           livecheck=livecheck, opened=opened,
                           queue=_mod("realty.lookup.queue"))


def _open(w, pid: int) -> None:
    assert w.client.get(f"/property/{pid}").status_code == 200
    assert w.livecheck.LIVE.join(10)


def _job_of(w, pid: int):
    job = w.queue.latest_for(w.queue.opened_key(pid), since_s=3600)
    assert job is not None, w.livecheck.LIVE.status(pid)
    return job


# --- Один процес за раз ------------------------------------------------------------------------


def test_eight_opens_start_one_worker_that_drains_them_all(worker):
    w = worker
    for pid in range(2, 10):
        _open(w, pid)
    jobs = [_job_of(w, pid).id for pid in range(2, 10)]
    assert len(w.launched) == 1, "кожне відкриття запускало свій процес"
    assert {w.queue.get(j).state for j in jobs} == {"queued"}
    done = _run_jobs(w.launched[0])                   # той самий процес «доїдає» чергу
    assert sorted(j for j, _ in done) == sorted(jobs)
    assert {state for _, state in done} == {"done"}
    assert len(Fetcher.calls) == 8                    # по одному оголошенню на квартиру


def test_a_second_worker_leaves_the_queue_to_the_running_one(worker):
    w = worker
    drainer = runner.CycleLock(w.opened.DRAIN_LOCK)   # «процес перевірки вже працює»
    assert drainer.acquire()
    try:
        _open(w, 3)
        assert w.launched == []                       # працює — нового не запускаємо
        job = _job_of(w, 3).id
        assert _run_jobs(job) == [(job, "drainer-active")]
        assert w.queue.get(job).state == "queued" and Fetcher.calls == []
    finally:
        drainer.release()
    assert _run_jobs(job) == [(job, "done")]


def test_jobs_left_by_a_worker_out_of_budget_get_a_new_worker(worker):
    w = worker
    _open(w, 2)
    _open(w, 3)
    first, second = _job_of(w, 2).id, _job_of(w, 3).id
    assert w.launched == [first]
    opened = w.opened
    assert opened.run(first, budget_s=0, timeout_s=180) == [(first, "done")]   # бюджет вичерпано
    assert w.queue.get(second).state == "queued"
    w.livecheck.LIVE.tick()                           # фоновий огляд черги сайту
    assert w.launched == [first, second]


def test_after_the_worker_has_finished_the_next_open_starts_one_at_once(worker):
    """Пауза на старт процесу (launch_grace_s) не затримує наступного: попередній
    уже взяв своє завдання й вийшов."""
    w = worker
    _open(w, 2)
    assert _run_jobs(w.launched[0])[0][1] == "done"
    _open(w, 3)
    assert w.launched == [_job_of(w, 2).id, _job_of(w, 3).id]


# --- Цикл збору й COLLECTOR_OFF ------------------------------------------------------------------


def test_open_check_waits_for_the_cycle_without_a_single_request(worker):
    w = worker
    cycle = runner.CycleLock(w.opened.CYCLE_LOCK)
    assert cycle.acquire()
    try:
        _open(w, 1)
        assert w.launched == [], "під час циклу процес перевірки не запускається"
        job = _job_of(w, 1)
        assert job.state == "deferred"
        assert w.client.get("/api/property/1/liveness?wait=5").json()["state"] == "deferred"
        # Процес, що стартував попри це (Блок 5, ручний запуск), теж не йде в мережу.
        assert _run_jobs(job.id) == [(job.id, "deferred")]
        assert Fetcher.calls == []
        # Завдання, поставлене ДО циклу (queued), процес лише відкладає.
        early = w.queue.enqueue("opened", w.queue.opened_key(5), 5)
        assert _run_jobs(early) == [(early, "deferred")]
        assert w.queue.get(early).state == "deferred" and Fetcher.calls == []
    finally:
        cycle.release()
    w.livecheck.LIVE.tick()                           # цикл скінчився — огляд черги
    assert w.launched == [job.id]
    assert sorted(_run_jobs(job.id)) == sorted([(job.id, "done"), (early, "done")])
    assert len(Fetcher.calls) == 4                    # 3 оголошення квартири 1 + одне квартири 5
    assert w.client.get("/api/property/1/liveness").json()["state"] == "done"


def test_jobs_left_from_before_a_restart_run_after_the_cycle(worker):
    """Сайт перезапустили посеред циклу: відкладене раніше завдання не губиться —
    перший оберт бере його під нагляд, процес стартує, щойно цикл скінчиться."""
    w = worker
    job = w.queue.enqueue("opened", w.queue.opened_key(7), 7, state="deferred")
    fresh = w.livecheck.LiveCheck()                   # «новий процес сайту»
    cycle = runner.CycleLock(w.opened.CYCLE_LOCK)
    assert cycle.acquire()
    try:
        fresh.tick()
        assert w.launched == [] and job in fresh._watch
        fresh.tick()                                  # цикл ще йде — так само
        assert w.launched == []
    finally:
        cycle.release()
    fresh.tick()
    assert w.launched == [job]
    assert _run_jobs(job) == [(job, "done")]
    fresh.tick()
    assert job not in fresh._watch                    # завершене — з-під нагляду


def test_collector_off_means_no_open_checks(worker):
    w = worker
    w.opened.DISABLED_FLAG.write_text("базу перенесено")
    job = w.queue.enqueue("opened", w.queue.opened_key(1), 1)
    assert _run_jobs(job) == [(job, "skipped")]
    assert w.queue.get(job).message == "COLLECTOR_OFF" and Fetcher.calls == []
    _open(w, 5)                                       # і сайт нових завдань не ставить
    assert w.launched == []
    assert w.client.get("/api/property/5/liveness").json()["state"] == "skipped"


def test_a_worker_stopped_by_systemd_closes_its_job(worker, monkeypatch):
    """SIGTERM (TimeoutStartSec) приходить як SystemExit (cli.py): завдання —
    «failed», а не вічне «running», і замок черги вільний."""
    w = worker

    def killed(**kw):
        raise SystemExit(143)

    monkeypatch.setattr("realty.verify.verify_batch", killed)
    _open(w, 6)
    job = w.launched[0]
    with pytest.raises(SystemExit):
        _run_jobs(job)
    assert w.queue.get(job).state == "failed"
    assert w.opened.drainer_busy() is None


# --- Кеші сайту: лише за зміною видимого -----------------------------------------------------------


def test_lists_generation_moves_only_when_a_check_changes_visibility(worker, monkeypatch):
    w = worker
    txnwatch, webcache = _mod("realty.txnwatch"), _mod("realty.webcache")
    import realty.db as dbmod

    monkeypatch.setattr(dbmod, "engine", w.engine)    # AutoBump рахує записи саме цієї бази
    monkeypatch.setattr(txnwatch, "_autobump", txnwatch.AutoBump("lookup check"))
    gen0 = webcache.read()["lists"]
    # Усе живе (200): лише службові записи — last_attempt, check_events.
    _open(w, 4)
    job = w.launched[-1]
    assert _run_jobs(job) == [(job, "done")]
    txnwatch._exit_bump()                             # вихід процесу
    assert webcache.read()["lists"] == gen0, "службові записи скидали кеші списків"
    # 410 — знято: покоління одразу після завдання (процес можуть убити до виходу).
    Fetcher.codes = {"x-15.html": 410}
    _open(w, 5)
    job = w.launched[-1]
    assert _run_jobs(job) == [(job, "done")]
    assert webcache.read()["lists"] == gen0 + 1
    txnwatch._exit_bump()
    assert webcache.read()["lists"] == gen0 + 1       # і не вдруге при виході


def test_a_delisting_found_on_open_updates_the_flat_in_the_analytics_snapshot(worker):
    w = worker
    cache, segments = _mod("realty.analytics.cache"), _mod("realty.analytics.segments")
    assert w.client.get("/property/5?verify=0").status_code == 200    # знімок є
    before = vars(cache.HOLDER.current().universe.by_id[5])
    Fetcher.codes = {"x-15.html": 410}
    _open(w, 5)
    assert _run_jobs(w.launched[-1])[0][1] == "done"
    assert w.client.get("/api/property/5/liveness").json()["delisted"] == 1
    snap = cache.HOLDER.current()
    with w.Session() as s:
        fresh = segments.build_items(s, [5], now=snap.universe.built_at)
    assert vars(fresh[0]) != before                   # зміна справді видима (квартиру знято)
    assert vars(snap.universe.by_id[5]) == vars(fresh[0])


# --- Дрібне --------------------------------------------------------------------------------------


def test_lock_busy_reads_proc_locks_and_never_takes_the_lock(tmp_path):
    path = tmp_path / "cycle.lock"
    path.write_text("")
    ino = path.stat().st_ino
    proc = tmp_path / "locks"
    proc.write_text(f"1: POSIX  ADVISORY  WRITE 11 00:2b:{ino} 0 EOF\n"
                    f"2: -> FLOCK  ADVISORY  WRITE 12 00:2b:{ino} 0 EOF\n"
                    f"3: FLOCK  ADVISORY  WRITE 14 00:2b:{ino + 1} 0 EOF\n")
    assert runner.lock_busy(path, proc_locks=proc) is None     # не FLOCK, очікувач, інший файл
    proc.write_text(proc.read_text() + f"4: FLOCK  ADVISORY  WRITE 13 fd:00:{ino} 0 EOF\n")
    assert runner.lock_busy(path, proc_locks=proc)["lock_pid"] == 13
    lock = runner.CycleLock(path)                     # перевірка замка не брала
    assert lock.acquire()
    lock.release()
    # Без /proc/locks (Mac): за вмістом файла, який стирається при звільненні.
    missing = tmp_path / "no-proc-locks"
    assert lock.acquire()
    try:
        assert runner.lock_busy(path, proc_locks=missing)["pid"] == os.getpid()
    finally:
        lock.release()
    assert runner.lock_busy(path, proc_locks=missing) is None
    assert runner.lock_busy(tmp_path / "never-created.lock", proc_locks=missing) is None


def test_fallback_worker_yields_to_the_site(monkeypatch):
    livecheck = _mod("realty.web.livecheck")
    monkeypatch.setattr(livecheck.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert livecheck._low_priority() == ["nice", "-n", "10", "ionice", "-c2", "-n7"]
    monkeypatch.setattr(livecheck.shutil, "which", lambda name: None)
    assert livecheck._low_priority() == []


def test_dispatcher_memory_is_bounded(worker):
    live = worker.livecheck.LIVE
    for job in range(worker.livecheck.NOTIFIED_MAX + 50):
        live._changed(job, None)
    assert len(live._notified) == worker.livecheck.NOTIFIED_MAX
    live._jobs[999] = (None, "skipped", time.monotonic() - 10_000)
    live._prune()
    assert 999 not in live._jobs
