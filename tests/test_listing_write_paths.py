"""Кожен шлях запису в listings проходить слухачів ORM — або перелічений тут (E6, D51).

Рішення власника 5 (D46): телефони в описах замінюються на «[телефон]»
автоматично для нових оголошень з УСІХ джерел. Механізм один — слухачі ORM на
Listing (realty/models.py); ключ site_key ставлять вони ж. Тому:

  * СТАТИЧНО: усі Core-записи в listings (update/insert/delete(Listing),
    Listing.__table__, query(Listing), сирий SQL UPDATE/INSERT/REPLACE/DELETE у
    будь-якому регістрі й через кілька рядків, bulk_*), що обходять слухачів,
    перелічено тут разом із колонками. Новий такий запис валить тест — його треба
    або перевести на ORM, або свідомо додати сюди. Жоден Core-запис не пише
    description/title/original_url, крім разової заміни `privacy apply` (значення
    — з того самого privacy.find). Сам пошук перевірено на відомих обходах (рев'ю
    E6, D51);
  * ЗАПАСКА (realty/models.py, do_orm_execute): DML через сесію —
    update(Listing).values(...), insert(Listing) з параметрами, оновлення за
    первинним ключем, query(Listing).update(...), Listing.__table__.update() —
    отримує ту саму заміну й site_key; неперевірний запис (вираз SQL, сирий SQL
    через сесію) — ListingWriteError;
  * ДИНАМІЧНО: збір (_upsert — новий і наявний рядок), дозбір (backfill), LLM-фолбек
    (весь Pipeline.run), запис перевірки за посиланням (Pipeline._write з
    trigger='lookup', як у плані Блоку 5), контроль якості (revalidate) і дії
    сайту — на тимчасовій базі, номери синтетичні;
  * ДЖЕРЕЛА: справжні розбирачі DOM.RIA, LUN, OLX (картка + сторінка деталей),
    flombu (+ сторінка), rieltor (через добір LUN) і Благо → Pipeline._write →
    у базі «[телефон]».
"""
from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import pipeline  # noqa: E402
from realty.models import Base, Condition, Listing, MarketType  # noqa: E402
from realty.pipeline import Pipeline  # noqa: E402
from realty.quality import housekeeping, rules  # noqa: E402
from realty.quality.staging import QualityGate  # noqa: E402

PH = "[телефон]"
PHONE = "067 000 00 01"                 # синтетичний: код оператора чинний, абонент — нулі
PHONE2 = "+38 (050) 000-00-02"
OLX_URL = "https://www.olx.ua/d/uk/obyavlenie/kvartyra-test-ID{}.html"

# --- Статичний перелік Core-записів ----------------------------------------------------------

# Шукається по ВСЬОМУ тексту файлу (SQL буває розбитий на рядки) без урахування
# регістру; у звіті — рядок, де починається збіг. Рядки-коментарі пропускаються.
WRITE_RX = re.compile(
    r"\b(?:update|insert|delete)\s*\(\s*(?:\w+\.)*Listing\b"
    r"|\.query\(\s*(?:\w+\.)*Listing\b"
    r"|\bListing\.__table__\s*\.\s*(?:update|insert|delete)\b"
    r"|\bbulk_(?:update|insert|save)\w*"
    r"|\b(?:UPDATE(?:\s+OR\s+\w+)?|INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|DELETE\s+FROM)"
    r"\s+[\"'`\[]?(?:listings\b|\{)",
    re.IGNORECASE)

