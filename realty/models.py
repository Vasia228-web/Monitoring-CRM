"""Схема даних: одне оголошення про продаж квартири."""
from __future__ import annotations

import enum
import logging
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, Index, case, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class MarketType(str, enum.Enum):
    """Первинний ринок (новобудова) vs вторинний (старий фонд)."""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {"primary": "Новобудова", "secondary": "Вторинний ринок",
                "unknown": "Не визначено"}[self.value]


class Condition(str, enum.Enum):
    """Стан: готова до проживання vs без ремонту / сирець."""

    RENOVATED = "renovated"
    NEEDS_REPAIR = "needs_repair"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {"renovated": "З ремонтом", "needs_repair": "Без ремонту",
                "unknown": "Не визначено"}[self.value]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Статуси, з якими запис вважається придатним до показу й статистики.
CLEAN_STATUSES = ("ok",)


def is_clean():
    """Умова «запис пройшов контроль якості»."""
    return Listing.quality_status.in_(CLEAN_STATUSES)


def effective_active():
    """Чинна актуальність: ручне рішення сильніше за автоматичне."""
    return case((Listing.manual_active.isnot(None), Listing.manual_active),
                else_=Listing.is_active)


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_source_external"),
        Index("ix_price_usd", "price_usd"),
        Index("ix_rooms", "rooms"),
        Index("ix_condition", "condition"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- Обов'язкові поля зі схеми ТЗ -----------------------------------------
    source: Mapped[str] = mapped_column(String(32), index=True)
    # Не унікальний: LUN агрегує оголошення з інших сайтів, тож те саме
    # посилання приходить і від LUN, і від OLX. Ключ ідентичності —
    # (source, external_id); однакові URL зводить дедуплікація.
    original_url: Mapped[str] = mapped_column(String(1024), index=True)
    # Ключ «сайт:id» сайту, на який веде original_url (крок E6, D51): domria:…,
    # olx:… (з урахуванням регістру), rieltor:…, lun:…, flombu:…, blago:….
    # Рядки LUN несуть адреси OLX/rieltor/DIM.RIA — їхній ключ той самий, що й у
    # рядка першоджерела. ПОХІДНЕ поле: ставлять лише слухачі ORM нижче з
    # original_url (realty/links.py); записи джерел його не задають. NULL — ще
    # не заповнено (`cli.py links reindex`) або адреса не є оголошенням.
    site_key: Mapped[str | None] = mapped_column(String(64), index=True)
    price: Mapped[float | None] = mapped_column(Float)            # у валюті оголошення
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    rooms: Mapped[int | None] = mapped_column(Integer)
    location: Mapped[str | None] = mapped_column(String(512))     # адреса/район
    price_per_sqm: Mapped[float | None] = mapped_column(Float)    # USD/м², рахуємо якщо немає
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    market_type: Mapped[MarketType] = mapped_column(
        Enum(MarketType, native_enum=False), default=MarketType.UNKNOWN, index=True
    )
    condition: Mapped[Condition] = mapped_column(
        Enum(Condition, native_enum=False), default=Condition.UNKNOWN, index=True
    )

    # --- Нормалізовані/додаткові поля -----------------------------------------
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(512))
    price_usd: Mapped[float | None] = mapped_column(Float)   # єдина шкала для сортування
    price_uah: Mapped[float | None] = mapped_column(Float)
    area_total: Mapped[float | None] = mapped_column(Float)
    district: Mapped[str | None] = mapped_column(String(256))
    floor: Mapped[int | None] = mapped_column(Integer)
    floors_total: Mapped[int | None] = mapped_column(Integer)
    built_year: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    complex_name: Mapped[str | None] = mapped_column(String(256))  # ЖК

    # --- Актуальність ---------------------------------------------------------
    # `is_active` виставляє перевірка посилань, `manual_active` — людина.
    # Ручне рішення завжди сильніше за автоматичне; None означає «не чіпали».
    # Без index=True (Блок 2, D50): окремі індекси на булевих полях і на
    # quality_status шкодять планувальнику — виміряно, що ix_listings_quality_status
    # сповільнює першу сторінку списку з 0,4 до 36 мс, а глибоку — з 15 до 148 мс
    # (ANALYZE не рятує), а запити кроків циклу могли б перехопити план. У
    # робочій базі цих індексів і не було: create_all не додає індексів до
    # наявної таблиці. Видимість у списку обслуговують складені індекси нижче
    # (ix_listings_visible, ix_listings_keeper), а застарілі імена прибирає
    # `db.migrate()` (OBSOLETE_INDEXES), якщо вони є (свіжі установки до D50).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    manual_active: Mapped[bool | None] = mapped_column(Boolean)
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # `last_checked` — коли ми востаннє ОТРИМАЛИ ВІДПОВІДЬ, і вона була
    # зрозумілою: живе або знято. Саме ця позначка потрібна аналізу виживання,
    # бо задає проміжок між «точно було живе» і «вже мертве».
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # `last_attempt` — коли ми востаннє СПРОБУВАЛИ, незалежно від результату.
    # Окреме поле, бо ці дві дати відповідають на різні питання. Черга
    # впорядковується за спробою: без цього оголошення, яке стабільно віддає
    # 403, щоразу потрапляє на початок черги й блокує її назавжди — саме так
    # 2105 посилань LUN на olx.ua не перевірились жодного разу. Змішати їх
    # означало б або зациклити чергу, або збрехати аналітиці, що ми бачили
    # оголошення живим тоді, коли насправді не достукались.
    # Без індексу (D50): черга перевірок однаково сортує всю таблицю.
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime)
    # Скільки разів картку об'єкта відкривали. Те, на що дивляться, варто
    # перевіряти частіше за те, на що ніхто не дивиться: мертве посилання
    # дратує рівно там, куди дивляться. Лічильник тримаємо на оголошенні, а
    # не в окремій таблиці, бо нам потрібне саме число для черги, а не
    # журнал переглядів — журнал був би даними про користувача без потреби.
    views: Mapped[int] = mapped_column(Integer, default=0)          # без індексу — D50
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Скільки разів поспіль відповідь була незрозумілою. Потрібно, щоб
    # безнадійні посилання поступово відходили в кінець черги, а не з'їдали
    # бюджет кожного прогону.
    check_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Коли востаннє бачили оголошення ЖИВИМ. Разом із `delisted_at` це й є
    # інтервал, усередині якого воно зникло. Точної дати зняття ми не знаємо
    # і знати не можемо — аналіз виживання вміє працювати з інтервалом, але
    # тільки якщо його межі збережені.
    last_alive_at: Mapped[datetime | None] = mapped_column(DateTime)  # без індексу — D50
    # Блок 1, схема S3 (крок E8, D52). Коли рядок (DOM.RIA — ключ «domria:<id>»
    # на рядках УСІХ джерел; LUN і flombu — свій external_id) уперше зник із
    # ПОВНОГО переліку свого джерела; NULL — присутній або перелік не
    # застосовний. Ставить лише крок «різниця списків» (realty/snapshot.py).
    # Відсутність НІКОЛИ не знімає з продажу: вона лише ставить ключ у ярус
    # підказаних перевірок за графіком run.absent_backoff_hours. Частковий індекс
    # (інтеграція, конфлікт 1): позначених — сотні з ~29 тис.
    absent_since: Mapped[datetime | None] = mapped_column(DateTime)
    # Дата зняття, яку повідомило саме джерело (DOM.RIA deleted_at зі стану
    # сторінки, переведено в UTC). Ставиться лише разом зі зняттям і лише якщо
    # порожньо; при поверненні переходить у подію й обнуляється. Строк продажу
    # рахується від неї, якщо вона раніша за нашу дату виявлення.
    source_removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Полагоджена адреса для перевірки (подія url_repaired): original_url лишається
    # адресою джерела — її переписує кожен збір (інтеграція, конфлікт 21), тож
    # ремонт живе окремо, і перевірка бере його першим.
    probe_url: Mapped[str | None] = mapped_column(String(1024))
    # Сирі докази для Блоків 3 (тип продавця) і 4 (район/ЖК) — схема S3
    # інтеграційного плану. Пишуться ЛИШЕ туди, де порожньо (нові ключі — так,
    # наявні — ніколи; pipeline.FILL_ONLY_JSON); жодних імен і телефонів.
    seller_evidence: Mapped[dict | None] = mapped_column(JSON)
    seller_profile: Mapped[str | None] = mapped_column(String(96), index=True)
    seller_evidence_at: Mapped[datetime | None] = mapped_column(DateTime)
    place_raw: Mapped[dict | None] = mapped_column(JSON)

    # --- Район і ЖК (Блок 4, схема S4, крок E10, D57) -------------------------------
    # Нормалізовані ключі довідника config/places/*.toml. Пише ЛИШЕ крок «райони й ЖК»
    # (`cli.py places assign`) і ЛИШЕ туди, де порожньо (complex_key '_none' — «не в
    # ЖК» — дозволено уточнити конкретним ЖК); зміна вже визначеного лише рахується
    # (would_change, /status). Сирі district/complex_name/location не змінюються.
    # NULL — «не визначено». how — яким ступенем визначено (rules.tiers).
    district_key: Mapped[str | None] = mapped_column(String(48))
    district_how: Mapped[str | None] = mapped_column(String(16))
    complex_key: Mapped[str | None] = mapped_column(String(64))       # '_none' — не в ЖК
    complex_how: Mapped[str | None] = mapped_column(String(16))
    place_area: Mapped[str | None] = mapped_column(String(8))         # city|hromada|outside
    place_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Відбиток входів (докази, версії довідника й правил): чим визначено нинішні ключі.
    place_sig: Mapped[str | None] = mapped_column(String(16))
    # Значення КВАРТИРИ на кожному її оголошенні (рядок без квартири — власні ключі):
    # фільтр «Район»/«ЖК» і лічильники списку беруть їх із покривного індексу
    # ix_listings_place без з'єднання з properties. Кеш: пише лише dedup._sync_rows.
    row_district: Mapped[str | None] = mapped_column(String(48))
    row_complex: Mapped[str | None] = mapped_column(String(64))
    row_area: Mapped[str | None] = mapped_column(String(8))

    # --- Робочий процес -------------------------------------------------------
    # «Взято в обробку» — позначка користувача про те, що об'єктом займаються.
    # Свідомо окреме поле, а не `manual_active`: те відповідає за життєвий цикл
    # оголошення (чи воно ще продається) і читається шаром контролю якості при
    # розрахунку порогів. Змішати їх означало б зламати детекцію знятих.
    in_progress: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    in_progress_at: Mapped[datetime | None] = mapped_column(DateTime)

    # --- Контроль якості ------------------------------------------------------
    # Запис не потрапляє у видачу, доки не пройшов перевірку. `pending` —
    # щойно зібраний, `ok` — чистий, `review` — підозрілий і чекає людину,
    # `rejected` — не пускаємо, але й не видаляємо.
    # Без окремого індексу (D50): див. коментар біля is_active.
    quality_status: Mapped[str] = mapped_column(String(16), default="pending")
    quality_reason: Mapped[str | None] = mapped_column(Text)
    quality_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Прапорці якості даних — щоб не видавати оцінку за факт.
    price_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    detail_enriched: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_extracted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Майстер-запис, до якого належить оголошення (міжплатформна дедуплікація).
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"), index=True, nullable=True
    )

    raw: Mapped[dict | None] = mapped_column(JSON)

    # Сильні ознаки квартири й будинку від джерела (див. realty/identity.py):

    # id квартири/групи дублів, id будинку, ЖК, корпус, координати, продавець.

    identity: Mapped[dict | None] = mapped_column(JSON)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # «Коли востаннє бачили оголошення У СТРІЧЦІ ДЖЕРЕЛА». Ставить лише збір
    # (`pipeline._upsert`). Раніше тут стояв `onupdate`, і дату ставив будь-який
    # запис у рядок: контроль якості, перевірка актуальності, перебудова
    # квартир. Через це всі 2 681 зняті оголошення мали «бачили» ПІСЛЯ дати
    # зняття, а діагностика «давно не бачили» не знаходила жодного (D43).
    # Чи існує оголошення — окреме поле `last_alive_at` (пряма перевірка).
    # Індекс (Блок 2, D50): «оновлено …» у шапці списку — max(last_seen) — без
    # повного проходу таблиці.
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Listing {self.source}:{self.external_id} {self.price_usd}$ {self.rooms}к>"


