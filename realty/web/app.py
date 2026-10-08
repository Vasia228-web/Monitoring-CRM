"""Веб-інтерфейс: таблиця оголошень із сортуванням і фільтрами."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import logging

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .. import configfiles
from ..dedup import resolve_property_id
from ..db import SessionLocal, init_db, tune_for_web
from ..liveness import ui as liveness_ui
from ..ops import init_ops
from ..models import (
    Condition, Listing, MarketType, PriceEvent, Property, effective_active, is_clean,
)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
log = logging.getLogger(__name__)


def _relative_date(value) -> str:
    """«сьогодні» / «3 дні тому» / «12.04.2026» — залежно від давності."""
    if value is None:
        return "—"
    days = (datetime.now() - value).days
    if days < 0:
        return value.strftime("%d.%m.%Y")
    if days == 0:
        return "сьогодні"
    if days == 1:
        return "вчора"
    if days < 7:
        return f"{days} дні тому" if days < 5 else f"{days} днів тому"
    if days < 31:
        weeks = days // 7
        return f"{weeks} тиж. тому"
    return value.strftime("%d.%m.%Y")


def _money(value) -> str:
    """Розряди — вузькими нерозривними пробілами, щоб число не ламалось."""
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", "\u202f")


def _plural(count, one: str, few: str, many: str) -> str:
    """Українська форма числівника: 1 об'єкт, 2 об'єкти, 5 об'єктів.

    Числа в інтерфейсі читає людина, і «2 змін» одразу виглядає як недогляд —
    а недогляд у підписі підриває довіру до самої цифри.
    """
    n = abs(int(count or 0))
    if n % 100 in range(11, 15):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


from .navstate import carry, reset_url  # noqa: E402
from .pagination import PAGE_SIZES, build as build_page  # noqa: E402
from ..quality.rules import SEGMENT_MIN_SAMPLE, load_thresholds  # noqa: E402

# Доступні в кожному шаблоні: навігація має нести стан, а не скидати його.
templates.env.globals["carry"] = carry
templates.env.globals["reset_url"] = reset_url
# Позначка «актуальність не підтверджена» біля оголошень із хостів, які не
# перевіряються (Благо; Блок 1, E8, D52) — для картки квартири.
templates.env.globals["liveness_marker"] = liveness_ui.marker_for_url

templates.env.filters["relative_date"] = _relative_date
templates.env.filters["money"] = _money
templates.env.filters["plural"] = _plural

def _speed_or_none():
    """config/speed.toml або None: зламаний конфіг не має зупинити сайт (D49 п. 7)."""
    try:
        return configfiles.get("speed")
    except configfiles.ConfigError as e:
        log.error("config/speed.toml не читається — сайт без налаштувань швидкості: %s", e)
        return None


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_ops()
    warn_if_open()
    # Фоновий потік сайту (Блок 2, D49–D50): журнал часу й маячок, перегляди
    # карток і last_seen сесій — пакетами; раз на `generations.poll_s` —
    # покоління даних і стан циклу. Запит лише кладе рядок у буфер; при
    # зупинці сайту буфер дописується.
    from .perf import WRITER
    WRITER.start()
    # Потік перевірок при відкритті — одразу: завдання, що лишились із часу до
    # перезапуску (відкладені на цикл чи не взяті), отримають процес перевірки,
    # не чекаючи, поки хтось відкриє квартиру.
    from .livecheck import LIVE
    LIVE.start()
    # Шаблони компілюються при першому рендері (~десятки мс на Fedora) — нехай
    # це станеться до першого запиту, а не в ньому. Знімок «Аналітики» й типові
    # списки рахуються фоном: перша людина після перезапуску їх не чекає.
    for name in ("index.html", "processing.html", "analytics.html", "property.html",
                 "status.html", "property_missing.html", "login.html", "places.html"):
        try:
            templates.env.get_template(name)
        except Exception as e:                      # noqa: BLE001 — лише прогрів
            log.warning("шаблон %s не скомпілювався заздалегідь: %s", name, e)
    from . import speedcache
    speedcache.warm()
    speedcache.BACKGROUND.submit("warm-analytics", _warm_analytics)
    try:
        yield
    finally:
        WRITER.stop()


def _warm_analytics() -> None:
    from ..analytics import cache

    with SessionLocal() as s:
        cache.get(s)


# Документація API — лише власнику (auth.OWNER_ONLY; D55 п. 5, D58): oauth2-redirect
# Swagger UI — під тим самим префіксом /api/docs, а не окремим /docs/….
app = FastAPI(title="Нерухомість Івано-Франківська", docs_url="/api/docs",
              swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
              lifespan=lifespan)

# Кеш сторінок SQLite для з'єднань сайту (config/speed.toml, sqlite.cache_kb).
if (_cfg := _speed_or_none()) is not None:
    tune_for_web(_cfg.sqlite.cache_kb)

# Сторінка стану системи та ручне управління збором.
from .status import router as status_router  # noqa: E402

app.include_router(status_router)

# Аналітичний шар: сегменти й сторінка окремої квартири.
from .analytics_routes import router as analytics_router  # noqa: E402

app.include_router(analytics_router)

# «Райони й ЖК» (Блок 4, E10, D57): розподіл за районами й ЖК — обидві ролі.
from .places_routes import router as places_router  # noqa: E402

app.include_router(places_router)

# Ручне виправлення зведення квартир (лише власник).
from .dedup_routes import router as dedup_router  # noqa: E402

app.include_router(dedup_router)

# Стан перевірки при відкритті квартири (Блок 2, крок E5): обидві ролі.
from .livecheck import router as livecheck_router  # noqa: E402

app.include_router(livecheck_router)

# Пошук за посиланням (Блок 5, крок E14, D59): поле у верхній панелі, /find,
# «Перевірити зараз» — обидві ролі. Поле бере лише конфіг (без запиту до бази).
from .find_routes import router as find_router, ui_config as find_ui  # noqa: E402

app.include_router(find_router)
templates.env.globals["find_ui"] = find_ui

# Стиснення на origin (Блок 2, крок E5, D50): сторінка списку 124 → ~12 КБ через
# тунель, «Аналітика» 64 → ~10,5 КБ (план Блоку 2, D48); ~5 мс процесора на
# Fedora. Поріг і рівень — config/speed.toml [gzip]; читаються на старті (зміна —
# з перезапуском сайту); зламаний конфіг — без стиснення, як до Блоку 2.
# Шар — ВНУТРІШНІЙ (додано першим), а не зовнішній, як писав план: зовні від
# BaseHTTPMiddleware (вхід) тіло приходить потоком, і GZipMiddleware стискав би
# навіть 70-байтні відповіді кнопок, ігноруючи поріг (виявив тест). Ціна —
# Server-Timing включає ~5 мс стиснення: це теж час сервера.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

if (_cfg := _speed_or_none()) is not None:
    app.add_middleware(GZipMiddleware, minimum_size=_cfg.gzip.min_bytes,
                       compresslevel=_cfg.gzip.level)

# Захист усього інтерфейсу. Вмикається наявністю AUTH_USER/AUTH_PASSWORD,
# тож локальна розробка не потребує пароля, а публічний хостинг — потребує.
from .auth import (  # noqa: E402
    AuthMiddleware, current_role, robots_txt, router as auth_router, warn_if_open,
)

app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
templates.env.globals["current_role"] = current_role

# Вимірювання швидкості (Блок 2, крок E2, D49): заголовок Server-Timing і журнал
# часу на кожну відповідь, маячок браузера, зведення для власника. Проміжний
# шар додано ОСТАННІМ — отже, він зовнішній і міряє весь шлях запиту разом із
# перевіркою входу. Поблажок для 127.0.0.1 немає: тунель теж приходить звідти.
from .perf import ServerTimingMiddleware, router as perf_router, rum_for  # noqa: E402

app.add_middleware(ServerTimingMiddleware)
app.include_router(perf_router)
templates.env.globals["rum_for"] = rum_for


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return robots_txt()


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Порожня відповідь замість 401: іконку браузер просить без пароля."""
    return Response(status_code=204)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Перевірка живучості для хостингу — без пароля й без звернень до бази.

    `async def` — у циклі подій, не в пулі потоків: пул, зайнятий повільними сторінками
    під час циклу збору, не має робити сайт «мертвим» для сторожа (рецензія W3, 08.10)."""
    return {"ok": True}

def _num(value: str | None) -> float | None:
    """Порожнє поле форми приходить як `price_min=` — це не число, але й не
    помилка: користувач просто не заповнив фільтр."""
    if value is None or not str(value).strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


from . import speedcache  # noqa: E402
from .queries import (  # noqa: E402
    DEFAULT_SORT, MAX_ROWS, SORTS, list_ids_select, listing_query, page_rows_select,
    visible_now,
)


def _median(values: list[float]) -> float | None:
    """Медіана, а не середнє: на ринку нерухомості кілька дорогих об'єктів
    зміщують середнє так, що воно перестає описувати типову пропозицію."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _stats(session) -> dict:
    total = session.scalar(select(func.count(Listing.id))) or 0
    by_source = dict(
        session.execute(select(Listing.source, func.count(Listing.id))
                        .group_by(Listing.source)).all()
    )
    # Медіани рахуємо лише по записах, що пройшли контроль: інакше викиди
    # й сміття тягнуть за собою всі оцінки.
    sqm = list(session.scalars(select(Listing.price_per_sqm)
                               .where(Listing.price_per_sqm.isnot(None), is_clean())))
    prices = list(session.scalars(select(Listing.price_usd)
                                  .where(Listing.price_usd.isnot(None), is_clean())))
    median_sqm = _median(sqm)
    updated = session.scalar(select(func.max(Listing.last_seen)))
    inactive = session.scalar(
        select(func.count()).select_from(Listing).where(effective_active().is_(False))
    ) or 0
    properties = session.scalar(select(func.count()).select_from(Property)) or 0
    multi = session.scalar(select(func.count()).select_from(Property)
                           .where(Property.sources_count > 1)) or 0
    quality_counts = dict(session.execute(
        select(Listing.quality_status, func.count()).group_by(Listing.quality_status)).all())
    return {
        "total": total,
        "quality": quality_counts,
        "properties": properties,
        "multi_source": multi,
        "inactive": inactive,
        "by_source": by_source,
        "median_sqm": round(median_sqm) if median_sqm else None,
        "median_price": round(_median(prices)) if prices else None,
        "updated": updated,
    }


