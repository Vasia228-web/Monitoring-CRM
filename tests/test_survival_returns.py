"""Строк продажу після Блоку 1 (E8, D52): повернене — цензуроване, дата джерела — межа.

Рішення власника 2 (D46): повертати живих зі збереженням історії й показати, як
зміниться оцінка строку продажу. Повернене оголошення (delisted_at = NULL, попередня
дата — у події) — ще на ринку: спостереження цензуроване, а не «продано». Тихо зняте
DOM.RIA жило до дати зняття на джерелі (deleted_at), а не до нашого виявлення.
На коді до E8: повернень із подією не було, source_removed_at не існувало.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import FakeNet, add, clean_fuse, db, get, olx_url  # noqa: E402,F401

from realty import verify  # noqa: E402
from realty.analytics import segments  # noqa: E402
from realty.models import Property  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")
NOW = datetime(2026, 10, 8, 3, 0)


def _prop(Session, pid):
    with Session() as s:
        s.add(Property(id=pid, fingerprint=f"f{pid}", rooms=2, area_total=50.0,
                       price_usd_min=50_000.0, first_seen=NOW, last_seen=NOW))
        s.commit()


def _item(Session, pid):
    with Session() as s:
        return next(i for i in segments.build_items(s, [pid], now=NOW) if i.property_id == pid)


def test_returned_listing_is_censored_not_an_event(db, monkeypatch):
    """Крок циклу сам бере вибірку знятих (ярус rm_sample) і повертає живе з подією —
    на коді до E8 черга брала лише активні, знятих не перевіряв ніхто (0 повернень)."""
    from liveness_kit import events

    try:
        from realty.liveness import queue
        monkeypatch.setattr(queue, "_now", lambda: NOW)
    except ImportError:                    # код до E8: модуля немає, тест падає нижче
        pass
    _prop(db, 1)
    lid = add(db, olx_url("10Surv"), source="olx", external_id="s", property_id=1,
              is_active=False, delisted_at=NOW - timedelta(days=10),
              published_at=NOW - timedelta(days=60), first_seen=NOW - timedelta(days=50),
              last_alive_at=NOW - timedelta(days=12), last_seen=NOW - timedelta(days=11))
    days, event, _entry = _item(db, 1).observation
    assert event is True
    verify.verify_batch(http=FakeNet({"olx:10Surv": 200}))
    assert get(db, lid).delisted_at is None
    assert [e.kind for e in events(db, lid)] == ["returned"]
    days2, event2, _ = _item(db, 1).observation
    assert event2 is False, "повернене — ще на ринку (цензуроване)"
    assert days2 > days


def test_source_removal_date_bounds_the_lifetime(db):
    """DOM.RIA зняло 20 днів тому, наша стара перевірка HEAD «бачила живим» 5 днів тому
    (хибно): строк — до дати джерела, інтервал стискається до точки."""
    _prop(db, 2)
    pub = NOW - timedelta(days=90)
    add(db, "https://dom.ria.com/uk/realty-prodaja-kvartira-34500001.html", source="domria",
        external_id="34500001", property_id=2, is_active=False,
        delisted_at=NOW - timedelta(days=1), source_removed_at=NOW - timedelta(days=20),
        last_alive_at=NOW - timedelta(days=5), published_at=pub, first_seen=pub)
    item = _item(db, 2)
    assert item.delisted_at == NOW - timedelta(days=20)
    assert item.interval_days == 0
    assert round(item.lifetime_days) == 70


def test_source_date_before_our_first_sighting_is_not_used(db):
    """Копію LUN підхопили, коли DOM.RIA вже зняв оголошення (deleted_at на 6 год
    раніше за first_seen): дата джерела поза нашим спостереженням — лишаються наші
    дати, і строк довший за вхід у спостереження (подія під ризиком). На коді до
    виправлення строк ставав коротшим за вхід → (days == entry, подія) і Каплан—Меєр
    давав S = 0 (а двоє таких — S = −1)."""
    from realty.analytics.survival import Observation, kaplan_meier

    _prop(db, 3)
    pub = NOW - timedelta(days=60)
    seen = NOW - timedelta(days=30)
    add(db, "https://dom.ria.com/uk/realty-prodaja-kvartira-34500003.html", source="lun",
        external_id="L3", property_id=3, is_active=False, delisted_at=NOW - timedelta(days=2),
        source_removed_at=seen - timedelta(hours=6), published_at=pub, first_seen=seen)
    item = _item(db, 3)
    days, event, entry = item.observation
    assert event is True and days > entry, (days, entry)
    assert item.delisted_at == NOW - timedelta(days=2)
    curve = kaplan_meier([Observation(days=days, event=True, entry=entry),
                          Observation(days=days + 10, event=False, entry=0.0)])
    assert curve.points and min(p.survival for p in curve.points) > 0