# (файл, фрагмент рядка) → (колонки, які пише, чому без слухачів).
ALLOWED = {
    ("realty/dedup.py", "session.execute(update(Listing).where(Listing.id.in_(ids[start"):
        ({"property_id", "last_seen"}, "перенесення оголошень між квартирами; last_seen = last_seen"),
    ("realty/dedup.py", "session.execute(update(Listing)"):
        ({"property_id", "last_seen"}, "перебудова квартир; last_seen = last_seen"),
    ("realty/identity_backfill.py", "s.execute(update(Listing).where(Listing.id == lid).values("):
        ({"identity", "last_seen"}, "нічний дозбір сильних ознак"),
    ("realty/web/deferred.py", "sql = (update(Listing).where(Listing.id == bindparam(\"lid\"))"):
        ({"views", "viewed_at"}, "фоновий запис переглядів сайту"),
    ("realty/db.py", 'f"INSERT INTO listings ({names}) SELECT {names} FROM listings_legacy"'):
        ({"*copy*"}, "перебудова таблиці: дані переносяться як є"),
    ("realty/schema_repair.py", "con.execute(f'INSERT INTO \"{temp}\" ({cols}) SELECT {cols} FROM"):
        ({"*copy*"}, "ремонт ключів: дані переносяться як є"),
    ("realty/snapshot.py", "s.execute(update(Listing).where(Listing.id.in_(chunk))"):
        ({"absent_since"}, "позначки «зник із переліку» (Блок 1, E8, D52): лише absent_since"),
    ("realty/links_index.py", 'text("UPDATE listings SET site_key = :k WHERE id = :id AND site_key IS :old")'):
        ({"site_key"}, "разове заповнення ключа (links.site_key): NULL або --fix-mismatched"),
    ("realty/privacy_pass.py", 'conn.execute(text(f"UPDATE listings SET {sets} WHERE id = :id"), params)'):
        ({"description", "title"}, "разова заміна телефонів: значення — privacy.find"),
    ("realty/dedup.py", '_ROW_UPDATE = ("UPDATE listings SET row_district = :d, row_complex = :c, row_area = :a "'):
        ({"row_district", "row_complex", "row_area"},
         "район і ЖК квартири на її оголошеннях (кеш; Блок 4, E10, D57)"),
    ("realty/places/assign.py", 'res = conn.execute(text(f"UPDATE listings SET {sets} WHERE {where}"), params)'):
        ({"district_key", "district_how", "complex_key", "complex_how", "place_area",
          "place_at", "place_sig"},
         "крок «райони й ЖК»: ключі лише туди, де порожньо; reassign — за рішенням власника (Блок 4, E10, D57)"),
    ("scripts/cleanup_test_artifacts.py", 'con.executemany("UPDATE listings SET views = 0, viewed_at = NULL'):
        ({"views", "viewed_at"}, "прибирання слідів тестів"),
    ("scripts/page_equality.py", 'f"UPDATE listings SET in_progress = 1, in_progress_at = ? "'):
        ({"in_progress", "in_progress_at"}, "еталон сторінок на приватній копії"),
}
TEXT_COLUMNS = {"description", "title", "original_url"}


def _scan_source(src: str) -> list[str]:
    """Рядки (без відступу), де починаються записи в listings, — по всьому тексту."""
    lines = src.splitlines()
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    import bisect

    found = []
    for m in WRITE_RX.finditer(src):
        line = lines[bisect.bisect_right(starts, m.start()) - 1].strip()
        if line.startswith("#"):
            continue
        if line not in found:
            found.append(line)
    return found


def _write_sites() -> list[tuple[str, str]]:
    found = []
    files = [*sorted((ROOT / "realty").rglob("*.py")), ROOT / "cli.py",
             *sorted((ROOT / "scripts").glob("*.py"))]
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if rel == "realty/models.py":           # коментарі й запаска сесії (тест нижче)
            continue
        found += [(rel, line) for line in _scan_source(path.read_text(encoding="utf-8"))]
    return found


@pytest.mark.parametrize("src", [
    # Рев'ю E6 (D51): ці три обходи проходили старий построковий пошук.
    's.query(Listing).filter(Listing.id == i).update({"description": t})',
    's.execute(text("update listings set title = :t where id = :i"), {"t": t, "i": i})',
    's.execute(update(models.Listing).where(models.Listing.id == i).values(description=t))',
    # SQL на кількох рядках, інший регістр, лапки, INSERT OR REPLACE, __table__, bulk.
    'conn.execute(text("""\n    UPDATE\n      listings\n    SET description = :d"""))',
    "cur.execute('Insert Or Replace Into \"listings\" (id, title) VALUES (?, ?)', row)",
    "cur.execute('replace into `listings` values (?)', row)",
    "conn.execute(Listing.__table__.update().values(title=t))",
    "conn.execute(insert( Listing ), rows)",
    "s.bulk_update_mappings(Listing, rows)",
    "s.bulk_save_objects(objs)",
    'conn.execute(text(f"DELETE FROM {table} WHERE id = 1"))',
])
def test_static_guard_catches_known_bypasses(src):
    assert _scan_source("x = 1\n" + src + "\n"), src