def _cached_stats(session) -> dict:
    """Зведення шапки списку й /api/stats — раз на покоління даних (Блок 2, E5).

    Досі на КОЖЕН перегляд: 5 повних проходів таблиці й дві вибірки по ~28
    тис. значень для медіан. Значення спільне — не змінювати.
    """
    return speedcache.stats(session, lambda: _stats(session))


def _in_work(session) -> int:
    """Скільки оголошень «в обробці» — живим запитом за ix_listings_in_progress.

    Не кешується: значок у навігації має змінитись на НАСТУПНОМУ ж відкритті
    після «взяти в обробку» (умова власника), а за індексом це SEARCH, а не
    прохід таблиці.
    """
    return session.scalar(select(func.count()).select_from(Listing)
                          .where(Listing.in_progress.is_(True))) or 0


_thresholds_memo: dict = {}


def _thresholds():
    """Пороги якості — з пам'яті, доки файл той самий (час зміни й розмір).

    Досі файл читався й розбирався на кожен перегляд списку. Немає файлу —
    як і раніше, `load_thresholds` (рахує й зберігає).
    """
    from ..quality import rules

    path = rules.THRESHOLDS_FILE
    try:
        st = path.stat()
    except OSError:
        return load_thresholds()
    stamp = (str(path), st.st_mtime_ns, st.st_size)
    if _thresholds_memo.get("stamp") != stamp:
        _thresholds_memo.update(stamp=stamp, value=load_thresholds())
    return _thresholds_memo["value"]