# --- Індекси списку (Блок 2, крок E4, D50) -------------------------------------
# Перше поле обох — ВИРАЗ чинної актуальності, той самий `effective_active()`,
# що стоїть у запитах сайту: SQLite бере індекс за виразом, лише якщо вираз у
# запиті збігається з ним, тож запити кроків циклу («is_active IS 1 AND
# quality_status IN (…)») цих індексів не бачать і їхній план не змінюється
# (перевірено в прототипі: жодного перехоплення). Без ANALYZE — свідомо.
#
# ix_listings_visible — покривний для запиту id списку (`queries.list_ids_select`):
# усі поля фільтрів і сортувань, тож список id для будь-якого фільтра береться з
# індексу без читання рядків таблиці (замір прототипу: 6,5–10,5 мс на M4 замість
# 7 повних проходів 94-МБ таблиці на кожен перегляд «/»).
# ix_listings_keeper — для представника квартири (max(id) серед чистих і
# актуальних оголошень квартири, GROUP BY property_id).
# Нові фільтри Блоків 3/4 мають або ввійти сюди, або пройти тест-охоронець
# планів (tests/test_list_plans.py) — інакше список знову піде в сканування.
Index("ix_listings_visible", effective_active(), Listing.quality_status,
      Listing.property_id, Listing.in_progress, Listing.source, Listing.rooms,
      Listing.condition, Listing.market_type, Listing.price_usd, Listing.price_per_sqm,
      Listing.published_at)
