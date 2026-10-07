"""Схема S2: listings.site_key, слухачі ORM, разове заповнення й звірка (крок E6, D51).

  * `db.migrate()` на базі «як робоча до E6» додає колонку й індекс
    ix_listings_site_key і не змінює даних;
  * слухачі ORM ставлять ключ при вставці й оновленні (ключ завжди відповідає
    original_url; запис джерела його не задає — поле похідне);
  * `links reindex` заповнює ЛИШЕ NULL, пакетами ≤200 рядків, інших колонок не
    чіпає, під замком циклу; `--dry-run` нічого не пише;
  * `links selftest` на копії бази з conftest: кожна адреса знаходить свій рядок.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import db, links_index, runner  # noqa: E402
from realty.models import Base, Listing, Property  # noqa: E402

OLX = "https://www.olx.ua/d/uk/obyavlenie/kvartyra-test-ID{}.html"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _engine(path: Path):
    return create_engine(f"sqlite:///{path}", future=True)


def _seed(engine, n: int = 30) -> None:
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        s.add(Property(id=1, fingerprint="p1", rooms=2, area_total=50.0))
        s.flush()
        for i in range(1, n + 1):
            url = [OLX.format(f"t{i:04d}A"),
                   f"https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-test-{30000000 + i}.html",
                   f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{12000000 + i}/",
                   f"https://flombu.com/uk/estate_deal_sales/{100000 + i}"][i % 4]
            s.add(Listing(source=["olx", "domria", "lun", "flombu"][i % 4], external_id=str(i),
                          original_url=url, price_usd=40_000.0 + i, rooms=2, property_id=1,
                          quality_status="ok", description=f"опис {i}"))
        s.commit()


@pytest.fixture
def before_e6(tmp_path):
    """База «як робоча до E6»: таблиці моделі без колонки site_key і її індексу."""
    path = tmp_path / "prod.db"
    eng = _engine(path)
    Base.metadata.create_all(eng)
    _seed(eng)
    with eng.begin() as conn:
        conn.execute(text('DROP INDEX "ix_listings_site_key"'))
        conn.execute(text("ALTER TABLE listings DROP COLUMN site_key"))
    eng.dispose()
    return path


@pytest.fixture
def lock(tmp_path, monkeypatch):
    path = tmp_path / "cycle.lock"
    monkeypatch.setattr(runner, "LOCK_PATH", path)
    return path


def _cols(path: Path) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute('PRAGMA table_info("listings")')}


def _indexes(path: Path) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute('PRAGMA index_list("listings")')}


def test_migrate_adds_site_key_column_and_index_without_touching_data(before_e6):
    assert "site_key" not in _cols(before_e6)
    eng = _engine(before_e6)
    fp = db.data_fingerprint(eng)
    with eng.connect() as conn:
        digest = links_index.table_digest(conn, exclude=set())
    plan = db.migrate(dry_run=True, bind=eng)
    assert "listings.site_key" in plan.columns_added
    assert "ix_listings_site_key" in plan.indexes_created
    rep = db.migrate(bind=eng)
    assert rep.columns_added == ["listings.site_key"] and "ix_listings_site_key" in rep.indexes_created
    assert "site_key" in _cols(before_e6) and "ix_listings_site_key" in _indexes(before_e6)
    assert db.data_fingerprint(eng) == fp
    with eng.connect() as conn:
        assert links_index.table_digest(conn, exclude={"site_key"}) == digest
        assert conn.execute(text("SELECT count(*) FROM listings WHERE site_key IS NULL")).scalar() == 30
    assert not db.migrate(bind=eng).changed
    eng.dispose()


def test_site_key_lookup_uses_the_index(before_e6):
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    with eng.connect() as conn:
        plan = " ".join(r[3] for r in conn.execute(text(
            "EXPLAIN QUERY PLAN SELECT id FROM listings WHERE site_key = 'olx:x'")))
    assert "ix_listings_site_key" in plan
    eng.dispose()


# --- Слухачі ORM ----------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    eng = _engine(tmp_path / "orm.db")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        s.add(Property(id=1, fingerprint="p1"))
        s.commit()
        yield s
    eng.dispose()


def test_listener_sets_site_key_on_insert(session):
    row = Listing(source="lun", external_id="1", original_url=OLX.format("10BkYC"), property_id=1)
    session.add(row)
    session.commit()
    assert row.site_key == "olx:10BkYC"
    stored = session.execute(text("SELECT site_key FROM listings WHERE id = :i"),
                             {"i": row.id}).scalar()
    assert stored == "olx:10BkYC"


def test_listener_keeps_key_in_step_with_original_url(session):
    row = Listing(source="olx", external_id="10BkYC", original_url=OLX.format("10BkYC"))
    session.add(row)
    session.commit()
    # Нова назва (slug) того самого оголошення OLX — ключ той самий.
    row.original_url = "https://www.olx.ua/d/uk/obyavlenie/novyi-zagolovok-ID10BkYC.html"
    session.commit()
    assert row.site_key == "olx:10BkYC"
    # Інше оголошення — інший ключ; не оголошення — NULL.
    row.original_url = OLX.format("10BkYD")
    session.commit()
    assert row.site_key == "olx:10BkYD"
    row.original_url = "https://example.test/x"
    session.commit()
    assert row.site_key is None
    # Ключ — похідне поле: присвоєний руками «чужий» ключ виправляється з адреси.
    row.original_url = OLX.format("10BkYC")
    session.commit()
    row.site_key = "olx:WRONG"
    session.commit()
    assert row.site_key == "olx:10BkYC"


def test_any_orm_update_fills_a_missing_key(session):
    """Рядок до reindex (NULL) отримує ключ при будь-якому оновленні через ORM."""
    row = Listing(source="olx", external_id="10BkYC", original_url=OLX.format("10BkYC"))
    session.add(row)
    session.commit()
    session.execute(text("UPDATE listings SET site_key = NULL"))
    session.commit()
    session.expire_all()
    row = session.scalars(select(Listing)).one()
    assert row.site_key is None
    row.in_progress = True                                   # дія власника на сайті
    session.commit()
    assert session.execute(text("SELECT site_key FROM listings")).scalar() == "olx:10BkYC"


def test_pipeline_never_takes_site_key_from_a_record(tmp_path, monkeypatch):
    from realty import pipeline
    from realty.pipeline import Pipeline

    eng = _engine(tmp_path / "p.db")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, future=True)
    rec = {"source": "olx", "external_id": "10BkYC", "original_url": OLX.format("10BkYC"),
           "price": 50000, "currency": "USD", "price_usd": 50000.0, "rooms": 2,
           "site_key": "olx:FORGED"}
    with Session() as s:
        Pipeline._upsert(s, dict(rec))
        s.commit()
        Pipeline._upsert(s, dict(rec))
        s.commit()
        assert s.scalars(select(Listing.site_key)).one() == "olx:10BkYC"
    assert "site_key" in pipeline.DERIVED_FIELDS
    eng.dispose()


def _broken_links_dir(tmp_path, monkeypatch):
    from realty import configfiles

    d = tmp_path / "config"
    for src in (ROOT / "config").rglob("*.toml"):
        (d / src.name).parent.mkdir(parents=True, exist_ok=True)
        (d / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (d / "links.toml").write_text("зламано = [", encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(d))


def test_record_cannot_forge_the_key_even_when_links_config_is_broken(tmp_path, monkeypatch):
    """Друга лінія (політика DERIVED у pipeline): коли слухач ключа не може його
    обчислити (зламаний links.toml), значення із запису джерела однаково не береться."""
    from realty.pipeline import Pipeline

    eng = _engine(tmp_path / "p.db")
    Base.metadata.create_all(eng)
    _broken_links_dir(tmp_path, monkeypatch)
    Session = sessionmaker(bind=eng, future=True)
    rec = {"source": "olx", "external_id": "10BkYC", "original_url": OLX.format("10BkYC"),
           "price": 50000, "currency": "USD", "price_usd": 50000.0, "rooms": 2,
           "site_key": "olx:FORGED"}
    with Session() as s:
        Pipeline._upsert(s, dict(rec))
        s.commit()
        Pipeline._upsert(s, {**rec, "price": 49000})
        s.commit()
        assert s.scalars(select(Listing.site_key)).one() is None
    eng.dispose()


@pytest.mark.parametrize("url", [
    "https://[olx.ua/d/uk/obyavlenie/x-ID10BkYC.html",          # недописаний IPv6
    "https://olx.ua\uff03/d/uk/obyavlenie/x-ID10BkYC.html",     # повноширинний «#» з месенджера
])
def test_malformed_url_never_blocks_a_write(session, url, caplog):
    """Рев'ю E6 (D51): urlsplit кидав ValueError крізь слухача — запис падав.
    Тепер розбір дає NotALink, ключ NULL, запис триває; адреса — не в журналі."""
    row = Listing(source="olx", external_id="1", original_url=url)
    session.add(row)
    session.commit()
    assert row.site_key is None
    assert session.execute(text("SELECT count(*) FROM listings")).scalar() == 1
    row.original_url = OLX.format("10BkYC")
    session.commit()
    assert row.site_key == "olx:10BkYC"
    row.original_url = url
    session.commit()
    assert row.site_key is None
    assert not any("olx.ua" in r.getMessage() for r in caplog.records)


def test_unexpected_parser_error_logs_only_its_type(session, monkeypatch, caplog):
    """Запаска слухача: будь-який виняток розбору — ключ не ставиться, запис триває,
    у журналі лише тип винятку (у тексті винятку буває адреса з номером)."""
    from realty import links

    def boom(url):
        raise RuntimeError(f"зламано на {url}")

    monkeypatch.setattr(links, "site_key", boom)
    row = Listing(source="olx", external_id="1", original_url="https://380000000000.rieltor.ua/x")
    session.add(row)
    session.commit()
    assert row.site_key is None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "380000000000" not in logged


def test_broken_links_config_does_not_block_owner_writes(session, tmp_path, monkeypatch, caplog):
    """Ключ — довідкове поле: зламаний links.toml не зупиняє запису (лише журнал)."""
    _broken_links_dir(tmp_path, monkeypatch)
    row = Listing(source="olx", external_id="1", original_url=OLX.format("10BkYC"))
    session.add(row)
    session.commit()                                          # не падає
    assert row.site_key is None
    assert any("config/links.toml" in r.getMessage() for r in caplog.records)


# --- reindex і selftest ----------------------------------------------------------------------


def test_reindex_dry_run_writes_nothing(before_e6, lock):
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    eng.dispose()
    sha = _sha(before_e6)
    out: list[str] = []
    assert links_index.reindex(db=str(before_e6), dry_run=True, out=out.append) == 0
    assert _sha(before_e6) == sha
    assert any("без ключа (site_key IS NULL): 30" in line for line in out)
    assert not lock.exists()                                  # dry-run замка не бере


def test_reindex_fills_only_nulls_in_batches_and_touches_nothing_else(before_e6, lock):
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    with eng.begin() as conn:
        # Непорожнє значення (FILL_ONLY) і адреса, що не є оголошенням.
        conn.execute(text("UPDATE listings SET site_key = 'keep:me' WHERE id = 1"))
        conn.execute(text("UPDATE listings SET original_url = 'https://example.test/x' WHERE id = 2"))
    with eng.connect() as conn:
        digest = links_index.table_digest(conn, exclude={"site_key"})
    fp = db.data_fingerprint(eng)
    eng.dispose()

    commits = []

    def on_commit(conn):
        if str(before_e6) in str(conn.engine.url):
            commits.append(1)

    event.listen(Engine, "commit", on_commit)
    out: list[str] = []
    try:
        code = links_index.reindex(db=str(before_e6), batch=10, out=out.append)
    finally:
        event.remove(Engine, "commit", on_commit)
    assert code == 0, "\n".join(out)
    # 28 рядків до заповнення пакетами по 10 → 3 транзакції запису.
    assert any("заповнено: 28" in line and "транзакцій: 3" in line for line in out), out
    assert any("без ключа: 29 → 1" in line for line in out), out
    assert any("нерозібрані id (лишаться NULL): 2" in line for line in out), out
    eng = _engine(before_e6)
    with eng.connect() as conn:
        assert links_index.table_digest(conn, exclude={"site_key"}) == digest
        keys = dict(conn.execute(text("SELECT id, site_key FROM listings")).all())
    assert db.data_fingerprint(eng) == fp
    assert keys[1] == "keep:me" and keys[2] is None
    assert keys[3] == "flombu:100003" and keys[4] == "olx:t0004A" and keys[6] == "rieltor:12000006"
    assert len(commits) == 3                                  # саме три транзакції запису
    eng.dispose()
    assert links_index.reindex(db=str(before_e6), out=out.append) == 0    # повтор — нічого
    assert out[-1] == "заповнювати нічого"


def test_reindex_never_overwrites_a_key_written_meanwhile(before_e6, lock, monkeypatch):
    """Між планом і записом рядок отримав ключ (слухач ORM у процесі сайту) —
    UPDATE «лише де NULL» його не перезаписує."""
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    eng.dispose()
    real_plan = links_index._plan

    def plan_then_concurrent_write(path, *a, **k):
        result = real_plan(path, *a, **k)
        with sqlite3.connect(path) as c:
            c.execute("UPDATE listings SET site_key = 'written:meanwhile' WHERE id = 5")
        return result

    monkeypatch.setattr(links_index, "_plan", plan_then_concurrent_write)
    out: list[str] = []
    assert links_index.reindex(db=str(before_e6), out=out.append) == 0, out
    with sqlite3.connect(before_e6) as c:
        assert c.execute("SELECT site_key FROM listings WHERE id = 5").fetchone()[0] == \
            "written:meanwhile"
    assert any("заповнено: 29" in line for line in out), out


def _config_dir(tmp_path, monkeypatch, edits: dict[str, str]):
    from realty import configfiles

    d = tmp_path / "config"
    for src in (ROOT / "config").rglob("*.toml"):
        dst = d / src.relative_to(ROOT / "config")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    p = d / "links.toml"
    t = p.read_text(encoding="utf-8")
    for old, new in edits.items():
        assert t.count(old) == 1, old
        t = t.replace(old, new)
    p.write_text(t, encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(d))
    return d


def test_reindex_batch_size_comes_from_the_config(before_e6, lock, tmp_path, monkeypatch):
    """Рев'ю E6 (D51): рядків на транзакцію — `reindex.batch_rows` у links.toml, не в коді."""
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    eng.dispose()
    _config_dir(tmp_path, monkeypatch, {"batch_rows = 200": "batch_rows = 7"})
    out: list[str] = []
    assert links_index.reindex(db=str(before_e6), out=out.append) == 0, out
    assert any("заповнено: 30" in line and "транзакцій: 5 (≤7 рядків" in line for line in out), out