def _page_rows(session, page_ids) -> list:
    """Рядки сторінки за id — у порядку списку id (фаза 2)."""
    if not len(page_ids):
        return []
    got = {r.id: r for r in session.scalars(page_rows_select(page_ids)) if visible_now(r)}
    return [got[i] for i in page_ids if i in got]


def _peer_comparison(row, thresholds) -> dict:
    """Наскільки об'єкт дорожчий або дешевший за схожі.

    «Схожі» — та сама кімнатність, стан і тип ринку. Якщо таких у базі замало,
    чесна відповідь — «мало схожих квартир», а не число з нізвідки. Раніше
    порівняння йшло з медіаною по всій базі, і кожен рядок виходив «вище
    медіани»: однокімнатна з ремонтом у новобудові порівнювалась із
    трикімнатним сирцем на вторинці.
    """
    value = row.price_per_sqm
    if not value:
        return {"known": False, "reason": "немає ціни за м²"}
    key = thresholds.segment_key({"rooms": row.rooms, "condition": row.condition,
                                  "market_type": row.market_type})
    entry = thresholds.segment_median_sqm.get(key)
    if not entry or entry.get("n", 0) < SEGMENT_MIN_SAMPLE or not entry.get("median"):
        return {"known": False, "reason": "мало схожих квартир для порівняння"}
    delta = round(100 * (value / entry["median"] - 1))
    return {"known": True, "delta": delta, "n": entry["n"],
            "median": round(entry["median"]),
            "direction": "дорожче" if delta > 0 else "дешевше" if delta < 0 else "як у схожих"}


def _places():
    """(довідник, правила) районів і ЖК для сайту або (None, None) — зламаний конфіг не
    валить список: фільтри місця тоді не діють (попередження), рядки — як до E10."""
    from ..places import directory

    d = directory.current()
    if d is None:
        return None, None
    return d, d.rules


def places_ready(s, dk: tuple) -> bool:
    """Чи крок «райони й ЖК» уже щось визначив (хоч один row_* не NULL).

    До першого `places assign` (він чекає перегляду вибірки власником — рецензія E10)
    сайт показує все як до E10: сирий район біля рядка, без фільтрів місця, — а не
    «район не визначено» на кожному рядку. Раз на покоління; після кроку — один крок
    покритого індексу до першого знайденого."""
    from sqlalchemy import text

    return speedcache.flag(s, "places_ready", lambda: bool(s.execute(text(
        "SELECT EXISTS (SELECT 1 FROM listings WHERE row_district IS NOT NULL "
        "OR row_complex IS NOT NULL)")).scalar()), dk=dk)


