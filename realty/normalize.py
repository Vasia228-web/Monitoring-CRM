"""Нормалізація та класифікація: ціни, площі, дати, ринок, стан, гео-фільтр."""
from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timezone

import httpx

from .config import BBOX, CITY_ALIASES, FALLBACK_USD_UAH, NBU_RATE_URL, USER_AGENT
from .models import Condition, MarketType

log = logging.getLogger(__name__)

# --- Курс валют ---------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def usd_uah_rate() -> float:
    """Курс НБУ з резервним значенням із конфігу."""
    try:
        r = httpx.get(NBU_RATE_URL, timeout=10, headers={"User-Agent": USER_AGENT})
        rate = float(r.json()[0]["rate"])
        if 20 < rate < 200:
            log.info("Курс НБУ USD/UAH: %.2f", rate)
            return rate
    except Exception as e:  # мережа/формат — не критично, працюємо далі
        log.warning("Курс НБУ недоступний (%s), беремо резервний %.2f", e, FALLBACK_USD_UAH)
    return FALLBACK_USD_UAH


def to_usd(amount: float | None, currency: str) -> float | None:
    if amount is None:
        return None
    cur = (currency or "USD").upper()
    if cur in ("USD", "$"):
        return round(amount, 2)
    if cur in ("UAH", "ГРН", "₴"):
        return round(amount / usd_uah_rate(), 2)
    if cur in ("EUR", "€"):
        return round(amount * 1.08, 2)
    return round(amount, 2)


def to_uah(amount: float | None, currency: str) -> float | None:
    if amount is None:
        return None
    cur = (currency or "USD").upper()
    if cur in ("UAH", "ГРН", "₴"):
        return round(amount, 2)
    usd = to_usd(amount, cur)
    return round(usd * usd_uah_rate(), 2) if usd is not None else None


# --- Парсери числових полів ---------------------------------------------------

_NUM = r"\d[\d\s  ']*(?:[.,]\d+)?"
_CUR_MAP = {
    "$": "USD", "usd": "USD", "дол": "USD",
    "грн": "UAH", "₴": "UAH", "uah": "UAH",
    "€": "EUR", "eur": "EUR",
}


def _num(s: str) -> float | None:
    s = re.sub(r"[\s  ']", "", s).replace(",", ".")
    # 3 741 608.64 -> ок; "1.234.567" -> прибираємо роздільники тисяч
    if s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def parse_price(text: str | None) -> tuple[float | None, str]:
    """Витягує суму й валюту з довільного тексту ціни."""
    if not text:
        return None, "USD"
    t = str(text)
    mult = 1.0
    if re.search(r"\bмлн", t, re.I):
        mult = 1_000_000.0
    elif re.search(r"\bтис", t, re.I):
        mult = 1_000.0

    def _scaled(v: float | None) -> float | None:
        return round(v * mult, 2) if v is not None else None

    m = re.search(rf"({_NUM})\s*(?:тис\.?|млн\.?)?\s*(\$|₴|грн|usd|uah|eur|€|дол)", t, re.I)
    if not m:
        m2 = re.search(rf"(\$|₴|€)\s*({_NUM})", t)
        if m2:
            return _scaled(_num(m2.group(2))), _CUR_MAP.get(m2.group(1), "USD")
        m3 = re.search(_NUM, t)
        return (_scaled(_num(m3.group(0))), "USD") if m3 else (None, "USD")
    return _scaled(_num(m.group(1))), _CUR_MAP.get(m.group(2).lower(), "USD")


def parse_area(text: str | None) -> float | None:
    """Загальна площа в м²."""
    if not text:
        return None
    m = re.search(rf"({_NUM})\s*(?:м²|м2|m²|кв\.?\s*м)", str(text), re.I)
    if m:
        v = _num(m.group(1))
        return v if v and 8 <= v <= 1000 else None
    return None


