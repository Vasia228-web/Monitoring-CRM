"""Інкрементальний збір, режими прогону та розклад."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty.config import SOURCES
from realty.sources.base import BaseSource
from realty.sources.domria import DomRiaSource
from realty.sources.lun import LunSource


class _Src(BaseSource):
    name = "domria"          # джерело з датним сортуванням

    def iter_listings(self):
        return iter(())


def test_full_mode_lifts_page_ceiling_and_slows_pace():
    for name, cfg in SOURCES.items():
        full = cfg.paced("full")
        assert full.max_pages >= cfg.max_pages, name
        assert full.delay >= cfg.delay, name
    assert SOURCES["domria"].paced("fresh").max_pages == SOURCES["domria"].max_pages


def test_early_stop_only_for_recency_ordered_sources():
    """LUN не має сортування за датою — сторінка без новинок там нічого не каже."""
    assert SOURCES["domria"].supports_recency
    assert SOURCES["olx"].supports_recency
    assert not SOURCES["lun"].supports_recency


def test_page_without_new_listings_stops_the_crawl():
    src = _Src(mode="fresh")
    src.begin_page(0)
    src._page_new = 3          # на сторінці були новинки
    src.end_page()
    assert not src.stop_requested

    src.begin_page(1)          # сторінка без жодного нового оголошення
    src.end_page()
    assert src.stop_requested


def test_full_mode_never_stops_early():
    src = _Src(mode="full")
    src.begin_page(0)
    src.end_page()
    assert not src.stop_requested


def test_known_ids_mark_listings_as_seen():
    src = _Src(mode="fresh", known_ids={"111"})
    assert "111" in src.known_ids and "222" not in src.known_ids


def test_checkpoint_callback_receives_page_number():
    seen = []
    src = _Src(mode="full", on_page=lambda name, page: seen.append((name, page)))
    src.begin_page(7)
    src.end_page()
    assert seen == [("domria", 7)]


def test_start_page_resumes_where_it_stopped():
    src = DomRiaSource(mode="full", start_page=120)
    assert src.start_page == 120 and src.current_page == 120


def test_olx_asks_for_newest_first():
    from realty.sources.olx import OlxSource

    url = OlxSource()._page_url(2)
    assert "created_at" in url and "desc" in url and "page=2" in url


# --- розклад ------------------------------------------------------------------

def test_plist_is_valid_and_points_at_the_runner():
    from realty import scheduler

    plist = scheduler.build_plist(7200)
    assert plist["StartInterval"] == 7200
    assert plist["Label"] == scheduler.LABEL
    assert plist["ProgramArguments"][0] == "/bin/bash"
    assert scheduler.RUNNER.exists(), "скрипт запуску відсутній"
    # Логи агента мають лежати поза проєктом — інакше при блокуванні TCC
    # причина збою нікуди не запишеться.
    assert "Library/Logs" in plist["StandardErrorPath"]


def test_tcc_warning_fires_for_protected_folders(monkeypatch):
    from realty import scheduler

    monkeypatch.setattr(scheduler, "ROOT", Path.home() / "Desktop" / "proj")
    assert "Desktop" in (scheduler.tcc_warning() or "")
    monkeypatch.setattr(scheduler, "ROOT", Path.home() / "work" / "proj")
    assert scheduler.tcc_warning() is None


def test_full_scrape_state_roundtrip(tmp_path, monkeypatch):
    import scripts.full_scrape as fs

    monkeypatch.setattr(fs, "STATE_FILE", tmp_path / "state.json")
    fs.save_state({"domria": {"page": 42}})
    assert fs.load_state()["domria"]["page"] == 42

    (tmp_path / "state.json").write_text("{зіпсовано", encoding="utf-8")
    assert fs.load_state() == {}     # пошкоджений стан не має валити прогін


def test_estimate_counts_remaining_pages_only():
    import scripts.full_scrape as fs

    fresh, _ = fs.estimate(["domria"], {})
    resumed, _ = fs.estimate(["domria"], {"domria": {"page": 400}})
    assert resumed < fresh


def test_one_bad_record_does_not_lose_the_batch(tmp_path, monkeypatch):
    """Регресія: помилка цілісності на одному записі відкочувала весь пакет.

    На повному прогоні через єдиний конфлікт посилань так зникло понад
    5 000 зібраних оголошень LUN.
    """
    import realty.db as db
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from realty.models import Base, Listing
    from realty.pipeline import Pipeline

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, future=True))

    good = [{"source": "t", "external_id": str(i), "original_url": f"u{i}",
             "price": 1.0, "currency": "USD"} for i in range(5)]
    bad = {"source": "t", "external_id": None, "original_url": None}   # NOT NULL

    # Контроль якості пропускає лише повні записи, тому даємо їм усі поля.
    for rec in good:
        rec.update({"rooms": 2, "location": "вул. Тестова, 1", "price_usd": 70000.0,
                    "area_total": 60.0, "price_per_sqm": 1166.0, "price": 70000})

    p = Pipeline(use_llm=False)
    p._write(good[:2] + [bad] + good[2:])

    with db.SessionLocal() as s:
        written = s.scalar(select(func.count()).select_from(Listing))
    assert written == 5, "добрі записи мали зберегтись попри поганий сусідній"