def test_fix_mismatched_rewrites_stale_keys_after_a_config_change(before_e6, lock, tmp_path,
                                                                  monkeypatch):
    """Рев'ю E6 (D51): після правки links.toml старі ключі самі не виправляються
    (reindex — лише NULL, слухач — лише при зміні адреси). --fix-mismatched
    переписує саме їх, пакетами, інших колонок не чіпає; selftest після — 0."""
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    eng.dispose()
    assert links_index.reindex(db=str(before_e6), out=lambda *_: None) == 0
    with sqlite3.connect(before_e6) as c:
        c.execute("UPDATE listings SET site_key = 'olx:WRONG' WHERE id = 4")
    # Правка конфігу: flombu більше не сімейство оголошень → його ключі мають зникнути.
    _config_dir(tmp_path, monkeypatch, {
        'listing_families = ["domria", "olx", "rieltor", "lun", "flombu", "blago"]':
        'listing_families = ["domria", "olx", "rieltor", "lun", "blago"]'})
    rep = links_index.selftest(db=str(before_e6))
    assert rep["stored_mismatch"] == 1 + 7
    eng = _engine(before_e6)
    with eng.connect() as conn:
        digest = links_index.table_digest(conn, exclude={"site_key"})
    eng.dispose()
    # Звичайний reindex (FILL_ONLY) розбіжностей не чіпає; --dry-run нічого не пише.
    out: list[str] = []
    assert links_index.reindex(db=str(before_e6), out=out.append) == 0
    assert out[-1] == "заповнювати нічого"
    sha = _sha(before_e6)
    out = []
    assert links_index.reindex(db=str(before_e6), fix_mismatched=True, dry_run=True,
                               out=out.append) == 0
    assert _sha(before_e6) == sha
    assert any("ключ у базі ≠ ключ з адреси: 8 — перепишемо (з них стане NULL: 7)" in line
               for line in out), out
    out = []
    assert links_index.reindex(db=str(before_e6), fix_mismatched=True, batch=3,
                               out=out.append) == 0, "\n".join(out)
    assert any("ключ у базі ≠ ключ з адреси: 8 → 0" in line for line in out), out
    assert any(line.split() == ["flombu", "7", "0"] for line in out), out
    rep = links_index.selftest(db=str(before_e6))
    assert rep["stored_mismatch"] == 0
    with sqlite3.connect(before_e6) as c:
        assert c.execute("SELECT site_key FROM listings WHERE id = 4").fetchone()[0] == "olx:t0004A"
    eng = _engine(before_e6)
    with eng.connect() as conn:
        assert links_index.table_digest(conn, exclude={"site_key"}) == digest
    eng.dispose()


