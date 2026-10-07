"""Разовий прохід телефонів у наявних рядках: scan / impact / apply (кроки E6–E7, D51).

На тимчасовій базі з «давніми» рядками (номери записано в обхід слухачів — як
у робочій базі до E6). Номери синтетичні.

  * scan і impact — лише читання (файл бази байт у байт той самий), у звіті
    немає абонентських цифр;
  * impact бачить, коли заміна змінила б похідне (номер після коми як «номер
    будинку»), і дає 0 на звичайних описах;
  * apply: без --yes — відмова; без свіжого бекапу — відмова; під чужим замком
    циклу — відмова; понад apply.max_rows — стоп; інакше пакетами ≤200 рядків,
    змінює рівно стільки, скільки показав scan, інших колонок не чіпає, після —
    0 номерів; повтор нічого не змінює.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import backup, links_index, ops, privacy, privacy_pass, runner  # noqa: E402
from realty.models import Base  # noqa: E402

PH = "[телефон]"
N_PHONE = 450                       # > 2 пакетів по 200


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def legacy(tmp_path):
    """Рядки, записані до E6: номери в описах (сирим SQL, без слухачів)."""
    path = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(eng)
    rows = []
    for i in range(1, 601):
        if i <= N_PHONE:
            desc = [f"Квартира {i}. Тел. 067 000 {i % 100:02d} 01, торг.",
                    f"Ціна 65 000 $. viber://chat?number=%2B3805000{i:05d}",
                    f"Від власника, 0X******XX, огляд 12.10.2026 о 10:00"][i % 3]
        else:
            desc = f"Квартира {i}: 45,3 м², 5/9 поверх, 1 250 000 грн, кадастр 2610100000:01:002:0123"
        src, url = [("olx", f"https://www.olx.ua/d/uk/obyavlenie/x-IDt{i:05d}.html"),
                    ("lun", f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{12000000 + i}/"),
                    ("domria", f"https://dom.ria.com/uk/realty-prodaja-kvartira-x-{30000000 + i}.html")][i % 3]
        rows.append({"id": i, "source": src, "external_id": str(i), "original_url": url,
                     "description": desc, "title": "2-к квартира", "is_active": i % 2,
                     "price_usd": 50000.0 + i})
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO listings (id, source, external_id, original_url, description, title, "
            "is_active, price_usd, currency, rooms, market_type, condition, views, check_failures, "
            "in_progress, quality_status, price_estimated, detail_enriched, llm_extracted, "
            "first_seen, last_seen) VALUES (:id, :source, :external_id, :original_url, "
            ":description, :title, :is_active, :price_usd, 'USD', 2, 'UNKNOWN', 'UNKNOWN', 0, 0, "
            "0, 'ok', 0, 0, 0, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"), rows)
    eng.dispose()
    return path


@pytest.fixture
def lock(tmp_path, monkeypatch):
    path = tmp_path / "cycle.lock"
    monkeypatch.setattr(runner, "LOCK_PATH", path)
    return path


def test_scan_is_read_only_and_prints_no_subscriber_digits(legacy):
    sha = _sha(legacy)
    rep = privacy_pass.scan(db=str(legacy))
    assert _sha(legacy) == sha
    assert rep["rows_scanned"] == 600 and rep["rows_would_change"] == N_PHONE
    assert rep["active_rows_would_change"] == sum(1 for i in range(1, N_PHONE + 1) if i % 2)
    assert rep["by_kind"] == {"number": 150, "link:viber": 150, "mask": 150}
    assert set(rep["per_group"]) == {"olx", "lun>rieltor", "domria"}
    assert sum(g.get("description_rows", 0) for g in rep["per_group"].values()) == N_PHONE
    out = privacy_pass.render_scan(rep)
    for pattern, _n in rep["patterns"]:
        assert re.sub(r"\D", "", pattern) in ("", "0", "80", "380"), pattern
    # Жодної абонентської частини номерів у звіті (лише префікси й лічильники).
    assert not re.search(r"\b000 \d\d 01\b|3805000\d{5}", out)
    small = privacy_pass.scan(db=str(legacy), limit=30)
    assert small["rows_scanned"] == 30 and small["rows_would_change"] == 30
    # Контрольна точка E6: лише нові оголошення (first_seen ≥ дати розгортання).
    eng = create_engine(f"sqlite:///{legacy}", future=True)
    with eng.begin() as conn:
        conn.execute(text("UPDATE listings SET first_seen = '2026-10-09 02:00:00' WHERE id IN (1, 500)"))
    eng.dispose()
    new = privacy_pass.scan(db=str(legacy), since="2026-10-09")
    assert new["rows_scanned"] == 2 and new["rows_would_change"] == 1


@pytest.mark.parametrize("since,scanned", [
    ("2026-10-06", 2), ("2026-10-06 00:00", 2), ("2026-10-06T00:00", 2),
    (" 2026-10-06T00:00:00 ", 2), ("2026-10-06T03:00+03:00", 2), ("2026-10-06T00:00Z", 2),
    ("2026-10-06T05:00", 2), ("2026-10-06T05:00:01", 1), ("2026-10-07", 0),
])
def test_scan_since_accepts_any_iso_spelling(legacy, since, scanned):
    """Рев'ю E6 (D51): «2026-10-06T00:00» порівнювався як текст і відкидав увесь
    день (first_seen у базі — '2026-10-06 05:00:00.…'); контрольна точка E6 дала
    б хибний 0."""
    eng = create_engine(f"sqlite:///{legacy}", future=True)
    with eng.begin() as conn:
        conn.execute(text("UPDATE listings SET first_seen = '2026-10-06 05:00:00' WHERE id = 1"))
        conn.execute(text("UPDATE listings SET first_seen = '2026-10-06 23:59:59.500000' "
                          "WHERE id = 2"))
    eng.dispose()
    assert privacy_pass.scan(db=str(legacy), since=since)["rows_scanned"] == scanned


def test_scan_since_refuses_a_non_date(legacy):
    with pytest.raises(ValueError, match="не дата"):
        privacy_pass.scan(db=str(legacy), since="вчора")


def test_impact_is_read_only_and_finds_no_change_on_ordinary_text(legacy):
    sha = _sha(legacy)
    rep = privacy_pass.impact(db=str(legacy), force_dedup=True)
    assert _sha(legacy) == sha
    assert rep["rows_would_change"] == N_PHONE
    assert rep["differences"] == {"classify_condition": 0, "classify_market": 0,
                                  "street_from_text": 0, "quality_validate": 0}
    assert rep["dedup"]["shapes_differ"] == 0
    assert rep["dedup"]["all"]["listings_in_changed_groups"] == 0
    assert "НЕЗАЛЕЖНІСТЬ ПІДТВЕРДЖЕНО" in privacy_pass.render_impact(rep)


def test_impact_checks_the_classifier_calls_production_makes(legacy, monkeypatch):
    """Рев'ю E6 (D51): housekeeping.reclassify кличе classify_market(назва, опис,
    назва ЖК, built_year=…) і classify_condition(…, market=…) — impact звіряє й ці
    форми, а не лише прості."""
    from realty import normalize

    calls = {"market3": 0, "condition_market": 0}
    real_m, real_c = normalize.classify_market, normalize.classify_condition

    def market(*parts, **kw):
        calls["market3"] += len(parts) == 3 and "built_year" in kw
        return real_m(*parts, **kw)

    def condition(*parts, **kw):
        calls["condition_market"] += kw.get("market") is not None
        return real_c(*parts, **kw)

    monkeypatch.setattr(normalize, "classify_market", market)
    monkeypatch.setattr(normalize, "classify_condition", condition)
    rep = privacy_pass.impact(db=str(legacy), dedup=False)
    assert calls["market3"] >= 2 * N_PHONE and calls["condition_market"] >= 2 * N_PHONE
    assert rep["differences"]["classify_market"] == rep["differences"]["classify_condition"] == 0
    # А якщо класифікатор у формі production бачить номер — розбіжність є.
    monkeypatch.setattr(normalize, "classify_market",
                        lambda *p, **kw: "X" if len(p) == 3 and "000" in (p[1] or "") else "Y")
    rep = privacy_pass.impact(db=str(legacy), dedup=False)
    assert rep["differences"]["classify_market"] > 0


def test_impact_runs_on_a_database_before_the_e6_migration(legacy):
    """Рев'ю E6 (D51): на копії Етапу 0 (без колонки site_key) impact падав на
    «no such column» — тепер колонок, яких немає, не читає."""
    eng = create_engine(f"sqlite:///{legacy}", future=True)
    with eng.begin() as conn:
        conn.execute(text('DROP INDEX "ix_listings_site_key"'))
        conn.execute(text("ALTER TABLE listings DROP COLUMN site_key"))
    eng.dispose()
    sha = _sha(legacy)
    rep = privacy_pass.impact(db=str(legacy), dedup=True)
    assert _sha(legacy) == sha
    assert rep["rows_would_change"] == N_PHONE and sum(rep["differences"].values()) == 0
    assert rep["dedup"]["shapes_differ"] == 0


def test_impact_detects_a_phone_read_as_a_house_number(legacy):
    """Якби номер стояв після коми за назвою вулиці, його прочитали б як номер
    будинку — і заміна змінила б зведення. Перевірка це бачить."""
    eng = create_engine(f"sqlite:///{legacy}", future=True)
    with eng.begin() as conn:
        conn.execute(text("UPDATE listings SET description = 'вул. Мазепи, 067 000 00 01' "
                          "WHERE id = 1"))
    eng.dispose()
    rep = privacy_pass.impact(db=str(legacy), dedup=True)
    assert rep["differences"]["street_from_text"] == 1
    assert rep["dedup"]["shapes_differ"] == 1
    assert "УВАГА" in privacy_pass.render_impact(rep)


def test_apply_refuses_without_yes(legacy, lock):
    sha = _sha(legacy)
    out: list[str] = []
    assert privacy_pass.apply(db=str(legacy), no_backup_check=True, out=out.append) == 2
    assert _sha(legacy) == sha and "--yes" in out[-1]


def test_apply_refuses_without_a_fresh_backup(legacy, lock, monkeypatch):
    sha = _sha(legacy)
    out: list[str] = []
    monkeypatch.setattr(backup, "last_success_at", lambda: None)
    assert privacy_pass.apply(db=str(legacy), yes=True, out=out.append) == 2
    assert "успішного бекапу немає" in out[-1]
    monkeypatch.setattr(backup, "last_success_at",
                        lambda: ops._now() - __import__("datetime").timedelta(hours=5))
    assert privacy_pass.apply(db=str(legacy), yes=True, out=out.append) == 2
    assert "старший за 2 год" in out[-1]
    assert _sha(legacy) == sha


def test_apply_refuses_while_the_cycle_holds_the_lock(legacy, lock):
    held = runner.CycleLock(lock)
    assert held.acquire()
    try:
        sha = _sha(legacy)
        out: list[str] = []
        assert privacy_pass.apply(db=str(legacy), yes=True, no_backup_check=True, wait_min=0,
                                  out=out.append) == 2
        assert _sha(legacy) == sha and "ВІДМОВА" in out[-1]
    finally:
        held.release()


def test_apply_stops_above_max_rows(legacy, lock):
    cfg = privacy.config()
    small = dataclasses.replace(cfg, apply=dataclasses.replace(cfg.apply, max_rows=100))
    sha = _sha(legacy)
    out: list[str] = []
    assert privacy_pass.apply(db=str(legacy), yes=True, no_backup_check=True, cfg=small,
                              out=out.append) == 1
    assert _sha(legacy) == sha and "apply.max_rows" in out[-1]


def test_apply_changes_exactly_the_scanned_rows_in_batches(legacy, lock, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "last_success_at", lambda: ops._now())
    eng = create_engine(f"sqlite:///{legacy}", future=True)
    with eng.connect() as conn:
        others = links_index.table_digest(conn, exclude={"description", "title"})
        before = dict(conn.execute(text("SELECT id, description FROM listings")).all())
    eng.dispose()
    commits = []

    def on_commit(conn):
        if str(legacy) in str(conn.engine.url):
            commits.append(1)

    event.listen(Engine, "commit", on_commit)
    out: list[str] = []
    ids_file = tmp_path / "changed.txt"
    try:
        code = privacy_pass.apply(db=str(legacy), yes=True, ids_out=str(ids_file), out=out.append)
    finally:
        event.remove(Engine, "commit", on_commit)
    assert code == 0, "\n".join(out)
    assert len(commits) == 3                                   # 450 рядків по ≤200
    assert any(f"змінено рядків: {N_PHONE} " in line for line in out), out
    assert any(line.startswith("після: рядків із номерами 0") for line in out), out
    changed = [int(x) for x in ids_file.read_text().split()]
    assert changed == list(range(1, N_PHONE + 1))
    eng = create_engine(f"sqlite:///{legacy}", future=True)
    with eng.connect() as conn:
        assert links_index.table_digest(conn, exclude={"description", "title"}) == others
        after = dict(conn.execute(text("SELECT id, description FROM listings")).all())
    eng.dispose()
    for i in range(1, 601):
        if i <= N_PHONE:
            assert after[i] == privacy.redact_phones(before[i]) != before[i]
            assert PH in after[i]
        else:
            assert after[i] == before[i]                       # ціни, площі, кадастр — як були
    assert after[2] == "Від власника, [телефон], огляд 12.10.2026 о 10:00"              # дата поруч із маскою вціліла
    assert privacy_pass.scan(db=str(legacy))["rows_would_change"] == 0
    # Повтор — нічого не змінює і не падає.
    out2: list[str] = []
    assert privacy_pass.apply(db=str(legacy), yes=True, out=out2.append) == 0
    assert any("змінено рядків: 0 " in line for line in out2), out2