_WORD_ROOMS = (
    (re.compile(r"однокімнат|1-кімнат|одноком"), 1),
    (re.compile(r"двокімнат|2-кімнат|двухком"), 2),
    (re.compile(r"трикімнат|3-кімнат|трехком"), 3),
    (re.compile(r"чотирикімнат|4-кімнат|четырехком"), 4),
)
# «3-кім», «2 кімнати», «3 room»
_R_BEFORE = re.compile(r"(?<!\d)([1-9])\s*[-\s]?\s*(?:кім|комн|кк\b|room)")
# «1к квартира», «2-к кв» — «к» не має бути початком іншого слова
_R_SHORT = re.compile(r"(?<!\d)([1-9])\s*-?\s*к(?![а-яіїєґa-z0-9])")
# «кімн. 3», «кімнат: 2», «К-сть кімнат | 2» — обов'язковий роздільник,
# інакше шаблон ловить площу в «двокімнатна 60,2 м».
_R_AFTER = re.compile(r"(?:кімнат|кімн|комнат|комн)\w*\s*[.:\-|]\s*([1-9])(?!\d)")


def parse_rooms(text: str | None) -> int | None:
    """Кількість кімнат із заголовка/тексту.

    Порядок правил має значення: словесні форми («двокімнатна») перевіряємо
    раніше за числові, бо інакше сусіднє число (площа, поверх) видає себе
    за кількість кімнат.
    """
    if not text:
        return None
    t = str(text).lower()
    for rx, n in _WORD_ROOMS:
        if rx.search(t):
            return n
    for rx in (_R_BEFORE, _R_SHORT, _R_AFTER):
        if m := rx.search(t):
            return int(m.group(1))
    return None