def test_selftest_rendering_never_rounds_a_miss_up_to_100(before_e6, lock):
    """Рев'ю E6 (D51): 7 999 із 8 001 друкувалось як «100.0%»."""
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    eng.dispose()
    assert links_index.reindex(db=str(before_e6), out=lambda *_: None) == 0
    full = links_index.render_selftest(links_index.selftest(db=str(before_e6)))
    per_source = [line for line in full.splitlines()
                  if line.strip().split(" ")[0] in ("olx", "domria", "lun", "flombu")]
    assert len(per_source) == 4 and all("(100.0%)" in line and "≠" not in line
                                        for line in per_source), per_source
    with sqlite3.connect(before_e6) as c:
        c.execute("UPDATE listings SET site_key = 'olx:WRONG' WHERE id = 4")
    rep = links_index.selftest(db=str(before_e6))
    text_out = links_index.render_selftest(rep)
    olx_line = next(line for line in text_out.splitlines() if line.strip().startswith("olx"))
    assert "6/7 (85.7%) ≠" in olx_line
    # Дрібна частка теж униз: 1999 із 2000 — 99.9%, не 100.0%.
    rep["per_source"]["olx"].update(rows=2000, maps_back=1999)
    assert "1999/2000 (99.9%) ≠" in links_index.render_selftest(rep)