Index("ix_listings_keeper", Listing.property_id, Listing.quality_status, effective_active())
# Ярус «зникли з переліку» (Блок 1, E8, D52): частковий індекс — лише позначені
# рядки (інтеграція, конфлікт 1: окремих індексів із малою кількістю значень не
# додаємо; запити списку й кроків циклу цього індексу не бачать — умова інша).
Index("ix_listings_absent_since", Listing.absent_since,
      sqlite_where=Listing.absent_since.isnot(None))
# Фільтри «Район», «ЖК», «тільки місто» і їхні лічильники (Блок 4, схема S4, E10, D57):
# ПОКРИВНИЙ для запиту id списку з цими фільтрами й для GROUP BY лічильників — ті самі
# поля, що й ix_listings_visible, плюс row_*. План інтеграції називав складений
# (row_district, row_complex); охоронець планів Блоку 2 (tests/test_list_plans.py)
# вимагає для запиту id лише покривного індексу, тож row_* стоять тут одразу після
# виразу актуальності й якості (рівність за районом звужує прохід). Без фільтрів місця
# планувальник лишається на вужчому ix_listings_visible (перевіряє той самий тест).
Index("ix_listings_place", effective_active(), Listing.quality_status, Listing.row_district,
      Listing.row_complex, Listing.row_area, Listing.property_id, Listing.in_progress,
      Listing.source, Listing.rooms, Listing.condition, Listing.market_type,
      Listing.price_usd, Listing.price_per_sqm, Listing.published_at)


