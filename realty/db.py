"""Підключення до БД, сесії й міграція схеми.

Міграція (Блок 2, крок E4, D50) — одна функція `migrate()`, яку свідомо
запускає `cli.py db migrate` під замком циклу, а не перший-ліпший процес після
`git pull`: init_db() викликають 11 місць (кожен процес збору, якість, сайт…),
і два одночасні ALTER падали б на «duplicate column», а побудова індексу на
94-МБ таблиці на HDD (3–10 с під блокуванням запису) потрапила б у випадковий
процес — можливо, в запит сайту.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex

from . import txnwatch
from .config import DB_URL
from .models import Base

log = logging.getLogger(__name__)

# Скільки чекати, якщо база зайнята чужим записом, перш ніж здатися.
BUSY_TIMEOUT_MS = 30_000

engine = create_engine(DB_URL, future=True)


@event.listens_for(engine, "connect")
def _tune_sqlite(dbapi_connection, _record) -> None:
    """Режим WAL і терпіння до зайнятої бази.

    20.09.2026 сторож не зміг прочитати `check_events`, поки цикл писав:
    «database is locked». У звичайному режимі SQLite читач чекає на письменника
    і за 5 секунд здається. У WAL читання й запис не заважають одне одному, а
    busy_timeout дає запас на рідкісні збіги. Бекап це не ламає: копія
    робиться через backup API, який враховує вміст -wal.
    """
    cur = dbapi_connection.cursor()
    try:
        cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA journal_mode = WAL")
        # Перевірка зовнішніх ключів. 20.09 я ввімкнув її, не перевіривши схему,
        # і добу падав запис: `price_events` посилався на `listings_legacy`.
        # 21.09 схему відремонтовано (`cli.py schema repair`, звірка кожного
        # рядка) — тепер перевірка знову ввімкнена й захищає від сиріт.
        cur.execute("PRAGMA foreign_keys = ON")
    finally:
        cur.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# Лічильник зафіксованих записів (Блок 2, крок E5, D50): з нього сайт знає, що
# дані в його ж процесі змінились, а інші процеси — що треба збільшити
# покоління кешу сайту при виході (txnwatch.install_autobump).
txnwatch.TRACKER.install()


def tune_for_web(cache_kb: int) -> None:
    """PRAGMA cache_size для з'єднань САЙТУ (config/speed.toml, sqlite.cache_kb).

    Лише вебпроцес: кроки циклу живуть хвилини й мають свою пам'ять. Ставиться
    на кожне з'єднання пулу при першій видачі (і тим, що відкрились до виклику).
    Від'ємне значення в PRAGMA — це кілобайти, а не сторінки.
    """
    value = -int(cache_kb)

    @event.listens_for(engine, "checkout")
    def _cache_size(dbapi_connection, record, _proxy) -> None:
        if record.info.get("realty_cache_kb") == value:
            return
        cur = dbapi_connection.cursor()
        try:
            cur.execute(f"PRAGMA cache_size = {value}")
        finally:
            cur.close()
        record.info["realty_cache_kb"] = value


# Індекси, які модель оголошувала до D50 (index=True на quality_status,
# is_active, manual_active, views, last_attempt, last_alive_at). У робочій базі
# їх немає (create_all не додає індексів до наявної таблиці), а на свіжих
# установках вони є — і ix_listings_quality_status, за виміром Блоку 2,
# сповільнює першу сторінку списку 0,4 → 36 мс, а глибоку 15 → 148 мс. Тому
# migrate() їх видаляє, ЯКЩО вони є; жоден інший індекс migrate() не видаляє.
OBSOLETE_INDEXES = (
    "ix_listings_quality_status", "ix_listings_is_active", "ix_listings_manual_active",
    "ix_listings_views", "ix_listings_last_attempt", "ix_listings_last_alive_at",
)


def init_db() -> None:
    """Те, що безпечно робити в будь-якому процесі: нові таблиці й колонки.

    Індексів на наявних таблицях не будує (це `migrate()`, явно й під замком):
    лише попереджає в журналі, якщо оголошених індексів бракує — сайт тоді
    працює так само, але повільніше.
    """
    _drop_obsolete_url_unique()
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _warn_missing_indexes()


@dataclass
class MigrationReport:
    """Що зробив (або зробив би, `dry_run`) `migrate()` — для `cli.py db migrate`."""

    dry_run: bool
    rebuild_listings: bool = False          # знято застаріле UNIQUE(original_url)
    tables_created: list[str] = field(default_factory=list)
    columns_added: list[str] = field(default_factory=list)
    indexes_created: list[str] = field(default_factory=list)
    indexes_dropped: list[str] = field(default_factory=list)
    ddl: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.rebuild_listings or self.tables_created or self.columns_added
                    or self.indexes_created or self.indexes_dropped)


@contextmanager
def _reading(bind):
    """З'єднання для читання: передане (`Connection`) — воно ж, інакше нове."""
    if isinstance(bind, Connection):
        yield bind
    else:
        with bind.connect() as conn:
            yield conn


