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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..config import DATA_DIR
from ..models import Listing, effective_active

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


@dataclass
class Thresholds:
    price_usd: Band
    price_per_sqm: Band
    area_total: Band
    computed_at: str = ""
    sample_size: int = 0

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
    )


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