# --- Слухачі ORM: ключ site_key і телефони (крок E6, D51) --------------------------------------
# Одне місце для ВСІХ шляхів запису через ORM (інтеграція, конфлікт
# «pipeline._upsert»): збір (`Pipeline._upsert` — нові й наявні рядки), дозбір
# (`backfill`), LLM-фолбек, перевірка за посиланням Блоку 5 (той самий
# `Pipeline._write`), дії сайту. DML через сесію (`update(Listing)` тощо) об'єктів
# не має — для нього запаска do_orm_execute нижче; Core-записи повз сесію
# (Connection.execute, bulk_*) — перелік і колонки стереже
# tests/test_listing_write_paths.py.
#
# Телефони (рішення власника 5, D46): подія «set» на description і title — номер
# замінюється в значенні, ЯКЕ ЗАПИСУЄТЬСЯ (і в конструкторі Listing(...), і в
# setattr). Старі рядки, яких ніхто не переписує, не змінюються: разова заміна —
# окремий крок зі свіжим бекапом (`cli.py privacy apply`, E7). Зламаний
# config/privacy.toml зупиняє запис тексту (ConfigError), а не пропускає номер.
#
# site_key: before_insert — завжди з original_url; before_update — якщо змінився
# original_url чи сам ключ або ключа ще немає (поле похідне: ключ завжди
# відповідає адресі). Зламаний config/links.toml ключа не ставить (журнал), але
# й запису не зупиняє: ключ — довідкове поле, а дії власника не мають падати.