def _raw_rows(s, stmt) -> list:
    """Рядки Core-запиту курсором драйвера, без обробки рядків SQLAlchemy (для 12–22
    тис. рядків «id + row_*» — ≈2 мс із 19; значення — int і str, перетворювати нічого)."""
    res = s.connection().execute(stmt)
    try:
        return res.cursor.fetchall()
    finally:
        res.close()


def _build_place_map() -> tuple[tuple, dict]:
    """(ключ даних, id → (row_district, row_complex, row_area)) рядків, які можуть бути
    в списку: чисті й актуальні (ті самі умови, що й у list_ids_select). Лише фон
    (прогрів після покоління; speedcache.place_map_schedule) — на шляху запиту ні."""
    from ..models import Listing, effective_active, is_clean

    with SessionLocal() as s:
        dk = speedcache.data_key(s)
        interned: dict = {}
        m = {}
        for lid, d_, c_, a_ in _raw_rows(s, select(
                Listing.id, Listing.row_district, Listing.row_complex, Listing.row_area)
                .where(is_clean(), effective_active().is_(True))):
            t = (d_, c_, a_)
            m[lid] = interned.setdefault(t, t)
    return dk, m


def _ids_with_groups(s, stmt) -> tuple[list, list]:
    """Список id і групи лічильників ОДНИМ проходом: row_* поруч з id у тому самому
    запиті (покривний ix_listings_place) — замість окремого GROUP BY, коли карти id →
    row_* ще немає (холодний «/»: +2–4 мс замість +7 мс на M4; рецензія E10)."""
    from collections import Counter

    from ..models import Listing

    rows = _raw_rows(s, stmt.with_only_columns(
        Listing.id, Listing.row_district, Listing.row_complex, Listing.row_area))
    counts = Counter((r[1], r[2], r[3]) for r in rows)
    return [r[0] for r in rows], [(*k, n) for k, n in counts.items()]


def _groups_from_ids(m: dict, ids) -> list | None:
    """Групи лічильників за готовим списком id (той самий список, що й рядки сторінки).
    None — у карті бракує id (карта з іншого моменту) — тоді GROUP BY."""
    from collections import Counter

    try:
        return [(*k, n) for k, n in Counter(m[i] for i in ids).items()]
    except KeyError:
        return None


def _base_groups(s, base_query: dict, base_key: tuple, ids=None, *, dk: tuple,
                 groups=None) -> tuple:
    """Лічильники фільтрів місця (Блок 4, E10, D57) — групи вибірки БЕЗ фільтрів місця.

    Кеш за ключем даних `dk` (прочитаним запитом один раз — speedcache.data_key) і
    фільтром. Якщо обчислювати: є карта id → row_* (будує лише фон) і список id
    вибірки без фільтрів місця (цього ж перегляду або з кешу) — Counter за картою; інакше
    один GROUP BY над тим самим запитом, а карта ставиться у фон. `groups` — уже
    пораховані тим самим проходом, що й список id (_ids_with_groups). Замір на копії
    (M4): Counter за 12 355 id — 0,6 мс; GROUP BY — ≈7 мс; карта — ≈10 мс, лише фоном
    (рецензія E10).
    """
    def compute():
        if groups is not None:
            if speedcache.place_map_peek(dk) is None:
                speedcache.place_map_schedule(_build_place_map)
            return groups
        m = speedcache.place_map_peek(dk)
        if m is None:
            speedcache.place_map_schedule(_build_place_map)
        else:
            base_ids = ids if ids is not None else speedcache.cached_list_ids(
                s, base_key, dk=dk)
            if base_ids is not None:
                got = _groups_from_ids(m, base_ids)
                if got is not None:
                    return got
        from ..places import facets
        return [tuple(r) for r in s.execute(
            facets.grouped_select(list_ids_select(**base_query))).all()]

    return speedcache.facets(s, base_key, compute, dk=dk)


def _place_rows(rows, d, rules) -> dict | None:
    """Підпис місця біля рядка: район (з довідника), ЖК, «громада»/«поза громадою».

    Район квартири, а не сирий district (у LUN там найближчий POI — «ТЦ Арсен»)."""
    if d is None:
        return None
    from ..places.directory import NONE

    out = {}
    for r in rows:
        label = d.label(r.row_district)
        area = r.row_area
        tag = (rules.labels.hromada if area == "hromada" else
               rules.labels.outside if area == "outside" else None)
        cplx = d.complex_label(r.row_complex) if r.row_complex != NONE else None
        out[r.id] = {"district": label,
                     "complex": d.complex_display(r.row_complex) if cplx else None,
                     "tag": tag,
                     "unknown": rules.labels.unknown_district if not label else None}
    return out


