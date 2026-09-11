"""Правила валідності та пороги, пораховані з наявних даних.

Пороги не задані наперед: вони рахуються з активних записів огорожами Тьюкі
в логарифмічному просторі — стандартний робастний спосіб для скошених
розподілів, яким і є ціна нерухомості. Зберігаються у файл і перераховуються
щотижневою рутиною, щоб змінюватись разом із ринком.

Два рівні суворості, бо ціна помилки різна:
    k=1.5  — «підозріло», запис іде на ручний перегляд (~0.5% бази)
    k=3.0  — «зламано», запис не пускається в прод      (~0.03% бази)
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..config import DATA_DIR
from ..models import Listing, effective_active
from ..normalize import _FUTURE_REPAIR, _HAS_REPAIR, _NO_REPAIR, _UNBUILT, is_assignment

log = logging.getLogger(__name__)

THRESHOLDS_FILE = DATA_DIR / "quality_thresholds.json"

REQUIRED_FIELDS = ("price", "rooms", "location", "original_url")
REVIEW_K = 1.5
REJECT_K = 3.0
# Падіння або стрибок ціни, який не можна приймати мовчки.
PRICE_JUMP_LIMIT = 0.70
# Частка відхилених в одному прогоні джерела, після якої зупиняємось і питаємо.
ESCALATION_RATE = 0.20
ESCALATION_MIN_BATCH = 20        # на дрібних пакетах відсоток нічого не означає


@dataclass
class Band:
    review_low: float
    review_high: float
    reject_low: float
    reject_high: float

    def verdict(self, value: float | None) -> str | None:
        if value is None:
            return None
        if value < self.reject_low or value > self.reject_high:
            return "reject"
        if value < self.review_low or value > self.review_high:
            return "review"
        return None


# Наскільки ціна за м² може бути нижчою за медіану СВОГО сегмента, перш ніж
# запис піде на перевірку. Глобальний поріг цього класу помилок не бачить:
# нижня межа по всій базі — близько $400/м², бо туди входить і сирець, і
# дешева вторинка. Квартира «з ремонтом у новобудові» за $717/м² при медіані
# сегмента $1 948 у цю смугу вписується вільно, хоча вона втричі дешевша за
# схожі. А сортування за зростанням ціни виносить саме такі записи на першу
# сторінку — тобто туди, куди дивляться першим ділом.
SEGMENT_LOW_RATIO = 0.55
# Сегмент менший за це число не дає надійної медіани — не робимо висновків.
SEGMENT_MIN_SAMPLE = 30


@dataclass
class Thresholds:
    price_usd: Band
    price_per_sqm: Band
    area_total: Band
    computed_at: str = ""
    sample_size: int = 0
    # Медіана ціни за м² по кожному сегменту «кімнатність|стан|ринок».
    # Ключ — рядок, щоб пороги лишались звичайним JSON-файлом, який можна
    # відкрити й прочитати очима.
    segment_median_sqm: dict = field(default_factory=dict)

    def segment_key(self, rec: dict) -> str:
        rooms = rec.get("rooms")
        band = "?" if rooms is None else str(min(int(rooms), 4))
        cond = rec.get("condition")
        market = rec.get("market_type")
        return f"{band}|{getattr(cond, 'value', cond) or '?'}|" \
               f"{getattr(market, 'value', market) or '?'}"

    def segment_floor(self, rec: dict) -> float | None:
        """Нижня межа ціни за м² для сегмента цього запису."""
        entry = self.segment_median_sqm.get(self.segment_key(rec))
        if not entry or entry.get("n", 0) < SEGMENT_MIN_SAMPLE:
            return None
        return entry["median"] * SEGMENT_LOW_RATIO

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Thresholds":
        return cls(
            price_usd=Band(**d["price_usd"]),
            price_per_sqm=Band(**d["price_per_sqm"]),
            area_total=Band(**d["area_total"]),
            computed_at=d.get("computed_at", ""),
            sample_size=d.get("sample_size", 0),
            segment_median_sqm=d.get("segment_median_sqm") or {},
        )


def _fences(values: list[float], k: float) -> tuple[float, float]:
    logs = sorted(math.log10(v) for v in values if v and v > 0)
    if len(logs) < 20:
        return 0.0, float("inf")
    q1, q3 = logs[len(logs) // 4], logs[3 * len(logs) // 4]
    iqr = q3 - q1
    return 10 ** (q1 - k * iqr), 10 ** (q3 + k * iqr)


def _band(values: list[float]) -> Band:
    rl, rh = _fences(values, REVIEW_K)
    xl, xh = _fences(values, REJECT_K)
    return Band(round(rl, 2), round(rh, 2), round(xl, 2), round(xh, 2))


def compute_thresholds(session) -> Thresholds:
    """Рахує пороги з активних записів, які вже пройшли контроль."""
    base = [effective_active().is_(True)]
    price = [v for v in session.scalars(
        select(Listing.price_usd).where(Listing.price_usd.isnot(None), *base))]
    sqm = [v for v in session.scalars(
        select(Listing.price_per_sqm).where(Listing.price_per_sqm.isnot(None), *base))]
    area = [v for v in session.scalars(
        select(Listing.area_total).where(Listing.area_total.isnot(None), *base))]
    return Thresholds(
        price_usd=_band(price), price_per_sqm=_band(sqm), area_total=_band(area),
        computed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        sample_size=len(price),
        segment_median_sqm=_segment_medians(session, base),
    )


def _segment_medians(session, base) -> dict:
    """Медіана ціни за м² по кожному сегменту «кімнатність|стан|ринок»."""
    rows = session.execute(
        select(Listing.rooms, Listing.condition, Listing.market_type,
               Listing.price_per_sqm)
        .where(Listing.price_per_sqm.isnot(None), Listing.quality_status == "ok",
               *base)).all()
    buckets: dict[str, list[float]] = {}
    for rooms, cond, market, value in rows:
        band = "?" if rooms is None else str(min(int(rooms), 4))
        key = f"{band}|{cond.value}|{market.value}"
        buckets.setdefault(key, []).append(value)
    return {k: {"median": round(statistics.median(v), 2), "n": len(v)}
            for k, v in buckets.items()}


def save_thresholds(t: Thresholds) -> None:
    THRESHOLDS_FILE.write_text(json.dumps(t.to_json(), ensure_ascii=False, indent=1),
                               encoding="utf-8")
    log.info("Пороги оновлено на вибірці %d записів", t.sample_size)


def load_thresholds(session=None) -> Thresholds:
    """Пороги з файлу; якщо їх ще немає — рахує й зберігає."""
    if THRESHOLDS_FILE.exists():
        try:
            return Thresholds.from_json(json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, TypeError):
            log.warning("Файл порогів пошкоджено — рахуємо заново")
    if session is None:
        from ..db import SessionLocal
        with SessionLocal() as s:
            t = compute_thresholds(s)
    else:
        t = compute_thresholds(session)
    save_thresholds(t)
    return t


def validate(rec: dict, t: Thresholds, previous_price_usd: float | None = None) -> tuple[str, list[str]]:
    """Вердикт для одного запису: `ok` / `review` / `rejected` + причини."""
    reasons: list[str] = []

    missing = [f for f in REQUIRED_FIELDS if not rec.get(f)]
    if missing:
        return "rejected", [f"немає обов'язкових полів: {', '.join(missing)}"]

    checks = (
        ("ціна", rec.get("price_usd"), t.price_usd, "$"),
        ("ціна за м²", rec.get("price_per_sqm"), t.price_per_sqm, "$"),
        ("площа", rec.get("area_total"), t.area_total, "м²"),
    )
    verdict = "ok"
    for label, value, band, unit in checks:
        got = band.verdict(value)
        if got == "reject":
            verdict = "rejected"
            reasons.append(f"{label} {value:,.0f}{unit} поза межами "
                           f"{band.reject_low:,.0f}–{band.reject_high:,.0f}")
        elif got == "review" and verdict != "rejected":
            verdict = "review"
            reasons.append(f"{label} {value:,.0f}{unit} поза звичним "
                           f"{band.review_low:,.0f}–{band.review_high:,.0f}")

    rooms = rec.get("rooms")
    if rooms is not None and not (1 <= rooms <= 9):
        return "rejected", reasons + [f"кімнат {rooms} — поза розумними межами"]

    # Ціна, підозріло низька для СВОГО сегмента. Помилки в ціні майже завжди
    # зміщені вниз — пропущений розряд, ціна «від», ціна за метр замість
    # загальної, не та валюта, — тому сортування за зростанням систематично
    # витягує биті записи нагору. Глобальна смуга їх не ловить.
    floor = t.segment_floor(rec)
    value = rec.get("price_per_sqm")
    if floor and value and value < floor:
        if verdict != "rejected":
            verdict = "review"
        reasons.append(
            f"ціна за м² ${value:,.0f} — нижче за {SEGMENT_LOW_RATIO:.0%} медіани "
            f"свого сегмента (${floor / SEGMENT_LOW_RATIO:,.0f})")

    # Класифікацію карантин досі не перевіряв узагалі — ні стан, ні тип ринку.
    # Через це помилка в цих полях проходила без жодного сліду, хоч саме вона
    # найпомітніша: людина ставить фільтр «з ремонтом» і відкриває сирець.
    conflict = _classification_conflict(rec)
    if conflict:
        if verdict != "rejected":
            verdict = "review"
        reasons.append(conflict)

    # Різкий стрибок ціни не перезаписує історію мовчки.
    new_price = rec.get("price_usd")
    if previous_price_usd and new_price and previous_price_usd > 0:
        delta = (new_price - previous_price_usd) / previous_price_usd
        if abs(delta) >= PRICE_JUMP_LIMIT:
            if verdict != "rejected":
                verdict = "review"
            reasons.append(f"ціна змінилась на {delta * 100:+.0f}% "
                           f"(${previous_price_usd:,.0f} -> ${new_price:,.0f})")
    return verdict, reasons


def _classification_conflict(rec: dict) -> str | None:
    """Чи не суперечить проставлений стан тому, що написано в оголошенні."""
    condition = getattr(rec.get("condition"), "value", rec.get("condition"))
    text = " ".join(str(rec.get(f) or "") for f in ("title", "description"))

    # Рік введення в експлуатацію в майбутньому — найнадійніша ознака того, що
    # квартири ще немає. Вона не залежить від формулювань в описі, тому
    # перевіряється до тексту.
    year = rec.get("built_year")
    if condition == "renovated" and year and year > datetime.now().year:
        return (f"позначено «з ремонтом», але будинок вводять в експлуатацію "
                f"аж {year} року")

    if not text.strip():
        return None
    if condition == "renovated":
        if is_assignment(text):
            return "позначено «з ремонтом», але це переуступка — квартири ще немає"
        if _UNBUILT.search(text):
            return "позначено «з ремонтом», але будинок ще не зданий"
        if _NO_REPAIR.search(text):
            return "позначено «з ремонтом», а в описі сказано протилежне"
    if condition == "needs_repair" and _HAS_REPAIR.search(text) \
            and not _NO_REPAIR.search(text) and not _FUTURE_REPAIR.search(text):
        return "позначено «без ремонту», а в описі йдеться про готовий ремонт"
    return None


def validate_llm_output(parsed) -> tuple[bool, list[str]]:
    """Перевірка відповіді моделі перед тим, як пускати її далі."""
    problems: list[str] = []
    if parsed is None:
        return False, ["модель не відповіла"]
    price = getattr(parsed, "price", None)
    rooms = getattr(parsed, "rooms", None)
    area = getattr(parsed, "area_total", None)
    if price is not None and price <= 0:
        problems.append(f"ціна {price} не додатна")
    if rooms is not None and not (1 <= rooms <= 9):
        problems.append(f"кімнат {rooms} поза межами 1–9")
    if area is not None and not (8 <= area <= 1000):
        problems.append(f"площа {area} поза межами 8–1000 м²")
    if not any(v is not None for v in (price, rooms, area,
                                       getattr(parsed, "location", None))):
        problems.append("порожня відповідь")
    return not problems, problems