_key_log = logging.getLogger("realty.links")
_key_error_logged: set[str] = set()


def _key_for(url: str | None) -> tuple[bool, str | None]:
    """(чи вдалося обчислити, ключ)."""
    from . import links
    from .configfiles import ConfigError

    try:
        return True, links.site_key(url)
    except ConfigError as e:
        text = str(e)
        if text not in _key_error_logged:
            _key_error_logged.add(text)
            _key_log.error("site_key не обчислено — config/links.toml не проходить "
                           "перевірку:\n%s", text)
        return False, None
    except Exception as e:                           # noqa: BLE001 — ключ довідковий
        # Розбір обіцяє Link або NotALink; якщо ні — помилка в розборі, а не в записі.
        # У журнал — лише тип: у тексті винятку буває адреса (піддомен агенції
        # rieltor буває номером телефону, D51).
        name = type(e).__name__
        if name not in _key_error_logged:
            _key_error_logged.add(name)
            _key_log.error("site_key не обчислено: %s у links.site_key (адресу не друкуємо)",
                           name)
        return False, None


def _redact_on_set(target, value, oldvalue, initiator):
    from . import privacy

    return privacy.redact_field(initiator.key, value)


def _redact_dirty(target, *, only_changed: bool) -> None:
    from sqlalchemy import inspect as sa_inspect

    from . import privacy

    state = sa_inspect(target)
    for name in ("description", "title"):
        if only_changed and not state.attrs[name].history.has_changes():
            continue
        value = state.dict.get(name)
        cleaned = privacy.redact_field(name, value)
        if cleaned != value:
            setattr(target, name, cleaned)


def _listing_before_insert(_mapper, _connection, target) -> None:
    _redact_dirty(target, only_changed=False)
    ok, key = _key_for(target.original_url)
    if ok:
        target.site_key = key


def _listing_before_update(_mapper, _connection, target) -> None:
    from sqlalchemy import inspect as sa_inspect

    _redact_dirty(target, only_changed=True)
    attrs = sa_inspect(target).attrs
    if (target.site_key is None or attrs.original_url.history.has_changes()
            or attrs.site_key.history.has_changes()):
        ok, key = _key_for(target.original_url)
        if ok and key != target.site_key:
            target.site_key = key


event.listen(Listing.description, "set", _redact_on_set, retval=True)
event.listen(Listing.title, "set", _redact_on_set, retval=True)
event.listen(Listing, "before_insert", _listing_before_insert)
event.listen(Listing, "before_update", _listing_before_update)


# --- Запаска: DML через сесію (рев'ю E6, D51) -------------------------------------------------
# Слухачі вище бачать лише об'єкти Listing. Оператори через сесію —
# `session.execute(update(Listing).values(...))`, `session.execute(insert(Listing),
# [...])`, оновлення за первинним ключем `session.execute(update(Listing), [...])`,
# `session.query(Listing).update({...})`, `Listing.__table__.update()` — їх обходять.
# Подія сесії do_orm_execute ловить їх усі: значення description/title проходять ту
# саму заміну телефонів, а для original_url ставиться site_key. Значення, яке не
# можна перевірити (вираз SQL, INSERT … SELECT, ON CONFLICT, кілька рядків VALUES),
# і сирий SQL через сесію, що пише ці колонки, — помилка ListingWriteError, а не
# тихий пропуск номера. Повз сесію (Connection.execute, bulk_*_mappings) подія не
# проходить — ці шляхи перелічує й стереже tests/test_listing_write_paths.py.

_GUARDED = ("description", "title", "original_url")
_SITE_KEY_PARAM = "realty_guard_site_key"


class ListingWriteError(RuntimeError):
    """Запис у listings, який не можна перевірити на телефони й site_key."""