@contextmanager
def _writing(bind):
    """Транзакція для запису: у переданому `Connection` — його ж транзакція
    (`cli.py db migrate` тримає одну BEGIN IMMEDIATE на всю міграцію), інакше —
    окрема коротка транзакція."""
    if isinstance(bind, Connection):
        yield bind
    else:
        with bind.begin() as conn:
            yield conn


def migrate(*, dry_run: bool = False, bind=None) -> MigrationReport:
    """Схему бази — до моделі: таблиці, колонки, індекси (Блок 2, D50).

    Порядок інтеграційного плану (S1): застаріле UNIQUE(original_url) →
    create_all (лише нові таблиці) → відсутні колонки → індекси. Схема лише
    доповнюється; DROP — тільки для імен з OBSOLETE_INDEXES. Дані не
    змінюються: кількості й суми до/після звіряє `cli.py db migrate`.
    `dry_run` — нічого не пише, лише план DDL. `bind` — рушій або
    `Connection` з уже відкритою транзакцією: тоді вся міграція — у ній (DDL
    у SQLite транзакційний), а перебудову listings треба зробити раніше.
    """
    eng = bind or engine
    started = time.perf_counter()
    rep = MigrationReport(dry_run=dry_run)
    insp = inspect(eng)
    if insp.has_table("listings") and _has_url_unique(eng):
        rep.rebuild_listings = True
        rep.ddl.append("-- перебудова listings: зняти UNIQUE(original_url) (дані переносяться повністю)")
        if not dry_run:
            if isinstance(eng, Connection):
                # PRAGMA foreign_keys усередині транзакції не діє, а перебудова
                # на нього покладається — її робить `cli.py db migrate` окремо.
                raise RuntimeError("перебудову listings не можна робити в чужій транзакції")
            _drop_obsolete_url_unique(eng)
            insp = inspect(eng)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            rep.tables_created.append(table.name)
            rep.ddl.append(f"-- CREATE TABLE {table.name} (разом з її індексами)")
    if rep.tables_created and not dry_run:
        Base.metadata.create_all(eng)
    for table, _col, ddl in _missing_columns(eng):
        rep.columns_added.append(f"{table}.{_col}")
        rep.ddl.append(ddl)
    if rep.columns_added and not dry_run:
        _add_missing_columns(eng)
    creates, drops = index_plan(eng)
    rep.indexes_created = [name for name, _ in creates]
    rep.indexes_dropped = [name for name, _ in drops]
    rep.ddl += [ddl for _, ddl in drops] + [ddl for _, ddl in creates]
    if not dry_run and (creates or drops):
        # Без переданої транзакції кожен індекс — окрема коротка транзакція:
        # блокування запису тримається на час одного індексу.
        for _, ddl in drops + creates:
            with _writing(eng) as conn:
                conn.execute(text(ddl))
    rep.seconds = round(time.perf_counter() - started, 2)
    return rep


