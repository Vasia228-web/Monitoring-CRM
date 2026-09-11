"""Різниця списків: пошук кандидатів на зникнення й захист від збоїв збору."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from realty import snapshot
from realty.snapshot import MIN_SIZE, SHRINK_GUARD, Snapshot, compare


def _snap(source="domria", ids=None, n=None):
    if ids is None:
        ids = {str(i) for i in range(n or 100)}
    return Snapshot(source=source, taken_at=datetime(2026, 9, 11), ids=set(ids))


# --- головне обмеження --------------------------------------------------------

def test_disappearance_produces_candidates_not_verdicts():
    """Різниця списків нікого не знімає з продажу — вона лише називає кандидатів.

    Оголошення могло опуститись у ранжуванні або випасти через збій пагінації.
    Вирок виносить тільки поодинокий запит із явним 404.
    """
    before = _snap(ids={"1", "2", "3", "4"} | {str(i) for i in range(100, 130)})
    after = _snap(ids={"1", "3"} | {str(i) for i in range(100, 130)})
    report = compare(before, after)
    assert report.used is True
    assert report.candidates == ["2", "4"]
    # У звіті немає жодного поля, яке б означало «знято»: тільки кандидати.
    assert not hasattr(report, "delisted")


# --- захист від збою пагінації ------------------------------------------------

def test_a_collapsed_listing_is_treated_as_a_failure_not_a_mass_delisting():
    """Перелік, що впав більш ніж на чверть, — це збій збору, а не ринок.

    Без цього захисту одна невдала пагінація згенерувала б тисячі кандидатів
    і з'їла б добовий бюджет перевірок на порожньому місці.
    """
    before = _snap(n=1000)
    after = _snap(ids={str(i) for i in range(500)})
    report = compare(before, after)
    assert report.used is False
    assert "збій пагінації" in report.reason
    assert report.candidates == []


def test_a_shrink_within_the_guard_is_accepted():
    before = _snap(n=1000)
    after = _snap(ids={str(i) for i in range(int(1000 * SHRINK_GUARD) + 10)})
    report = compare(before, after)
    assert report.used is True
    assert len(report.candidates) == 1000 - (int(1000 * SHRINK_GUARD) + 10)


def test_an_empty_or_tiny_listing_is_never_used():
    assert compare(_snap(n=1000), _snap(ids=set())).used is False
    assert compare(_snap(n=1000), _snap(n=MIN_SIZE - 1)).used is False


def test_first_run_stores_a_baseline_without_accusing_anyone():
    report = compare(None, _snap(n=100))
    assert report.used is True
    assert report.candidates == []
    assert "перший перелік" in report.reason


def test_candidate_count_is_capped_per_run():
    """Дивна поведінка сайту не має перетворитись на лавину запитів до нього ж."""
    before = _snap(n=5000)
    after = _snap(ids={str(i) for i in range(4000)})
    report = compare(before, after)
    assert report.used is True
    assert len(report.candidates) == snapshot.MAX_CANDIDATES


def test_growth_produces_no_candidates():
    before = _snap(n=100)
    after = _snap(n=150)
    assert compare(before, after).candidates == []


# --- зберігання ---------------------------------------------------------------

def test_snapshot_survives_a_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "DIR", tmp_path)
    original = _snap(ids={"a", "b", "c"})
    snapshot.save(original)
    restored = snapshot.load("domria")
    assert restored.ids == original.ids
    assert restored.taken_at == original.taken_at


def test_missing_snapshot_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "DIR", tmp_path)
    assert snapshot.load("domria") is None


def test_corrupted_snapshot_does_not_crash_the_run(tmp_path, monkeypatch):
    """Побитий файл читається як «снапшота немає», а не валить прогін."""
    monkeypatch.setattr(snapshot, "DIR", tmp_path)
    (tmp_path / "domria.json").write_text("{це не json")
    assert snapshot.load("domria") is None


def test_broken_snapshot_is_not_saved_as_the_new_baseline(tmp_path, monkeypatch):
    """Зіпсований перелік не має ставати базою — інакше отруїть і наступне порівняння."""
    monkeypatch.setattr(snapshot, "DIR", tmp_path)
    good = _snap(n=1000)
    snapshot.save(good)
    broken = _snap(ids={"1", "2"})
    report = compare(snapshot.load("domria"), broken)
    assert report.used is False
    # `run` зберігає лише коли `used`; перевіряємо, що база лишилась цілою.
    assert snapshot.load("domria").size == 1000
    stored = json.loads((tmp_path / "domria.json").read_text())
    assert stored["count"] == 1000


def test_first_run_compares_against_what_the_database_believes(tmp_path, monkeypatch):
    """Найперший перелік не пропадає даремно: базою стає стан нашої бази.

    Інакше перше порівняння по кожному джерелу було б вхолосту — снапшот
    зберігся б, а користь з'явилась би тільки наступного разу.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from realty.models import Base, Listing

    engine = create_engine(f"sqlite:///{tmp_path/'b.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    from contextlib import contextmanager

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(snapshot, "session_scope", scope)
    with Session() as s:
        for i in range(5):
            s.add(Listing(source="domria", external_id=str(i),
                          original_url=f"https://x/{i}", is_active=(i != 4)))
        s.commit()

    base = snapshot.baseline_from_db("domria")
    assert base.ids == {"0", "1", "2", "3"}     # знятого в базі вже немає


def test_sources_without_full_enumeration_are_skipped_explicitly():
    """OLX обриває видачу на 25-й сторінці — неповний перелік гірший за жоден.

    Кожен прогін по ньому давав би тисячі «кандидатів» з недосяжних сторінок
    і витрачав би на них усі поодинокі перевірки.
    """
    assert "olx" not in snapshot.ENUMERABLE
    assert "blago" not in snapshot.ENUMERABLE
    assert snapshot.ENUMERABLE == {"domria", "lun", "flombu"}
    assert "25 сторінками" in snapshot.NOT_ENUMERABLE_REASON["olx"]