def test_static_guard_ignores_reads_and_comments():
    src = ("rows = s.scalars(select(Listing)).all()\n"
           "cols = [c.name for c in Listing.__table__.columns]\n"
           "# UPDATE listings SET description = :d — лише коментар\n"
           "n = conn.execute(text('SELECT count(*) FROM listings')).scalar()\n"
           "conn.execute(text('UPDATE listings_legacy SET x = 1'))\n")
    assert _scan_source(src) == []


def _site_of(rel: str, line: str):
    for (path, needle), spec in ALLOWED.items():
        if path == rel and line.startswith(needle):
            return (path, needle), spec
    return None, None


def test_every_core_write_to_listings_is_known():
    unknown, seen = [], set()
    for rel, line in _write_sites():
        key, _ = _site_of(rel, line)
        if key is None:
            unknown.append(f"{rel}: {line}")
        else:
            seen.add(key)
    assert not unknown, ("Новий запис у listings в обхід слухачів ORM (телефони, site_key) — "
                         "переведіть на ORM або додайте в ALLOWED з колонками:\n  "
                         + "\n  ".join(unknown))
    stale = set(ALLOWED) - seen
    assert not stale, f"застарілі рядки ALLOWED: {sorted(stale)}"


def test_core_writes_do_not_touch_text_columns_except_the_one_time_pass():
    for (path, needle), (cols, _why) in ALLOWED.items():
        if path == "realty/privacy_pass.py":
            assert cols == {"description", "title"}
            continue
        assert not (cols & TEXT_COLUMNS), (path, cols)