def _render_list(request: Request, template: str, *, in_progress: bool | None,
                 condition: str, market: str, source: str, rooms: str,
                 price_min: str | None, price_max: str | None, sort: str,
                 page: str | None = None, per_page: str | None = None,
                 all_ads: str = "", path: str = "/", district: str = "",
                 complex_: str = "", area: str = ""):
    """Спільна збірка будь-якої сторінки зі списком оголошень."""
    from ..places import facets
    from ..places.directory import NONE, UNKNOWN

    lo, hi = _num(price_min), _num(price_max)
    warning = None
    if lo is not None and hi is not None and lo > hi:
        # Порожній список без пояснення виглядає як поломка, а не як фільтр.
        warning = (f"Ціна «від» (${lo:,.0f}) більша за «до» (${hi:,.0f}) — "
                   f"нічого не може потрапити в такий діапазон.")
        lo = hi = None
    # Район, ЖК, «тільки місто» (Блок 4, E10, D57): невідомий ключ — попередження, і
    # фільтр не діє (як «від» > «до»).
    d, rules = _places()
    sel, place_warnings = facets.selection(d, rules, district=district, complex_=complex_,
                                           area=area)
    if place_warnings:
        warning = " ".join(filter(None, [warning, *place_warnings]))

    collapse = all_ads != "1"
    # Відхилення ціни за м² рахується в межах сегмента, а не по всій базі.
    # Медіана по всій базі змішує однокімнатні з п'ятикімнатними, новобудови
    # з сирцем і ремонт із його відсутністю — через це кожен рядок показував
    # «+55%», «+76%», і цифра переставала щось означати: якщо всі вище
    # медіани, це вже не порівняння.
    thresholds = _thresholds()
    # Список у дві фази (Блок 2, крок E5, D50): упорядковані id усіх рядків
    # фільтра — з покривного індексу й з кешу за поколінням даних; лічильник
    # «за фільтром» — їх кількість, сторінка — зріз. Рядки сторінки — за id.
    # Той самий фільтр і порядок, що й досі, тож і рядки, і лічильник, і
    # нумерація ті самі (scripts/page_equality.py: 0 відмінностей).
    query = dict(condition=condition, market=market, source=source, rooms=rooms,
                 price_min=lo, price_max=hi, sort=sort, in_progress=in_progress,
                 collapse=collapse)
    place_state = {"district": sel.district, "complex": sel.complex, "area": sel.area}
    key = speedcache.list_key({**query, **place_state, "all_ads": all_ads},
                              in_progress=in_progress, collapse=collapse)
    place = None
    base_key = speedcache.list_key(
        {**query, "district": "", "complex": "", "area": "", "all_ads": all_ads,
         "sort": DEFAULT_SORT}, in_progress=in_progress, collapse=collapse)
    with SessionLocal() as s:
        # Ключ даних — один на запит: список id, лічильники й варіанти з одного покоління.
        dk = speedcache.data_key(s)
        if d is not None and not places_ready(s, dk):
            # Райони й ЖК ще не визначались: сторінка — як до E10.
            if sel.active:
                warning = " ".join(filter(None, [warning, "Райони й ЖК ще не визначено — "
                                                 "фільтр за місцем не застосовано."]))
            d = rules = None
            sel = facets.Selection()
            key = speedcache.list_key({**query, "district": "", "complex": "", "area": "",
                                       "all_ads": all_ads}, in_progress=in_progress,
                                      collapse=collapse)
        if d is not None and sel.district and sel.complex not in ("", NONE, UNKNOWN):
            # Без JS зміна району не скидає ЖК: ЖК, якого у вибраному районі немає,
            # скидаємо самі — інакше число біля району (без ЖК) ≠ видачі (район ∩ ЖК).
            groups = _base_groups(s, {**query, "sort": DEFAULT_SORT}, base_key, dk=dk)
            in_district = facets.predicate(sel)
            if not any(n for rd, rc, ra, n in groups if in_district(rd, rc, ra)):
                warning = " ".join(filter(None, [
                    warning, "ЖК скинуто — його немає у вибраному районі."]))
                sel = facets.without_complex(sel)
                key = speedcache.list_key(
                    {**query, "district": sel.district, "complex": "", "area": sel.area,
                     "all_ads": all_ads}, in_progress=in_progress, collapse=collapse)
        hint: dict = {}

        def compute_ids():
            stmt = list_ids_select(**query, place=sel)
            if (d is not None and not sel.active and collapse
                    and speedcache.place_map_peek(dk) is None
                    and speedcache.facets_peek(base_key, dk) is None):
                # Холодно й карти немає: лічильники — з того самого проходу, що й id.
                got, hint["groups"] = _ids_with_groups(s, stmt)
                return got
            return s.execute(stmt).scalars().all()

        ids = speedcache.list_ids(s, key, compute_ids, dk=dk)
        matched = len(ids)
        pager = build_page(matched, page, per_page)
        rows = _page_rows(s, ids[pager.offset:pager.offset + pager.size])
        stats = _cached_stats(s)
        in_work = _in_work(s)
        # Благо не перевіряється (рішення власника 3, D46): позначка біля рядка
        # квартири, жодне актуальне оголошення якої не можна перевірити (E8, D52).
        unconfirmed = liveness_ui.list_rows(s, rows, collapse=collapse)
        if d is not None:
            groups = _base_groups(s, {**query, "sort": DEFAULT_SORT}, base_key,
                                  None if sel.active else ids, dk=dk,
                                  groups=hint.get("groups"))
            place = speedcache.facet_options(
                s, (base_key, sel, d.version),
                lambda: facets.options(groups, sel, d, rules), dk=dk)
    if place is not None and sel.district and sel.complex and not matched:
        # «Не в ЖК» / «не визначено» у районі, де таких немає: порожній список пояснюємо.
        warning = " ".join(filter(None, [warning, "У вибраному районі немає оголошень "
                                         "цього ЖК — скиньте ЖК."]))
    peers = {row.id: _peer_comparison(row, thresholds) for row in rows}
    return templates.TemplateResponse(request, template, {
        "rows": rows, "stats": stats, "sources": sorted(stats["by_source"]),
        "peers": peers, "unconfirmed": unconfirmed,
        "unconfirmed_marker": liveness_ui.marker() if unconfirmed else None,
        "matched": matched, "warning": warning, "pager": pager,
        "page_sizes": PAGE_SIZES, "collapse": collapse,
        "in_work": in_work, "place": place, "place_rows": _place_rows(rows, d, rules),
        # Сортування теж є станом: без нього кнопка скидання зникала саме тоді,
        # коли вибірка вже не була типовою.
        "active_filters": any((condition, market, source, rooms, price_min,
                               price_max, sort != DEFAULT_SORT, sel.district,
                               sel.complex, sel.area)),
        "path": path,
        "f": {"condition": condition, "market": market, "source": source,
              "rooms": rooms, "district": sel.district, "complex": sel.complex,
              "area": sel.area, "price_min": price_min or "", "price_max": price_max or "",
              "sort": sort, "page": str(pager.number), "per_page": str(pager.size),
              "all_ads": all_ads},
    })


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    condition: str = Query("", description="renovated | needs_repair | unknown"),
    market: str = Query(""),
    source: str = Query(""),
    rooms: str = Query(""),
    district: str = Query(""),
    complex_: str = Query("", alias="complex"),
    area: str = Query(""),
    price_min: str | None = Query(None),
    price_max: str | None = Query(None),
    sort: str = Query(DEFAULT_SORT),
    page: str | None = Query(None),
    per_page: str | None = Query(None),
    all_ads: str = Query(""),
):
    return _render_list(request, "index.html", in_progress=None, path="/",
                        condition=condition, market=market, source=source, rooms=rooms,
                        price_min=price_min, price_max=price_max, sort=sort,
                        page=page, per_page=per_page, all_ads=all_ads,
                        district=district, complex_=complex_, area=area)