def existing_indexes(bind=None) -> dict[str, str]:
    """{ім'я індексу: таблиця} — з sqlite_master, а не з рефлексії SQLAlchemy.

    `inspect().get_indexes` пропускає індекси за виразом (лише SAWarning), тож
    ix_listings_visible і ix_listings_keeper для неї «не існують» — і помічник,
    побудований на ній, створював би їх щоразу або конфліктував за іменем.
    Автоіндекси обмежень (sql IS NULL) сюди не входять: їх не можна ні створити,
    ні видалити окремо.
    """
    with _reading(bind or engine) as conn:
        rows = conn.execute(text(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"))
        return {name: tbl for name, tbl in rows}


def index_plan(bind=None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """([(ім'я, CREATE …)], [(ім'я, DROP …)]) — чого бракує й що застаріло.

    Оголошені в моделі індекси наявних таблиць, яких немає в базі, — CREATE
    INDEX IF NOT EXISTS (текст DDL — з тієї самої моделі, тож вираз
    актуальності в індексі збігається з виразом у запитах). Таблиць, яких ще
    немає, тут немає: їх разом з індексами створює create_all.
    """
    eng = bind or engine
    have = existing_indexes(eng)
    tables = set(inspect(eng).get_table_names())
    creates = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        for ix in sorted(table.indexes, key=lambda i: i.name):
            if ix.name not in have:
                ddl = str(CreateIndex(ix, if_not_exists=True).compile(dialect=eng.dialect))
                creates.append((ix.name, ddl))
    drops = [(name, f'DROP INDEX IF EXISTS "{name}"') for name in OBSOLETE_INDEXES
             if name in have]
    return creates, drops


_index_warning_done = False


def _warn_missing_indexes() -> None:
    global _index_warning_done
    if _index_warning_done:
        return
    _index_warning_done = True
    try:
        creates, drops = index_plan()
    except Exception as e:                            # noqa: BLE001 — лише попередження
        log.warning("не вдалося перевірити індекси: %s", e)
        return
    if creates or drops:
        log.warning("Схема індексів відстає від моделі (бракує: %s; застарілі: %s) — "
                    "сайт працює, але повільніше. Виконайте `cli.py db migrate` між циклами.",
                    ", ".join(n for n, _ in creates) or "—", ", ".join(n for n, _ in drops) or "—")


def _has_url_unique(bind) -> bool:
    """Чи лишилось у listings застаріле UNIQUE(original_url).

    Через PRAGMA, а не `inspect().get_unique_constraints`: та рефлексує й
    індекси таблиці і на індексах за виразом (D50) сипле SAWarning. Обмеження
    UNIQUE (і в рядку колонки, і окремим рядком) SQLite тримає як автоіндекс
    із походженням «u».
    """
    with _reading(bind) as conn:
        for row in conn.execute(text('PRAGMA index_list("listings")')).all():
            name, unique, origin = row[1], row[2], row[3]
            if not unique or origin != "u":
                continue
            cols = [r[2] for r in conn.execute(text(f'PRAGMA index_info("{name}")'))]
            if cols == ["original_url"]:
                return True
    return False


def _created_indexes(conn, table: str) -> list[str]:
    """Імена індексів таблиці, створених CREATE INDEX (без автоіндексів обмежень).

    PRAGMA index_list, а не рефлексія: та пропускає індекси за виразом (D50).
    """
    return [row[1] for row in conn.execute(text(f'PRAGMA index_list("{table}")'))
            if row[3] == "c"]


def _drop_obsolete_url_unique(bind=None) -> None:
    """Знімає застаріле UNIQUE(original_url) перебудовою таблиці.

    SQLite не вміє видаляти обмеження через ALTER, а обмеження помилкове:
    LUN віддає посилання на olx.ua, і той самий URL законно приходить від двох
    джерел. Дані переносяться повністю.
    """
    engine_ = bind or engine
    insp = inspect(engine_)
    if not insp.has_table("listings"):
        return
    if not _has_url_unique(engine_):
        return
    cols = [c["name"] for c in insp.get_columns("listings")]
    names = ", ".join(f'"{c}"' for c in cols)
    # Індекси в SQLite переїжджають разом із перейменованою таблицею, але їхні
    # імена лишаються глобальними, тож без цього кроку CREATE INDEX конфліктує.
    # Перелік — з PRAGMA index_list (D50): рефлексія пропустила б індекси за
    # виразом, і create_all нижче впав би на «index … already exists».
    with engine_.connect() as conn:
        old_indexes = _created_indexes(conn, "listings")
    log.warning("Міграція: знімаємо UNIQUE(original_url) — перебудова таблиці")
    with engine_.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=off"))
        # legacy_alter_table=ON: інакше SQLite перепише посилання в ІНШИХ
        # таблицях на `listings_legacy`, яку ми зараз видалимо. Саме так
        # `price_events` лишився з битим ключем (виправлено 21.09.2026).
        conn.execute(text("PRAGMA legacy_alter_table=ON"))
        conn.execute(text("ALTER TABLE listings RENAME TO listings_legacy"))
        conn.execute(text("PRAGMA legacy_alter_table=OFF"))
        for ix in old_indexes:
            conn.execute(text(f'DROP INDEX IF EXISTS "{ix}"'))
    Base.metadata.create_all(engine_)
    with engine_.begin() as conn:
        conn.execute(text(
            f"INSERT INTO listings ({names}) SELECT {names} FROM listings_legacy"
        ))
        moved = conn.execute(text("SELECT COUNT(*) FROM listings")).scalar()
        legacy = conn.execute(text("SELECT COUNT(*) FROM listings_legacy")).scalar()
        if moved != legacy:
            raise RuntimeError(
                f"перенесено {moved} із {legacy} рядків — таблицю listings_legacy лишаю"
            )
        conn.execute(text("DROP TABLE listings_legacy"))
        conn.execute(text("PRAGMA foreign_keys=on"))
    log.warning("Міграція завершена: перенесено %s рядків", moved)


def _missing_columns(bind=None) -> list[tuple[str, str, str]]:
    """[(таблиця, колонка, ALTER …)] — колонки моделі, яких немає в наявних таблицях."""
    eng = bind or engine
    insp = inspect(eng)
    out = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = col.type.compile(eng.dialect)
            # Значення за замовчуванням беремо з самої колонки: сліпий DEFAULT 0
            # для `is_active` позначив би всю базу як неактуальну.
            default = ""
            arg = getattr(col.default, "arg", None) if col.default is not None else None
            if isinstance(arg, bool):
                default = f" DEFAULT {1 if arg else 0}"
            elif isinstance(arg, (int, float)):
                default = f" DEFAULT {arg}"
            elif ddl.upper().startswith(("BOOL", "INT")) and not col.nullable:
                default = " DEFAULT 0"
            out.append((table.name, col.name,
                        f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}{default}"))
    return out


def _add_missing_columns(bind=None) -> None:
    """Проста міграція: доливає нові колонки в уже створену таблицю.

    Схема тут росте лише додаванням полів, тож повноцінний Alembic був би
    надмірним — але й мовчки втрачати зібрані дані не хочеться.

    Два процеси, що стартували одночасно після оновлення коду, обидва бачать
    колонку відсутньою; другий ALTER падає на «duplicate column name». Це не
    помилка, а гонка, яку виграв сусід (інтеграційний план S1): колонка є —
    чого й хотіли.
    """
    eng = bind or engine
    for table, column, ddl in _missing_columns(eng):
        try:
            with _writing(eng) as conn:
                conn.execute(text(ddl))
        except OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
            log.info("колонку %s.%s уже додав інший процес", table, column)


# Що звіряти до/після міграції (інтеграційний план, процедура розгортання п. 3, 5).
FINGERPRINT_TABLES = ("listings", "properties", "price_events", "check_events",
                      "data_reports", "property_redirects", "dedup_decisions")


def data_fingerprint(bind=None) -> dict:
    """Кількості рядків і контрольні суми — доказ, що міграція не зачепила даних."""
    eng = bind or engine
    tables = set(inspect(eng).get_table_names())
    out = {}
    with _reading(eng) as conn:
        for t in FINGERPRINT_TABLES:
            if t in tables:
                out[f"count({t})"] = conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
        if "listings" in tables:
            out["sum(listings.id)"] = conn.execute(text("SELECT total(id) FROM listings")).scalar()
            out["sum(listings.price_usd)"] = conn.execute(
                text("SELECT total(price_usd) FROM listings")).scalar()
            out["max(listings.last_seen)"] = conn.execute(
                text("SELECT max(last_seen) FROM listings")).scalar()
    return out


def integrity(bind=None) -> tuple[str, list]:
    """(PRAGMA integrity_check, рядки PRAGMA foreign_key_check)."""
    with _reading(bind or engine) as conn:
        check = conn.execute(text("PRAGMA integrity_check")).scalar()
        fk = [tuple(r) for r in conn.execute(text("PRAGMA foreign_key_check")).all()]
    return check, fk


@contextmanager
def session_scope():
    s: Session = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