def test_db_level_expectations_of_the_stage0_prototype(tmp_path, lock):
    """Рев'ю E6 (D51): очікування прототипу на рівні бази — у CI, а не лише вручну
    на копії: токени OLX, що починаються з «ID» (IDs3H і s3H — різні рядки);
    10LNlw і 10LNLW — різні ключі; рядок LUN з адресою OLX і власний рядок OLX —
    один ключ, навіть у різних квартирах."""
    path = tmp_path / "proto.db"
    eng = _engine(path)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, future=True)
    olx = "https://www.olx.ua/d/uk/obyavlenie/kvartyra-test-ID{}.html"
    rows = [  # (source, external_id, original_url, property_id)
        ("olx", "IDs3H", olx.format("IDs3H"), 1),
        ("olx", "s3H", olx.format("s3H"), 2),
        ("olx", "10LNlw", olx.format("10LNlw"), 3),
        ("olx", "10LNLW", olx.format("10LNLW"), 4),
        ("olx", "10BkYC", olx.format("10BkYC"), 5),
        ("lun", "4720682454", olx.format("10BkYC"), 6),
        ("lun", "4720682455", "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/", 7),
        ("lun", "4720682456", "https://rieltor.ua/volchinets/flats-sale/view/13017427/", 7),
        ("domria", "34616500",
         "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html", 8),
        ("lun", "4720682457",
         "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-x-34616500.html", 8),
    ]
    with Session() as s:
        for pid in range(1, 9):
            s.add(Property(id=pid, fingerprint=f"p{pid}"))
        s.flush()
        for src, ext, url, pid in rows:
            s.add(Listing(source=src, external_id=ext, original_url=url, property_id=pid))
        s.commit()
    with eng.begin() as conn:
        conn.execute(text("UPDATE listings SET site_key = NULL"))       # як до reindex
    eng.dispose()
    assert links_index.reindex(db=str(path), out=lambda *_: None) == 0
    with sqlite3.connect(path) as c:
        def ids(key):
            return [r[0] for r in c.execute(
                "SELECT external_id FROM listings WHERE site_key = ? ORDER BY id", (key,))]
        assert ids("olx:IDs3H") == ["IDs3H"] and ids("olx:s3H") == ["s3H"]
        assert ids("olx:10LNlw") == ["10LNlw"] and ids("olx:10LNLW") == ["10LNLW"]
        assert ids("olx:10BkYC") == ["10BkYC", "4720682454"]
        assert ids("rieltor:13017427") == ["4720682455", "4720682456"]
        assert ids("domria:34616500") == ["34616500", "4720682457"]
    rep = links_index.selftest(db=str(path))
    assert rep["unparsed"] == 0 and rep["stored_mismatch"] == 0
    assert rep["keys_in_several_properties"] == 1               # olx:10BkYC: квартири 5 і 6
    assert rep["olx_case_insensitive_collision_groups"] == 1    # 10LNlw / 10LNLW
    assert rep["per_source"]["lun"]["via:olx"] == 1
    assert rep["per_source"]["olx"]["id_equals_external_id"] == 5