@app.get("/processing", response_class=HTMLResponse)
def processing(
    request: Request,
    condition: str = Query(""),
    market: str = Query(""),
    source: str = Query(""),
    rooms: str = Query(""),
    district: str = Query(""),
    complex_: str = Query("", alias="complex"),
    area: str = Query(""),
    price_min: str | None = Query(None),
    price_max: str | None = Query(None),
    sort: str = Query(DEFAULT_SORT),
    page: str | None = Query(None),
    per_page: str | None = Query(None),
    all_ads: str = Query(""),
):
    """Тільки об'єкти, взяті в обробку — той самий набір даних і сортування."""
    return _render_list(request, "processing.html", in_progress=True, path="/processing",
                        condition=condition, market=market, source=source, rooms=rooms,
                        price_min=price_min, price_max=price_max, sort=sort,
                        page=page, per_page=per_page, all_ads=all_ads,
                        district=district, complex_=complex_, area=area)


@app.get("/api/listings")
def api_listings(
    condition: str = "", market: str = "", source: str = "", rooms: str = "",
    price_min: str | None = None, price_max: str | None = None,
    sort: str = DEFAULT_SORT, in_progress: bool | None = None,
    district: str = "", complex_: str = Query("", alias="complex"), area: str = "",
    limit: int = Query(500, le=MAX_ROWS),
):
    """JSON-зріз тих самих даних."""
    from ..places import facets

    price_min, price_max = _num(price_min), _num(price_max)
    d, rules = _places()
    sel, warn = facets.selection(d, rules, district=district, complex_=complex_, area=area)
    if warn:
        # Невідомий район/ЖК/місцевість (друкарська помилка) — не «відфільтрований» повний
        # список, а явна відмова (рецензія E10).
        return JSONResponse({"error": " ".join(warn), "warnings": warn}, status_code=400)
    stmt = listing_query(condition=condition, market=market, source=source, rooms=rooms,
                         price_min=price_min, price_max=price_max, sort=sort,
                         in_progress=in_progress, place=sel)
    with SessionLocal() as s:
        rows = s.scalars(stmt.limit(limit)).all()
        return JSONResponse([{
            "source": r.source,
            "price": r.price,
            "currency": r.currency,
            "price_usd": r.price_usd,
            "rooms": r.rooms,
            "area_total": r.area_total,
            "location": r.location,
            "price_per_sqm": r.price_per_sqm,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "market_type": r.market_type.value,
            "condition": r.condition.value,
            "original_url": r.original_url,
            "price_estimated": r.price_estimated,
            "is_active": r.is_active,
            "manual_active": r.manual_active,
            "active": r.manual_active if r.manual_active is not None else r.is_active,
            "delisted_at": r.delisted_at.isoformat() if r.delisted_at else None,
            "in_progress": r.in_progress,
            "in_progress_at": r.in_progress_at.isoformat() if r.in_progress_at else None,
            # Район і ЖК квартири (Блок 4, E10, D57) — лише додані поля.
            "district_key": r.row_district,
            "district_label": d.label(r.row_district) if d is not None else None,
            "complex_key": r.row_complex,
            "complex_label": d.complex_label(r.row_complex) if d is not None else None,
            "place_area": r.row_area,
        } for r in rows])


