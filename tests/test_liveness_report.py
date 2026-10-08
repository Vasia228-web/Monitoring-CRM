"""Зведення /status Блоку 1: повернення — за ТИМ зняттям, яке вони скасували (рецензія E8, D52).

Показник помилки правила (рішення власника 1, D46) — частка знятих за банером DOM.RIA
чи повторним 404, що повернулись. На коді до виправлення причину повернення брали з
ОСТАННЬОГО зняття рядка взагалі: після «повернулось → знято знову (410)» попереднє
повернення ставало «до E8», і показник мовчав саме на тих посиланнях, що «блимають»;
а чисельник рахував повернення, чиє зняття старше за вікно, — частка бувала > 1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, add, at, clean_fuse, db, events, get, ria_page, ria_url,
)

from realty import verify  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")


def _at_time(monkeypatch, moment):
    from realty.liveness import queue

    monkeypatch.setattr(queue, "_now", lambda: moment)


def _report(db, now):
    from realty.liveness import policy as pol, report

    with db() as s:
        return report.status_block(s, pol.load(), now=now)


def _shares(rep):
    for block in ("rule_error", "reactivation_410"):
        for d in rep[block].values():
            yield d["share"]
    yield rep["repeat404_returns"]["share"]


def test_return_is_attributed_to_the_removal_it_undid(db, monkeypatch):
    """знято старим кодом → повернулось → знято (банер) → повернулось → знято (410):
    друге повернення — після банера (помилка правила 5 з 5), перше — «до E8»."""
    base = 35900000
    ids = [add(db, ria_url(base + i), source="domria", external_id=str(base + i),
               is_active=False, delisted_at=at(-120)) for i in range(5)]
    alive = FakeNet({f"domria:{base + i}": (200, ria_page(base + i)) for i in range(5)})
    archive = FakeNet({f"domria:{base + i}": (200, ria_page(base + i, archived=True))
                       for i in range(5)})
    gone = FakeNet({f"domria:{base + i}": 410 for i in range(5)})
    for hours, net in ((0, alive), (4, archive), (8, alive), (12, gone)):
        _at_time(monkeypatch, at(hours))
        verify.verify_batch(ids=ids, http=net)
    assert [(e.kind, e.reason) for e in events(db, ids[0])] == [
        ("returned", "sweep"), ("removed", "ria_archive"), ("returned", "sweep"),
        ("removed", "status_410")]
    rep = _report(db, at(13))
    assert rep["returns"]["domria"] == {"legacy": 5, "ria_archive": 5}
    assert rep["rule_error"]["domria"] == {"returned": 5, "removed": 5, "share": 1.0}
    assert rep["reactivation_410"]["domria"]["returned"] == 0
    assert all(x is None or x <= 1 for x in _shares(rep))


def test_share_counts_only_removals_inside_the_window(db, monkeypatch):
    """Повернення, чиє зняття старше за вікно (30 дн.), не потрапляє в чисельник:
    3 повернулись (зняті 40 днів тому) і 1 знятий у вікні — частка 0 з 1, а не 3 з 1."""
    base = 36100000
    old = [add(db, ria_url(base + i), source="domria", external_id=str(base + i))
           for i in range(3)]
    fresh = add(db, ria_url(base + 9), source="domria", external_id=str(base + 9))
    archive = FakeNet({f"domria:{base + i}": (200, ria_page(base + i, archived=True))
                       for i in (0, 1, 2, 9)})
    _at_time(monkeypatch, at(-40 * 24))
    verify.verify_batch(ids=old, http=archive)
    _at_time(monkeypatch, at(-1))
    verify.verify_batch(ids=[fresh], http=archive)
    _at_time(monkeypatch, at(0))
    verify.verify_batch(ids=old, http=FakeNet({f"domria:{base + i}": (200, ria_page(base + i))
                                                for i in range(3)}))
    assert all(get(db, i).is_active for i in old)
    rep = _report(db, at(1))
    assert rep["returns"]["domria"] == {"ria_archive": 3}
    assert rep["rule_error"]["domria"] == {"returned": 0, "removed": 1, "share": 0.0}
    assert all(x is None or x <= 1 for x in _shares(rep))