def _top_level_kwargs(body: str) -> set[str]:
    """Імена аргументів `ім'я=` на верхньому рівні дужок `.values(...)`."""
    names, depth, i = set(), 0, 0
    while i < len(body):
        ch = body[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (m := re.match(r"(\w+)\s*=(?!=)", body[i:])) and \
                (i == 0 or not (body[i - 1].isalnum() or body[i - 1] == "_")):
            names.add(m.group(1))
            i += m.end()
            continue
        i += 1
    return names


def test_declared_columns_match_the_code():
    """Колонки в ALLOWED — ті, що справді стоять у .values(...) / SET … (де їх видно)."""
    for path, needle in ALLOWED:
        cols = ALLOWED[(path, needle)][0]
        if "*copy*" in cols or path == "realty/privacy_pass.py":
            continue
        src = (ROOT / path).read_text(encoding="utf-8")
        at = src.index(needle)
        chunk = src[at:at + 600]
        if ".values(" in chunk:
            got = _top_level_kwargs(chunk[chunk.index(".values(") + 8:])
        else:
            sets = re.search(r"SET (.+?)\bWHERE", chunk, re.S).group(1)
            got = set(re.findall(r"(\w+)\s*=", sets))
        assert got <= cols, (path, got, cols)


# --- Тимчасова база для шляхів через ORM -----------------------------------------------------


@pytest.fixture
def iso(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'w.db'}", future=True)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False, future=True)

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(pipeline, "session_scope", scope)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(housekeeping, "session_scope", scope)
    yield Session
    eng.dispose()


def _gate() -> QualityGate:
    wide = rules.Band(0, 1e9, 0, 1e9)
    return QualityGate(thresholds=rules.Thresholds(price_usd=wide, price_per_sqm=wide,
                                                   area_total=wide, sample_size=5000))


def _rec(**kw) -> dict:
    rec = {"source": "olx", "external_id": "10BkYC", "original_url": OLX_URL.format("10BkYC"),
           "price": 60000, "currency": "USD", "price_usd": 60000.0, "rooms": 2,
           "area_total": 55.0, "location": "вул. Тестова", "title": "2-к квартира",
           "description": f"Затишна квартира. Дзвоніть {PHONE}.",
           "market_type": MarketType.SECONDARY, "condition": Condition.RENOVATED}
    rec.update(kw)
    return rec


def _stored(Session) -> list[tuple]:
    with Session() as s:
        return [tuple(r) for r in s.execute(text(
            "SELECT external_id, title, description, site_key FROM listings ORDER BY id"))]


def test_upsert_new_and_existing_rows(iso):
    p = Pipeline(sources=["olx"], use_llm=False, gate=_gate())
    p._write([_rec(title=f"Продаж, {PHONE2}")])
    assert _stored(iso) == [("10BkYC", f"Продаж, {PH}", f"Затишна квартира. Дзвоніть {PH}.",
                             "olx:10BkYC")]
    # Наявний рядок: джерело віддало новий текст із номером — у базі заміна.
    p._write([_rec(description=f"Нова ціна! {PHONE2}, viber://chat?number=%2B380670000001")])
    assert _stored(iso)[0][2] == f"Нова ціна! {PH}, {PH}"
    assert p.report.inserted == 1 and p.report.updated == 1


def test_lookup_insert_path(iso):
    """План Блоку 5: перевірка за посиланням пише Pipeline(use_llm=False, trigger='lookup')._write."""
    p = Pipeline(sources=["lun"], use_llm=False, trigger="lookup", gate=_gate())
    p._write([_rec(source="lun", external_id="4720682454",
                   original_url="https://rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/")])
    assert _stored(iso) == [("4720682454", "2-к квартира", f"Затишна квартира. Дзвоніть {PH}.",
                             "rieltor:13017427")]


def _fake_source_class(records, detail_desc):
    """Підставне джерело з добором зі сторінки деталей (без мережі)."""
    from realty.sources.base import BaseSource

    class Fake(BaseSource):
        name = "fake"

        def iter_listings(self):
            yield from (dict(r) for r in records)

        def enrich(self, rec, html):
            if not rec.get("description"):
                rec["description"] = detail_desc
            if not rec.get("rooms"):
                rec["rooms"] = 2
            rec["detail_enriched"] = True
            return rec

    return Fake


def test_backfill_writes_redacted_detail_text(iso, monkeypatch):
    from realty import sources

    with iso() as s:
        s.add(Listing(source="fake", external_id="1", original_url=OLX_URL.format("10BkYC"),
                      price=60000, price_usd=60000.0, rooms=None, location="вул. Тестова",
                      quality_status="ok", condition=Condition.UNKNOWN,
                      market_type=MarketType.UNKNOWN))
        s.commit()
    monkeypatch.setitem(sources.REGISTRY, "fake", _fake_source_class([], f"Опис. Тел. {PHONE}"))
    monkeypatch.setattr(pipeline, "REGISTRY", sources.REGISTRY)
    monkeypatch.setattr(Pipeline, "_page_text", lambda self, rec: "<html>сторінка</html>")
    rep = Pipeline(sources=["fake"], use_llm=False, gate=_gate()).backfill(limit=10)
    assert rep.updated == 1
    assert _stored(iso) == [("1", None, f"Опис. Тел. {PH}", "olx:10BkYC")]


def test_llm_fallback_run_writes_redacted_text(iso, monkeypatch):
    """Увесь Pipeline.run: стрічка → сторінка → LLM доповнює площу → запис."""
    from realty import sources
    from realty.llm import ExtractedListing

    class FakeLLM:
        available = True
        calls = in_tokens = out_tokens = passed = failed = 0
        cost_usd = 0.0

        def __init__(self, *a, **k):
            pass

        def extract(self, html, url):
            FakeLLM.calls += 1
            return ExtractedListing(area_total=55.0)

    rec = _rec(source="fake", external_id="2", area_total=None, price_per_sqm=None,
               description=f"Без площі в стрічці. {PHONE}")
    monkeypatch.setitem(sources.REGISTRY, "fake", _fake_source_class([rec], None))
    monkeypatch.setattr(pipeline, "REGISTRY", sources.REGISTRY)
    monkeypatch.setattr(pipeline, "LLMExtractor", FakeLLM)
    monkeypatch.setattr(Pipeline, "_page_text", lambda self, rec: "<html>сторінка</html>")
    report = Pipeline(sources=["fake"], use_llm=True, gate=_gate()).run()
    assert FakeLLM.calls == 1 and report.inserted == 1
    with iso() as s:
        row = s.scalars(select(Listing)).one()
    assert row.llm_extracted and row.area_total == 55.0
    assert row.description == f"Без площі в стрічці. {PH}" and row.site_key == "olx:10BkYC"


def test_revalidate_and_site_actions_keep_old_text_but_fill_the_key(iso):
    """Рядок, записаний до E6 (номер в описі, ключа немає), — контроль якості й дія
    сайту його тексту не переписують (разова заміна — окремий крок зі свіжим
    бекапом, E7), але ключ (нове порожнє поле) заповнюють."""
    with iso() as s:
        s.add(Listing(source="olx", external_id="10BkYC", original_url=OLX_URL.format("10BkYC"),
                      price=60000, price_usd=60000.0, rooms=2, location="вул. Тестова",
                      area_total=55.0, quality_status="pending"))
        s.commit()
        # Рядок «до E6» — сирим SQL повз сесію (через сесію запаска не дала б).
        s.connection().execute(text("UPDATE listings SET description = :d, site_key = NULL"),
                               {"d": f"Старий опис {PHONE}"})
        s.commit()
    wide = rules.Band(0, 1e9, 0, 1e9)
    housekeeping.revalidate(thresholds=rules.Thresholds(price_usd=wide, price_per_sqm=wide,
                                                        area_total=wide, sample_size=5000))
    assert _stored(iso) == [("10BkYC", None, f"Старий опис {PHONE}", "olx:10BkYC")]
    with iso() as s:
        row = s.scalars(select(Listing)).one()
        row.in_progress = True
        s.commit()
        # А от будь-яке ПЕРЕПИСУВАННЯ тексту через ORM — уже з заміною.
        row.description = row.description + " (оновлено)"
        s.commit()
    assert _stored(iso)[0][2] == f"Старий опис {PH} (оновлено)"


def test_backfill_does_not_count_an_unchanged_redacted_text(iso, monkeypatch):
    """Рев'ю E6 (D51): опис у рядку вже «[телефон]», сторінка віддає той самий
    опис із номером — у базу нічого нового не лягає, отже й «оновлено» 0."""
    from realty import sources
    from realty.sources.base import BaseSource

    raw = f"Опис. Тел. {PHONE}"

    class Same(BaseSource):
        name = "fake"

        def iter_listings(self):
            yield from ()

        def enrich(self, rec, html):
            rec["description"] = raw
            return rec

    from realty.normalize import compute_price_per_sqm, to_uah

    with iso() as s:
        # Решта полів — уже такі, як їх перерахує дозбір: міг би змінитись лише опис.
        s.add(Listing(source="fake", external_id="1", original_url=OLX_URL.format("10BkYC"),
                      price=60000, currency="USD", price_usd=60000.0, rooms=2, area_total=55.0,
                      price_uah=to_uah(60000, "USD"),
                      price_per_sqm=compute_price_per_sqm(60000.0, 55.0, None),
                      location="вул. Тестова", description=raw, quality_status="ok",
                      condition=Condition.UNKNOWN, market_type=MarketType.UNKNOWN))
        s.commit()
    monkeypatch.setitem(sources.REGISTRY, "fake", Same)
    monkeypatch.setattr(pipeline, "REGISTRY", sources.REGISTRY)
    monkeypatch.setattr(Pipeline, "_page_text", lambda self, rec: "<html>сторінка</html>")
    rep = Pipeline(sources=["fake"], use_llm=False, gate=_gate()).backfill(limit=10)
    assert rep.updated == 0
    assert _stored(iso)[0][2] == f"Опис. Тел. {PH}"


def test_session_dml_past_the_objects_is_redacted_and_keyed(iso):
    """Рев'ю E6 (D51): DML через сесію повз об'єкти Listing (подій «set» і
    before_insert/before_update немає) — запаска do_orm_execute у models.py."""
    from sqlalchemy import bindparam, insert, update

    with iso() as s:
        s.add(Listing(source="olx", external_id="a", original_url=OLX_URL.format("10BkYC")))
        s.commit()
        s.execute(update(Listing).where(Listing.external_id == "a")
                  .values(description=f"update {PHONE}"))
        s.query(Listing).filter(Listing.external_id == "a").update({"title": f"query {PHONE2}"})
        s.execute(insert(Listing), [
            {"source": "olx", "external_id": "b", "original_url": OLX_URL.format("10BkYD"),
             "description": f"bulk {PHONE}", "site_key": "olx:FORGED"},
            {"source": "olx", "external_id": "c", "original_url": OLX_URL.format("10BkYE"),
             "title": f"bulk {PHONE2}"}])
        bid = s.scalar(select(Listing.id).where(Listing.external_id == "b"))
        s.execute(update(Listing), [{"id": bid, "title": f"pk {PHONE2}",
                                     "original_url": OLX_URL.format("10BkYF")}])
        s.execute(insert(Listing).values(source="olx", external_id="d",
                                         original_url=OLX_URL.format("10BkYG"),
                                         description=f"values {PHONE}"))
        s.execute(Listing.__table__.update().where(Listing.__table__.c.external_id == "d")
                  .values(title=f"table {PHONE2}"))
        s.execute(update(Listing).where(Listing.external_id == "c")
                  .values(description=bindparam("d"), original_url=bindparam("u")),
                  {"d": f"bind {PHONE}", "u": OLX_URL.format("10BkYH")})
        s.commit()
    assert _stored(iso) == [
        ("a", f"query {PH}", f"update {PH}", "olx:10BkYC"),
        ("b", f"pk {PH}", f"bulk {PH}", "olx:10BkYF"),
        ("c", f"bulk {PH}", f"bind {PH}", "olx:10BkYH"),
        ("d", f"table {PH}", f"values {PH}", "olx:10BkYG"),
    ]


def test_unverifiable_session_writes_are_refused(iso):
    """Значення, яке не перевірити (вираз SQL, сирий SQL через сесію), — помилка,
    а не тихий номер у базі. Інші колонки — як були (перегляди сайту тощо)."""
    from sqlalchemy import func, update

    from realty.models import ListingWriteError

    with iso() as s:
        s.add(Listing(source="olx", external_id="a", original_url=OLX_URL.format("10BkYC"),
                      title="назва"))
        s.commit()
        for bad in (
            lambda: s.execute(text("update listings set title = :t"), {"t": f"x {PHONE}"}),
            lambda: s.execute(text("INSERT OR REPLACE INTO listings (id, source, external_id, "
                                   "original_url, description) VALUES (9, 'olx', 'z', 'u', :d)"),
                              {"d": PHONE}),
            lambda: s.execute(update(Listing).values(description=Listing.title + f" {PHONE}")),
            lambda: s.execute(update(Listing).values(original_url=func.lower(Listing.original_url))),
        ):
            with pytest.raises(ListingWriteError):
                bad()
            s.rollback()
        s.execute(update(Listing).values(views=func.coalesce(Listing.views, 0) + 1))
        s.execute(text("UPDATE listings SET in_progress = 1"))
        s.commit()
    with iso() as s:
        assert s.execute(text("SELECT title, description, views, in_progress FROM listings")).one() \
            == ("назва", None, 1, 1)


def test_constructor_and_setattr_both_redact(iso):
    with iso() as s:
        row = Listing(source="olx", external_id="x", original_url=OLX_URL.format("10BkYC"),
                      description=f"a {PHONE}", title=f"b {PHONE2}")
        assert row.description == f"a {PH}" and row.title == f"b {PH}"   # уже в пам'яті
        setattr(row, "description", f"c {PHONE}")
        assert row.description == f"c {PH}"
        s.add(row)
        s.commit()
    assert _stored(iso)[0][1:3] == (f"b {PH}", f"c {PH}")


def test_value_written_past_the_set_event_is_still_redacted(iso):
    """Запасний шар: значення, покладене в обхід події «set» (прямо в стан
    об'єкта + flag_modified), перед INSERT/UPDATE однаково проходить заміну."""
    from sqlalchemy.orm.attributes import flag_modified, set_committed_value

    with iso() as s:
        row = Listing(source="olx", external_id="y", original_url=OLX_URL.format("10BkYC"))
        row.__dict__["description"] = f"вставка {PHONE}"
        s.add(row)
        s.commit()
        assert _stored(iso)[0][2] == f"вставка {PH}"
        set_committed_value(row, "title", f"оновлення {PHONE2}")
        flag_modified(row, "title")
        s.commit()
    assert _stored(iso)[0][1] == f"оновлення {PH}"


# --- Кожне джерело: справжній розбирач → Pipeline._write --------------------------------------


def _domria_rec():
    from realty.config import DOMRIA_CITY_ID
    from realty.sources.domria import DomRiaSource

    card = {"city_id": DOMRIA_CITY_ID, "realty_id": 34616500,
            "beautiful_url": "realty-prodaja-kvartira-ivano-frankovsk-test-ulitsa-34616500.html",
            "description_uk": f"Продаж квартири. Телефон {PHONE}.", "advert_title": "2-к",
            "price_total": 60000, "currency_type_id": 1, "rooms_count": 2,
            "total_square_meters": 55, "street_name_uk": "Тестова", "building_number_str": "1"}
    src = DomRiaSource()
    return src.finalize(src._parse(card) | {"source": "domria"})


def _lun_rec():
    from realty.sources.lun import LunSource

    obj = {"id": 4720682454, "urlRaw": "https://lun.ua/uk/realty/4720682454", "price": 60000,
           "currency": "usd", "roomCount": 2, "areaTotal": 55,
           "geo": "вул. Тестова, Івано-Франківськ", "header": f"2-к, {PHONE2}",
           "text": f"Опис LUN {PHONE}"}
    src = LunSource()
    return src.finalize(src._parse(obj) | {"source": "lun"})


def _rieltor_rec():
    """Рядок LUN, що веде на rieltor: опис дає добір зі сторінки rieltor."""
    from realty.sources.lun import LunSource

    obj = {"id": 4720682455, "urlRaw": "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13017427/",
           "price": 60000, "currency": "usd", "roomCount": 2, "areaTotal": 55,
           "geo": "вул. Тестова, Івано-Франківськ", "header": "2-к", "text": ""}
    src = LunSource()
    rec = src.finalize(src._parse(obj) | {"source": "lun"})
    html = (f'<div class="offer-view-section-text">Опис rieltor. Дзвоніть: {PHONE}</div>')
    return src.enrich(rec, html)


def _olx_rec():
    from bs4 import BeautifulSoup

    from realty.sources.olx import OlxSource

    card = BeautifulSoup(
        '<div data-cy="l-card"><a href="/d/uk/obyavlenie/kvartyra-test-ID10BkYC.html?reason=x">'
        '<h6 data-testid="ad-card-title">2-кімнатна квартира</h6></a>'
        '<p data-testid="ad-price">60 000 $</p>'
        '<p data-testid="location-date">Івано-Франківськ, Центр - Сьогодні о 10:00</p>'
        '<span>55 м²</span></div>', "lxml").select_one('[data-cy="l-card"]')
    src = OlxSource()
    rec = src.finalize(src._parse(card) | {"source": "olx"})
    detail = (f'<div data-testid="ad_description">Опис OLX. Тел. {PHONE2}, 0X******XX</div>')
    return src.enrich(rec, detail)


def _flombu_rec():
    from realty.sources.flombu import FlombuSource

    item = {"id": 116037, "attributes": {"type2HumanVal": "Квартира", "title": "2-к квартира",
                                         "price": 60000, "priceCurrency": "USD",
                                         "addressToStreet": "вул. Тестова, Івано-Франківськ",
                                         "addressLocalityHumanVal": "Івано-Франківськ",
                                         "estateSizeHumanVal": "55 м²",
                                         "tileEstateAccentAttrs": ["2 кімнати"]}}
    src = FlombuSource()
    rec = src.finalize(src._parse(item, {}) | {"source": "flombu"})
    html = f"<div><h2>Опис</h2><p>Простора квартира в центрі міста, власник. {PHONE}</p></div>"
    return src.enrich(rec, html)


def _blago_rec():
    return {"source": "blago", "external_id": "41685",
            "original_url": "https://blagodeveloper.com/plannings/41685/", "price": 60000,
            "currency": "USD", "price_usd": 60000.0, "rooms": 2, "area_total": 55.0,
            "location": "ЖК Тест", "title": f"Планування 2к {PHONE}", "description": None}


@pytest.mark.parametrize("build,key", [
    (_domria_rec, "domria:34616500"), (_lun_rec, "lun:4720682454"),
    (_rieltor_rec, "rieltor:13017427"), (_olx_rec, "olx:10BkYC"),
    (_flombu_rec, "flombu:116037"), (_blago_rec, "blago:41685"),
], ids=["domria", "lun", "rieltor-via-lun", "olx", "flombu", "blago"])
def test_ingestion_from_every_source(iso, build, key):
    rec = build()
    raw = f"{rec.get('title')} {rec.get('description')}"
    assert re.search(r"\d{3}", raw), "у записі джерела має бути номер — інакше тест порожній"
    Pipeline(sources=[rec["source"]], use_llm=False, gate=_gate())._write([rec])
    (ext, title, desc, site_key), = _stored(iso)
    assert site_key == key
    stored = f"{title} {desc}"
    assert PH in stored
    assert "000 00 01" not in stored and "000-00-02" not in stored and "******" not in stored