@app.post("/api/listings/{listing_id}/processing")
def api_set_processing(listing_id: int, payload: dict = Body(default={})):
    """Взяти об'єкт в обробку або прибрати з неї.

    Зняття статусу нічого не видаляє: запис лишається в базі разом з історією
    цін і просто повертається в загальний список.
    """
    value = payload.get("in_progress", True)
    if value not in (True, False):
        return JSONResponse({"ok": False, "error": "in_progress має бути true або false"},
                            status_code=400)
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "оголошення не знайдено"},
                                status_code=404)
        row.in_progress = bool(value)
        row.in_progress_at = datetime.now() if value else None
        s.commit()
        speedcache.owner_changed("in_progress")
        return JSONResponse({"ok": True, "id": row.id, "in_progress": row.in_progress,
                             "in_progress_at": row.in_progress_at.isoformat()
                             if row.in_progress_at else None})


@app.post("/api/properties/{property_id}/processing")
def api_set_property_processing(property_id: int, payload: dict = Body(default={})):
    """Той самий статус, але на весь майстер-об'єкт одразу.

    Ріелтор працює з квартирою, а не з окремим оголошенням, тож на сторінці
    об'єкта одна кнопка ставить статус на всі склеєні оголошення. Як і для
    окремого оголошення, зняття статусу нічого не видаляє.
    """
    value = payload.get("in_progress", True)
    if value not in (True, False):
        return JSONResponse({"ok": False, "error": "in_progress має бути true або false"},
                            status_code=400)
    with SessionLocal() as s:
        # Старий id злитої квартири — ставимо статус тій, що лишилась.
        property_id = resolve_property_id(s, property_id) or property_id
        rows = s.scalars(select(Listing)
                         .where(Listing.property_id == property_id)).all()
        if not rows:
            return JSONResponse({"ok": False, "error": "об'єкт не знайдено"},
                                status_code=404)
        now = datetime.now() if value else None
        for row in rows:
            row.in_progress = bool(value)
            row.in_progress_at = now
        s.commit()
        speedcache.owner_changed("in_progress")
        return JSONResponse({"ok": True, "property_id": property_id,
                             "listings": len(rows), "in_progress": bool(value)})


@app.post("/api/listings/{listing_id}/status")
def api_set_status(listing_id: int, payload: dict = Body(default={})):
    """Ручна позначка актуальності.

    `active`: true — актуальна, false — неактуальна, null — зняти позначку
    й повернутись до автоматичного визначення.
    """
    value = payload.get("active", None)
    if value not in (True, False, None):
        return JSONResponse({"ok": False, "error": "active має бути true, false або null"},
                            status_code=400)
    with SessionLocal() as s:
        row = s.get(Listing, listing_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "оголошення не знайдено"},
                                status_code=404)
        row.manual_active = value
        s.commit()
        # Рядок зникає зі списку (чи повертається), лічильники й «неактуальних»
        # у зведенні — на наступному ж відкритті (умова власника для кешу).
        speedcache.owner_changed("manual_active")
        return JSONResponse({
            "ok": True, "id": row.id, "manual_active": row.manual_active,
            "is_active": row.is_active,
            "active": row.manual_active if row.manual_active is not None else row.is_active,
        })


