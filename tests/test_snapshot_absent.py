"""Різниця списків ставить absent_since, а не підтверджує кандидата раз (E8, D52).

На коді до E8: зниклий кандидат перевірявся одним запитом і більше ніколи (1 236
DOM.RIA після одного 200, Етап 0); неповний перелік DOM.RIA (FetchError посеред
пагінації) мовчки порівнювався, якщо був ≥75% попереднього; рядки LUN, що ведуть на
DOM.RIA, перелік DOM.RIA не бачив.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import add, clean_fuse, db, get, ria_url  # noqa: E402,F401

from realty import snapshot  # noqa: E402
from realty.fetcher import FetchError  # noqa: E402
from realty.sources import REGISTRY  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")


def _fake_domria(monkeypatch, pages, fail_at=None):
    cls = REGISTRY["domria"]

    def search(self, page, limit=None):
        if fail_at is not None and page == fail_at:
            raise FetchError("штучний збій сторінки")
        return pages[page] if page < len(pages) else []

    monkeypatch.setattr(cls, "_search_page", search)


def _ids(lo, hi):
    return [str(i) for i in range(lo, hi)]


def test_ria_key_absence_marks_rows_of_every_source_and_clears_on_return(db, monkeypatch):
    base = 36000000
    domria = add(db, ria_url(base), source="domria", external_id=str(base))
    lun = add(db, ria_url(base, "realty-prodaja-kvartira-ivano-frankovsk-lun"),
              source="lun", external_id="L1")
    present = add(db, ria_url(base + 1), source="domria", external_id=str(base + 1))
    others = _ids(base + 1, base + 60)
    _fake_domria(monkeypatch, [others[:30], others[30:]])
    rep = snapshot.run(["domria"], force=True)
    assert rep["sources"]["domria"]["newly_absent"] == 2
    assert get(db, domria).absent_since is not None
    assert get(db, lun).absent_since is not None, "рядок LUN на DOM.RIA — за ключем domria:"
    assert get(db, present).absent_since is None
    assert get(db, domria).is_active and get(db, lun).is_active, "відсутність не знімає"
    # Повернулось у перелік — позначка знімається.
    _fake_domria(monkeypatch, [[str(base)] + others[:29], others[29:]])
    rep = snapshot.run(["domria"], force=True)
    assert rep["sources"]["domria"]["back_in_list"] == 2
    assert get(db, domria).absent_since is None and get(db, lun).absent_since is None


def test_incomplete_enumeration_marks_nobody_and_is_not_saved(db, monkeypatch):
    base = 36100000
    lid = add(db, ria_url(base + 59), source="domria", external_id=str(base + 59))
    pages = [_ids(base + 30 * k, base + 30 * (k + 1)) for k in range(2)]
    _fake_domria(monkeypatch, pages, fail_at=1)                  # обрив на другій сторінці
    rep = snapshot.run(["domria"], force=True)
    entry = rep["sources"]["domria"]
    assert entry["complete"] is False
    assert snapshot.load("domria") is None, "неповний перелік не стає базою"
    assert get(db, lid).absent_since is None


def test_saved_snapshot_records_completeness(db, monkeypatch):
    base = 36200000
    _fake_domria(monkeypatch, [_ids(base, base + 40)])
    snapshot.run(["domria"], force=True)
    snap = snapshot.load("domria")
    assert snap is not None and snap.complete is True and snap.size == 40


def test_new_absent_marks_are_capped_per_run(db, monkeypatch):
    base = 36300000
    monkeypatch.setattr(snapshot, "MAX_CANDIDATES", 3)
    lids = [add(db, ria_url(base + i), source="domria", external_id=str(base + i))
            for i in range(5)]
    _fake_domria(monkeypatch, [_ids(base + 100, base + 150)])
    rep = snapshot.run(["domria"], force=True)
    assert rep["sources"]["domria"]["newly_absent"] == 3
    assert sum(get(db, i).absent_since is not None for i in lids) == 3


# --- Рецензія E8 (D52): перелік, обрізаний стелею сторінок, — не повний ------------------------


class _Pages:
    """Фальшива мережа для get/get_json (без мережі): лічить запити, віддає `answer(n)`."""

    def __init__(self, answer):
        self.answer = answer
        self.n = 0

    def get(self, url, *a, **k):
        self.n += 1
        return self.answer(self.n)

    def get_json(self, url, params=None, *a, **k):
        self.n += 1
        return self.answer(self.n)

    def close(self):
        pass


def _fake_lun(monkeypatch):
    from realty.sources import lun

    monkeypatch.setattr(lun, "extract_payload", lambda html: html or None)
    monkeypatch.setattr(lun, "resolve_text_rows", lambda payload: {})
    monkeypatch.setattr(lun, "iter_json_objects",
                        lambda payload: [{"price": 1, "urlRaw": "u", "id": payload}])
    monkeypatch.setattr(lun.LunSource, "_parse",
                        lambda self, d, rows=None: {"external_id": d["id"]})


def test_lun_enumeration_cut_by_the_page_cap_is_not_complete(monkeypatch):
    """Стеля full_pages, а сторінки ще повні — далі ми не дивились: не повний (на коді
    до виправлення — complete=True, і rieltor/lun.ua «немає в переліку» знімало)."""
    from dataclasses import replace

    from realty import config

    monkeypatch.setitem(config.SOURCES, "lun", replace(config.SOURCES["lun"], full_pages=3))
    _fake_lun(monkeypatch)
    net = _Pages(lambda n: f"PAGE{n}")
    snap = snapshot.capture("lun", fetcher=net)
    assert net.n == 3 and snap.complete is False


def test_lun_empty_payload_midway_is_not_complete(monkeypatch):
    _fake_lun(monkeypatch)
    net = _Pages(lambda n: "" if n == 2 else f"PAGE{n}")
    snap = snapshot.capture("lun", fetcher=net)
    assert snap.complete is False


def test_flombu_cap_below_the_sites_page_count_is_not_complete(monkeypatch):
    from dataclasses import replace

    from realty import config
    from realty.sources import flombu

    monkeypatch.setitem(config.SOURCES, "flombu",
                        replace(config.SOURCES["flombu"], full_pages=2))
    monkeypatch.setattr(flombu.FlombuSource, "_parse",
                        lambda self, item, geo: {"external_id": item["id"]})
    net = _Pages(lambda n: {"data": [{"id": f"f{n}"}], "meta": {"pages": 5}})
    assert snapshot.capture("flombu", fetcher=net).complete is False
    # Сайт сам каже «2 сторінки» — та сама стеля, перелік повний.
    net = _Pages(lambda n: {"data": [{"id": f"f{n}"}] if n <= 2 else [], "meta": {"pages": 2}})
    assert snapshot.capture("flombu", fetcher=net).complete is True


def test_domria_guard_and_loop_after_a_full_page_are_not_complete(monkeypatch):
    """Запобіжник 200 сторінок чи сторінка, що повторює бачене після ПОВНОЇ, — кінця
    переліку ми не бачили. Повтор після неповної сторінки — кінець (повний)."""
    cls = REGISTRY["domria"]
    size = cls.ID_PAGE_SIZE
    monkeypatch.setattr(cls, "_search_page",
                        lambda self, page, limit=None: list(range(page * size, (page + 1) * size)))
    assert snapshot.capture("domria").complete is False
    monkeypatch.setattr(cls, "_search_page",
                        lambda self, page, limit=None: list(range(min(page, 1) * size,
                                                                  (min(page, 1) + 1) * size)))
    assert snapshot.capture("domria").complete is False
    monkeypatch.setattr(cls, "_search_page",
                        lambda self, page, limit=None: list(range(size)) if page == 0
                        else list(range(size, size + 7)))
    assert snapshot.capture("domria").complete is True


def test_watchdog_alerts_when_there_is_no_complete_snapshot_since_deploy(db):
    """Файл переліку до E8 (без `complete`) і нові переліки, що обриваються: повного
    переліку немає — тривога після snapshot_stale_hours від розгортання (першого
    прогону перевірки). На коді до виправлення такий файл мовчки пропускався."""
    import json
    from datetime import datetime, timedelta

    from realty import ops, watchdog

    snapshot.DIR.mkdir(parents=True, exist_ok=True)
    taken = datetime(2026, 9, 19, 16, 46)
    snapshot.path_for("domria").write_text(json.dumps(
        {"source": "domria", "taken_at": taken.isoformat(), "count": 2, "requests": 1,
         "ids": ["1", "2"]}), encoding="utf-8")
    assert snapshot.load("domria").complete is False
    ops.init_ops()
    with ops.ops_session() as s:
        run = ops.LivenessRun(kind="manual", status="ok", started_at=ops._now())
        s.add(run)
        s.flush()
        run_id = run.id
    try:
        later = ops._now() + timedelta(hours=10)
        keys = {a.key: a.text for a in watchdog.check_liveness(later)}
        assert "liveness-snapshot-stale:domria" in keys, keys
        assert "немає повного переліку" in keys["liveness-snapshot-stale:domria"]
    finally:
        with ops.ops_session() as s:
            s.delete(s.get(ops.LivenessRun, run_id))