def _bound_value(value, param_keys: set[str]):
    """('param', ключ) | ('value', значення) | ('unknown', None) для значення з .values()."""
    from sqlalchemy.sql.elements import BindParameter, Null

    if isinstance(value, BindParameter):
        if value.key in param_keys:
            return "param", value.key
        if value.callable is not None:
            return "unknown", None
        return "value", value.value
    if value is None or isinstance(value, Null):
        return "value", None
    if isinstance(value, (str, int, float)):
        return "value", value
    return "unknown", None


def _guard_listing_dml(state):
    """do_orm_execute: UPDATE/INSERT у listings через сесію → та сама заміна й ключ."""
    import re

    from sqlalchemy import bindparam
    from sqlalchemy.sql.elements import TextClause

    stmt = state.statement
    if isinstance(stmt, TextClause):
        sql = stmt.text
        if re.search(r"(?is)\b(?:update|insert|replace)\b(?:\s+or\s+\w+)?(?:\s+into)?\s+"
                     r"[\"'`\[]?listings\b", sql) and \
                re.search(r"(?i)\b(?:description|title|original_url)\b", sql):
            raise ListingWriteError(
                "сирий SQL через сесію пише description/title/original_url у listings — "
                "в обхід заміни телефонів і site_key; пишіть через об'єкт Listing")
        return None
    if not (state.is_update or state.is_insert):
        return None
    table = getattr(stmt, "table", None)
    if getattr(table, "name", None) != Listing.__tablename__:
        return None
    from . import privacy

    params = state.parameters
    many = isinstance(params, (list, tuple))
    rows = list(params) if many else ([params] if params else [])
    param_keys = {k for r in rows for k in r}

    def refuse(what: str):
        raise ListingWriteError(f"listings: {what} — значення не перевірити на телефони "
                                f"й site_key; пишіть через об'єкт Listing")

    if getattr(stmt, "select", None) is not None and \
            set(getattr(stmt, "_select_names", None) or ()) & set(_GUARDED):
        refuse("INSERT … SELECT у description/title/original_url")
    post = getattr(stmt, "_post_values_clause", None)
    if post is not None and {getattr(k, "key", k) for k in
                             (getattr(post, "update_values_to_set", None) or ())} & set(_GUARDED):
        refuse("ON CONFLICT DO UPDATE у description/title/original_url")
    for multi in getattr(stmt, "_multi_values", None) or ():
        for row in multi:
            names = {getattr(k, "key", k) for k in (row if isinstance(row, dict) else ())}
            if not isinstance(row, dict) or names & set(_GUARDED):
                refuse("VALUES на кілька рядків")

    # Значення в .values(...): літерал — перевіряємо тут; bindparam — у параметрах.
    new_values: dict = {}
    key_col = None
    via_params: dict[str, str] = {}          # ключ параметра → поле
    for key, value in (getattr(stmt, "_values", None) or {}).items():
        name = key if isinstance(key, str) else getattr(key, "key", None)
        if name == "site_key":
            key_col = key
        if name not in _GUARDED:
            continue
        kind, raw = _bound_value(value, param_keys)
        if kind == "unknown":
            refuse(f"{name} = вираз SQL")
        if kind == "param":
            via_params[raw] = name
            continue
        if name == "original_url":
            ok, site = _key_for(raw)
            if ok:
                new_values["site_key"] = site
        else:
            cleaned = privacy.redact_field(name, raw)
            if cleaned != raw:
                new_values[key] = cleaned
    if "site_key" in new_values and key_col is not None and key_col != "site_key":
        new_values[key_col] = new_values.pop("site_key")

    # Параметри (executemany й оновлення за первинним ключем): імена — атрибути
    # або ключі bindparam із .values(); original_url через bindparam → site_key
    # теж через bindparam (свій для кожного рядка).
    url_param = next((k for k, n in via_params.items() if n == "original_url"), None)
    if url_param is not None:
        new_values[key_col if key_col is not None else "site_key"] = bindparam(_SITE_KEY_PARAM)
    new_rows, changed = [], False
    for r in rows:
        upd = {}
        for pkey, value in r.items():
            name = via_params.get(pkey, pkey)
            if name in ("description", "title"):
                cleaned = privacy.redact_field(name, value)
                if cleaned != value:
                    upd[pkey] = cleaned
            elif name == "original_url" and pkey == url_param:
                upd[_SITE_KEY_PARAM] = _key_for(value)[1]
            elif name == "original_url":
                ok, site = _key_for(value)
                if ok and r.get("site_key") != site:
                    upd["site_key"] = site
        new_rows.append(upd)
        changed = changed or bool(upd)
    if not new_values and not changed:
        return None
    new_stmt = stmt.values(new_values) if new_values else stmt
    new_params = None
    if changed:
        new_params = new_rows if many else new_rows[0]
    return state.invoke_statement(statement=new_stmt, params=new_params)