@app.get("/api/properties")
def api_properties(
    min_sources: int = Query(1, ge=1, description="лише об'єкти на N+ майданчиках"),
    condition: str = "", market: str = "", rooms: str = "",
    limit: int = Query(200, le=MAX_ROWS),
):
    """Майстер-записи: один об'єкт — один рядок із посиланнями на всі оголошення."""
    d, _rules = _places()
    with SessionLocal() as s:
        stmt = select(Property).where(Property.sources_count >= min_sources)
        if condition in {c.value for c in Condition}:
            stmt = stmt.where(Property.condition == Condition(condition))
        if market in {m.value for m in MarketType}:
            stmt = stmt.where(Property.market_type == MarketType(market))
        if rooms == "4+":
            stmt = stmt.where(Property.rooms >= 4)
        elif rooms.isdigit():
            stmt = stmt.where(Property.rooms == int(rooms))
        rows = s.scalars(stmt.order_by(Property.sources_count.desc(),
                                       Property.last_seen.desc()).limit(limit)).all()
        return JSONResponse([{
            "id": p.id,
            "rooms": p.rooms,
            "area_total": p.area_total,
            "floor": p.floor,
            "floors_total": p.floors_total,
            "location": p.location,
            "street": p.street,
            "district": p.district,
            "price_usd_min": p.price_usd_min,
            "price_usd_max": p.price_usd_max,
            "price_per_sqm": p.price_per_sqm,
            "market_type": p.market_type.value,
            "condition": p.condition.value,
            "sources_count": p.sources_count,
            # Район і ЖК квартири (Блок 4, E10, D57) — лише додані поля.
            "district_key": p.district_key,
            "district_label": d.label(p.district_key) if d is not None else None,
            "complex_key": p.complex_key,
            "complex_label": d.complex_label(p.complex_key) if d is not None else None,
            "place_area": p.place_area,
            # Масив посилань на всі оригінальні оголошення цього об'єкта.
            "sources": [{"source": l.source, "url": l.original_url,
                         "price_usd": l.price_usd,
                         "published_at": l.published_at.isoformat() if l.published_at else None}
                        for l in p.listings],
        } for p in rows])


@app.get("/api/properties/{property_id}/prices")
def api_property_prices(property_id: int):
    """Єдина історія зміни ціни об'єкта — злита з усіх його оголошень."""
    with SessionLocal() as s:
        property_id = resolve_property_id(s, property_id) or property_id
        prop = s.get(Property, property_id)
        if prop is None:
            return JSONResponse({"detail": "не знайдено"}, status_code=404)
        ids = [l.id for l in prop.listings]
        # Події з однаковим часом — за оголошенням і id, явно (див.
        # objects.price_history): порядок не має залежати від плану запиту.
        events = s.scalars(
            select(PriceEvent).where(PriceEvent.listing_id.in_(ids))
            .order_by(PriceEvent.observed_at, PriceEvent.listing_id, PriceEvent.id)
        ).all()
        return JSONResponse({
            "property_id": property_id,
            "sources_count": prop.sources_count,
            "history": [{"observed_at": e.observed_at.isoformat(), "source": e.source,
                         "price": e.price, "currency": e.currency,
                         "price_usd": e.price_usd} for e in events],
        })


@app.get("/api/stats")
def api_stats():
    with SessionLocal() as s:
        return _cached_stats(s)


def _warm_lists() -> None:
    """Типові ключі «/» і «В обробці» та зведення — фоном після нового покоління.

    Той самий шлях, що й у запиті (ключ, запит id, зведення), тож прогрітий
    кеш — рівно те, що запит узяв би сам.
    """
    from ..places import facets

    d, _rules = _places()
    with SessionLocal() as s:
        dk = speedcache.data_key(s)
        if d is not None:
            # Карта id → row_* — тут, у фоні, а не в першому запиті після покоління.
            speedcache.place_map_build(dk, _build_place_map)
        for in_progress in (None, True):
            query = dict(condition="", market="", source="", rooms="", price_min=None,
                         price_max=None, sort=DEFAULT_SORT, in_progress=in_progress,
                         collapse=True)
            key = speedcache.list_key({**query, "district": "", "complex": "", "area": "",
                                       "all_ads": ""}, in_progress=in_progress,
                                      collapse=True)
            ids = speedcache.list_ids(s, key, lambda q=query: s.execute(
                list_ids_select(**q, place=facets.Selection())).scalars().all(), dk=dk)
            # Лічильники фільтрів «Район»/«ЖК» типової сторінки — теж фоном (Блок 4, E10).
            if d is not None:
                _base_groups(s, query, key, ids, dk=dk)
        _cached_stats(s)


speedcache.set_warmer(_warm_lists)