_MONTHS = {
    "січ": 1, "лют": 2, "бер": 3, "квіт": 4, "трав": 5, "черв": 6,
    "лип": 7, "серп": 8, "верес": 9, "жовт": 10, "листоп": 11, "груд": 12,
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def parse_date(text: str | None) -> datetime | None:
    """Дата публікації з ISO, `дд.мм.рррр`, `05 вересня 2026` або «сьогодні»."""
    if not text:
        return None
    t = str(text).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(t[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", t)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s+([а-яіїєґ]{3,})\.?\s*(\d{4})?", t, re.I)
    if m:
        day, mon_raw, year = m.group(1), m.group(2).lower(), m.group(3)
        for stem, num in _MONTHS.items():
            if mon_raw.startswith(stem):
                y = int(year) if year else datetime.now().year
                try:
                    return datetime(y, num, int(day))
                except ValueError:
                    return None
    low = t.lower()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if "сьогодні" in low or "сегодня" in low or "хвилин" in low or "годин" in low:
        return now
    return None


# --- Класифікатори ------------------------------------------------------------

# Порядок важливий: спершу ловимо заперечення («без ремонту» містить «ремонт»).
_NO_REPAIR = re.compile(
    r"без\s+ремонт|без\s+обробк|без\s+оздобл|потребу\w*\s+ремонт|під\s+ремонт|"
    r"сирец|сирц|сир\w*\s*(?:стан|варіант)|черн\w*\s+(?:обробк|отделк|роб)|"
    r"після\s+будівельник|стартов\w+\s+стан|"
    r"штукатурк\w*\s+стін|стяжк|"
    r"требует\s+ремонт|под\s+ремонт|без\s+отделк|черновая",
    re.I,
)
# Ремонт у майбутньому часі — це НЕ ремонт. «Площа дозволяє зробити сучасний
# ремонт», «зроблю ремонт під покупця», «можливий ремонт за домовленістю» —
# у всіх трьох фразах слово «ремонт» присутнє, а ремонту немає. Шукаємо намір
# перед словом, а не саме слово.
# Форми навмисно перелічені, а не «зроб\w+»: «зробити ремонт» — це намір, а
# «зроблено ремонт» — факт, і один зайвий символ у шаблоні перетворює другий
# на перший.
_FUTURE_REPAIR = re.compile(
    r"(?:дозволя\w+|можн[ао]|можлив\w*|плану\w+|потрібн\w*|треба|варто|"
    r"зробити|зроблю|зробиш|зробимо|зробите|зробить|"
    r"під\s+себе|своїм\s+смаком|на\s+ваш\s+смак|за\s+домовлен\w+)"
    r"[^.!?\n]{0,40}?ремонт"
    r"|ремонт[^.!?\n]{0,30}?(?:під\s+(?:себе|покупц|власн)|за\s+домовлен\w+|"
    r"на\s+ваш\s+смак|своїм\s+смаком)",
    re.I,
)

# Продається не квартира, а право за договором у будинку, який ще будується.
# Заявлений у такому оголошенні «ремонт» описує обіцянку забудовника, а не
# те, що покупець побачить: квартири фізично ще немає.
_ASSIGNMENT = re.compile(
    r"переуступк|уступк\w+\s+прав|відступлення\s+прав|договор\w*\s+переуступ|"
    r"інвестиційн\w+\s+договор",
    re.I,
)

_HAS_REPAIR = re.compile(
    r"з\s+ремонт|євроремонт|еврорем|дизайнерськ\w+\s+ремонт|капітальн\w+\s+ремонт|"
    r"свіж\w+\s+ремонт|після\s+ремонт|якісн\w+\s+ремонт|сучасн\w+\s+ремонт|"
    r"готов\w*\s+до\s+проживанн|заїжджай|заходь\s+і\s+живи|з\s+меблями|"
    r"хорошем\s+состоянии|с\s+ремонтом|отличном\s+состоянии|"
    r"з\s+обробк|обставлен|вмебльован|з\s+технік",
    re.I,
)

# Будинок ще не зданий: у таких оголошеннях про ремонт не пишуть, бо квартира
# фізично не існує. Разом із первинним ринком це надійна ознака «без ремонту».
_UNBUILT = re.compile(
    r"здача\s+(?:жк|буд|компл|заявлена|планується)|введенн\w*\s+в\s+експлуатац|"
    r"будівництво\s+(?:трива|ведет)|на\s+етапі\s+будівництв|"
    r"\d\s*(?:-й|-го)?\s*квартал\w*\s+20\d\d\s*рок|черга\s+будівництв",
    re.I,
)

_PRIMARY = re.compile(
    r"новобудов|новостро|\bЖК\b|від\s+забудовник|перв(?:инн|ичн)\w+\s+ринок|"
    r"здача\s+в\s+\d{4}|введен\w*\s+в\s+експлуатац",
    re.I,
)
# Назви серій радянської та пострадянської забудови — однозначна ознака
# вторинного ринку. RIA й OLX ставлять їх окремими тегами.
_SECONDARY = re.compile(
    r"вторинн|хрущов|сталінк|сталинк|чешк|чеськ\w*\s+проект|"
    r"гостинк|малосімейк|польськ\w+\s+люкс|"
    r"житлов\w+\s+фонд\s+(?:19|20[01])\d|"
    r"втор(?:инн|ичн)\w+\s+ринок|старий\s+фонд",
    re.I,
)


def is_assignment(*parts: str | None) -> bool:
    """Чи продається право за договором, а не готова квартира."""
    return bool(_ASSIGNMENT.search(" ".join(p for p in parts if p)))


def classify_condition(*parts: str | None, market: "MarketType | None" = None,
                       declared: "Condition | None" = None) -> Condition:
    """Стан житла за описом, тегами й полем, яке заповнив продавець.

    Порядок перевірок — це і є вся логіка, і кожен щабель має причину:

    1. Пряме заперечення («без ремонту», «під ремонт», «сирець») сильніше за
       все інше, зокрема й за те, що продавець вибрав у полі на сайті.

    2. Переуступка — продається право за договором, квартири ще немає.
       Заявлений ремонт тут описує намір забудовника, а не те, що побачить
       покупець. Кажемо «не визначено», а не «без ремонту»: буває, що здають
       і з оздобленням.

    3. Будинок ще не зданий:
         * якщо про ремонт узагалі не йдеться — на цьому ринку квартиру
           віддають сирцем, і «без ремонту» тут обґрунтоване;
         * якщо ремонт заявлений — це обіцянка на майбутнє, а не факт, тож
           чесна відповідь «не визначено». Раніше тут поверталось «з
           ремонтом», і саме такі оголошення потрапляли у фільтр «з ремонтом»,
           а покупець відкривав недобудову.

    4. Поле, яке продавець заповнив сам, — найкращий сигнал із доступних,
       коли квартира існує.

    5. Ремонт у майбутньому часі («дозволяє зробити сучасний ремонт») — не
       ремонт. Без цієї перевірки шаблон спрацьовував на обіцянці.

    6. І лише потім — ознаки готового ремонту в тексті.
    """
    text = " ".join(p for p in parts if p)
    claimed = bool(declared and declared is Condition.RENOVATED) or (
        bool(text) and bool(_HAS_REPAIR.search(text))
        and not _FUTURE_REPAIR.search(text))

    if not text.strip():
        return declared or Condition.UNKNOWN
    if _NO_REPAIR.search(text):
        return Condition.NEEDS_REPAIR
    if _ASSIGNMENT.search(text):
        return Condition.UNKNOWN
    if _UNBUILT.search(text):
        if claimed:
            return Condition.UNKNOWN
        return (Condition.NEEDS_REPAIR if market is MarketType.PRIMARY
                else Condition.UNKNOWN)
    if declared is not None and declared is not Condition.UNKNOWN:
        return declared
    if _FUTURE_REPAIR.search(text):
        return Condition.UNKNOWN
    if _HAS_REPAIR.search(text):
        return Condition.RENOVATED
    return Condition.UNKNOWN


def classify_market(*parts: str | None, built_year: int | None = None,
                    complex_name: str | None = None) -> MarketType:
    """Первинний vs вторинний ринок."""
    text = " ".join(p for p in parts if p)
    if _PRIMARY.search(text):
        return MarketType.PRIMARY
    if _SECONDARY.search(text):
        return MarketType.SECONDARY
    if built_year:
        # Будинки останніх років фактично формують первинний ринок.
        return MarketType.PRIMARY if built_year >= datetime.now().year - 2 else MarketType.SECONDARY
    # Назва ЖК без інших ознак — недостатній сигнал: у ЖК буває і перепродаж.
    return MarketType.UNKNOWN


# --- Гео-фільтр ---------------------------------------------------------------


# Назва області містить назву міста, тому спершу прибираємо згадки області
# й району — інакше «м. Коломия, Івано-Франківська обл.» пройде як місто.
_OBLAST_RE = re.compile(
    r"івано[-\s]?франківськ\w*\s*(?:обл\w*|район\w*|р-н)|"
    r"ивано[-\s]?франковск\w*\s*(?:обл\w*|район\w*)",
    re.I,
)
_CITY_RE = re.compile(
    r"івано[-\s]?франківськ|ивано[-\s]?франковск|ivano[-\s]?frank(?:i|o)vsk", re.I
)


def in_ivano_frankivsk(text: str | None = None, lat: float | None = None,
                       lon: float | None = None) -> bool:
    """Чи належить об'єкт саме місту Івано-Франківськ.

    Координати — надійніший сигнал, тому мають пріоритет. Текстова перевірка
    відкидає згадки області/району, щоб інші міста Прикарпаття не проходили.
    """
    if lat is not None and lon is not None:
        return (BBOX["south"] <= lat <= BBOX["north"]
                and BBOX["west"] <= lon <= BBOX["east"])
    if text:
        cleaned = _OBLAST_RE.sub(" ", str(text))
        return bool(_CITY_RE.search(cleaned))
    return False


def compute_price_per_sqm(price_usd: float | None, area: float | None,
                          given: float | None = None) -> float | None:
    """Ціна за м² в USD: беремо готову або рахуємо."""
    if given and given > 0:
        return round(given, 2)
    if price_usd and area and area > 0:
        return round(price_usd / area, 2)
    return None