event.listen(Session, "do_orm_execute", _guard_listing_dml)


class Property(Base):
    """Один об'єкт нерухомості — незалежно від того, на скількох сайтах він є.

    Створюється модулем `realty.dedup` зі згрупованих оголошень; посилання на
    всі оригінали доступні через `listings`.
    """

    __tablename__ = "properties"
    __table_args__ = (Index("ix_property_shape", "rooms", "area_total"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    rooms: Mapped[int | None] = mapped_column(Integer)
    area_total: Mapped[float | None] = mapped_column(Float)
    floor: Mapped[int | None] = mapped_column(Integer)
    floors_total: Mapped[int | None] = mapped_column(Integer)
    street: Mapped[str | None] = mapped_column(String(256))
    house: Mapped[str | None] = mapped_column(String(32))
    district: Mapped[str | None] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(String(512))

    # Розкид цін між майданчиками — сам собою корисний сигнал.
    price_usd_min: Mapped[float | None] = mapped_column(Float)
    price_usd_max: Mapped[float | None] = mapped_column(Float)
    price_per_sqm: Mapped[float | None] = mapped_column(Float)

    market_type: Mapped[MarketType] = mapped_column(
        Enum(MarketType, native_enum=False), default=MarketType.UNKNOWN
    )
    condition: Mapped[Condition] = mapped_column(
        Enum(Condition, native_enum=False), default=Condition.UNKNOWN
    )
    sources_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Найсвіжіше «бачили» серед оголошень квартири — ставить перебудова.
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Район і ЖК квартири (Блок 4, схема S4, E10, D57): найсильніший ступінь серед її
    # оголошень, усередині — більшість (places.resolve.property_place); нічия — NULL і
    # place_conflict. Кеш квартири, як і решта полів: будує зведення й крок «райони й ЖК».
    # Аналітика порівнює «схожі» за district_key (а не за сирим district із POI LUN).
    district_key: Mapped[str | None] = mapped_column(String(48))
    complex_key: Mapped[str | None] = mapped_column(String(64))
    place_area: Mapped[str | None] = mapped_column(String(8))
    place_conflict: Mapped[dict | None] = mapped_column(JSON)

    listings: Mapped[list["Listing"]] = relationship(
        "Listing", backref="property", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Property {self.rooms}к {self.area_total}м² "
                f"{self.street or '?'} × {self.sources_count} джерел>")


class DataReport(Base):
    """Скарга користувача на те, що дані не збігаються з оголошенням.

    Одне натискання, без форми. Сенс не в тому, щоб завести чергу заявок, а в
    тому, щоб накопичити список підтверджених помилок: по ньому видно, які
    саме правила класифікації ламаються найчастіше. Це найдешевше джерело для
    наступних виправлень — дешевше за будь-який аудит, бо вказує людина, яка
    справді відкрила оголошення.

    Знімок полів на момент скарги зберігаємо тут же: дані потім зміняться, і
    без знімка буде незрозуміло, на що саме скаржились.
    """

    __tablename__ = "data_reports"
    __table_args__ = (Index("ix_report_listing", "listing_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    property_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    # Що саме не збігається, якщо людина уточнила. Порожнє значення — теж
    # відповідь: «щось не так», і цього досить, щоб запис потрапив у список.
    field: Mapped[str | None] = mapped_column(String(24))
    snapshot: Mapped[dict | None] = mapped_column(JSON)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class CheckEvent(Base):
    """Одна перевірка одного оголошення — журнал, що дописується.

    Потрібен аналізу виживання. Графік перевірок нерівномірний: пріоритет
    віддається тим, хто давно на ринку й у кого впала ціна. Нерівномірність
    сама по собі зміщує криву виживання, і єдиний спосіб її врахувати — знати
    фактичні моменти перевірок, а не вважати їх рівномірними.

    Пишеться і для незрозумілих відповідей: те, що ми не достукались, теж
    факт про графік спостережень.
    """

    __tablename__ = "check_events"
    __table_args__ = (Index("ix_check_listing", "listing_id", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    code: Mapped[int] = mapped_column(Integer)
    # True — живе, False — знято, None — не достукались.
    alive: Mapped[bool | None] = mapped_column(Boolean)
    # Чим викликана перевірка: плановий обхід чи підтвердження кандидата з
    # різниці списків. Без цього не відрізнити «дійшла черга» від «запідозрили».
    # Блок 1 (E8): ярус черги — sweep, canary, absent, reseen, repeat404, opened,
    # held, rm_sample, existence; старі candidate/opened лишаються.
    reason: Mapped[str] = mapped_column(String(16), default="sweep")
    # Вердикт класифікатора Блоку 1 (E8, D52): alive, status_410, ria_archive,
    # repeat_404, not_found, blocked, net_error, server_error, unrecognized,
    # conflict, id_mismatch, too_large. NULL — перевірка старим кодом (лише
    # код HEAD): так правило повторного 404 й покриття «новим підписом»
    # відрізняють нові перевірки від старих.
    signature: Mapped[str | None] = mapped_column(String(24))


class ListingEvent(Base):
    """Зміна актуальності оголошення: знято, повернулось, полагоджено посилання.

    Блок 1 (E8, D52), рішення власника 1–2 (D46): історію не стираємо — повернення
    очищає delisted_at, але попереднє значення лежить тут, у `evidence`. Доказ —
    код, кінцева адреса (без query, піддомени агенцій зведено до домену), ланцюжок
    переадресацій, підпис, стан сторінки DOM.RIA; тіл сторінок, імен і телефонів
    немає. Подія `removed` посилається на свою перевірку (check_event_id).
    """

    __tablename__ = "listing_events"
    __table_args__ = (Index("ix_listing_event_listing", "listing_id", "at"),
                      Index("ix_listing_event_kind_at", "kind", "at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # removed | returned | url_repaired
    kind: Mapped[str] = mapped_column(String(16))
    # removed: status_410 | ria_archive | repeat_404; returned: ярус черги
    # (rm_sample, reseen, sweep, opened, …); url_repaired: стратегія існування.
    reason: Mapped[str | None] = mapped_column(String(24))
    source: Mapped[str | None] = mapped_column(String(32))
    site_key: Mapped[str | None] = mapped_column(String(64))
    check_event_id: Mapped[int | None] = mapped_column(ForeignKey("check_events.id"),
                                                       nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON)


class PriceEvent(Base):
    """Зміна ціни в оголошенні.

    Пишеться лише тоді, коли ціна справді змінилась — повторний прогін із тією
    самою ціною запису не створює, інакше історія перетворилась би на журнал
    прогонів.
    """

    __tablename__ = "price_events"
    __table_args__ = (Index("ix_price_event_listing", "listing_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_usd: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PropertyRedirect(Base):
    """Куди тепер веде id квартири, що злилась з іншою.

    id квартир стабільні між перебудовами (див. `dedup.assign_ids`), але коли
    дві квартири виявляються однією, одна з них зникає. Старе посилання на неї
    має вести на ту, що лишилась, а не на порожню сторінку.
    """

    __tablename__ = "property_redirects"

    old_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    new_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class DedupDecision(Base):
    """Рішення власника про зведення: «це різні квартири» / «це одна квартира».

    Сильніше за будь-яке правило, і перебудова його не скасовує. Зберігається
    на рівні оголошень (id квартир між перебудовами можуть мінятись, id
    оголошень — ні). `different`: оголошення `left` і `right` — різні квартири;
    `same`: усі з `left` і `right` — одна. Нове рішення, що суперечить
    старому, вимикає старе (`active=False`), — діє останнє слово власника.
    """

    __tablename__ = "dedup_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    left: Mapped[list] = mapped_column(JSON)
    right: Mapped[list] = mapped_column(JSON, default=list)
    property_id: Mapped[int | None] = mapped_column(Integer)     # звідки ухвалено
    other_property_id: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