def test_reindex_refuses_while_the_cycle_holds_the_lock(before_e6, lock):
    eng = _engine(before_e6)
    db.migrate(bind=eng)
    eng.dispose()
    held = runner.CycleLock(lock)
    assert held.acquire()
    try:
        sha = _sha(before_e6)
        out: list[str] = []
        assert links_index.reindex(db=str(before_e6), wait_min=0, out=out.append) == 2
        assert _sha(before_e6) == sha
        assert "ВІДМОВА" in out[-1]
    finally:
        held.release()


def test_selftest_on_the_test_copy_of_the_database(tmp_path, lock):
    """Копія бази з conftest: кожна збережена адреса розбирається й знаходить свій рядок."""
    src = Path(os.environ["DB_URL"].removeprefix("sqlite:///"))
    with sqlite3.connect(src) as c:
        rows = c.execute("SELECT count(*) FROM listings").fetchone()[0]
    if not rows:
        pytest.skip("у тестовій копії немає даних")
    copy = tmp_path / "copy.db"
    a, b = sqlite3.connect(src), sqlite3.connect(copy)
    with b:
        a.backup(b)
    a.close()
    b.close()
    # Інші тести цього прогону дописують у спільну копію рядки з адресами
    # example.test (test_real_schema_writes) — це не адреси джерел.
    with sqlite3.connect(copy) as c:
        artifacts = c.execute("SELECT count(*) FROM listings WHERE original_url LIKE "
                              "'https://example.test/%'").fetchone()[0]
    assert links_index.reindex(db=str(copy), out=lambda *_: None) == 0
    rep = links_index.selftest(db=str(copy))
    assert rep["rows"] == rows and rep["unparsed"] == artifacts and rep["stored_mismatch"] == 0
    with sqlite3.connect(copy) as c:
        for lid in rep["unparsed_ids"]:
            url = c.execute("SELECT original_url FROM listings WHERE id = ?", (lid,)).fetchone()[0]
            assert url.startswith("https://example.test/"), lid
    for source, st in rep["per_source"].items():
        assert st["parsed"] == st["maps_back"] == st["stored_equal"], source
        # Рядок «свого» сайту: id з адреси == external_id (Етап 0: 100% для всіх).
        assert st.get("id_equals_external_id", 0) == st.get("own_site", 0), source
        if source in ("domria", "flombu", "blago"):
            assert st["id_equals_external_id"] == st["own_site"] == st["rows"], source
    assert "ключів, що ведуть у кілька квартир" in links_index.render_selftest(rep)
