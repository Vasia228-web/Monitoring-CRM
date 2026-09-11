"""Виявлення зниклих оголошень різницею списків.

Поодинокий обхід усіх 16 тисяч посилань — неправильний спосіб дізнатись, що
зникло. Правильний дешевший на два порядки: джерела самі перелічують те, що
зараз опубліковано, по 20–500 штук за запит. Досить зберегти вчорашній
перелік і порівняти з сьогоднішнім.

Ключове обмеження, і воно тут головне: **випадіння з видачі не є доказом
зняття**. Оголошення могло опуститись у ранжуванні, потрапити під інші
фільтри, зникнути через збій пагінації або тимчасову помилку сайту. Тому
різниця списків не знімає нічого з продажу — вона лише називає кандидатів,
яких далі перевіряє поодинокий запит. Дешевий широкий сигнал шукає, дорога
точкова перевірка підтверджує, і статус `delisted` як і раніше присвоюється
тільки за явним 404/410.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .config import DATA_DIR
from .db import session_scope
from .models import Listing
from .sources import REGISTRY

log = logging.getLogger(__name__)

DIR = Path(DATA_DIR) / "snapshots"

# Якщо новий перелік раптом виявився значно меншим за попередній, це майже
# напевно збій збору, а не масове зняття оголошень. Такий перелік не йде на
# порівняння: інакше одна невдала пагінація згенерувала б тисячі «кандидатів»
# і витратила б на них добовий бюджет перевірок.
SHRINK_GUARD = 0.75
# Мінімальний розмір, за якого порівняння взагалі має сенс.
MIN_SIZE = 20
# Стеля кандидатів за один прогін: захист від того, щоб дивна поведінка сайту
# не перетворилась на лавину запитів до нього ж.
MAX_CANDIDATES = 400

# Джерела, видачу яких можна перелічити ПОВНІСТЮ. Снапшот має сенс лише
# повний: якщо пагінація обривається, усе з недосяжних сторінок виглядатиме
# «зниклим», і кожен прогін генерував би тисячі хибних кандидатів.
#
# OLX сюди не входить за вимірюванням (`probes/p_olx_depth.py`): видача
# жорстко обмежена 25 сторінками — з 26-ї повертається початок списку (збіг
# зі сторінкою 1 — 45 карток із 52). Це дає стелю близько 1100 оголошень при
# 2488 у місті. Фільтрів, якими можна було б розрізати видачу на менші зрізи,
# у посиланнях немає — вони застосовуються скриптом. Тому OLX виявляється
# звичайною чергою поодиноких перевірок, яка після переходу на HEAD стала
# достатньо швидкою.
#
# blago не перевіряється взагалі: сайт віддає 200 і на живе, і на вигадане
# планування, тож ані перелік, ані поодинокий запит нічого не скажуть.
ENUMERABLE = {"domria", "lun", "flombu"}

# Як часто має сенс перелічувати кожне джерело. Число підібране під вартість
# переліку, а не «щоб частіше»: DOM.RIA віддає 8.5 тисяч ідентифікаторів за
# 43 запити, тож його дешево оновлювати щопрогону й мати затримку виявлення
# в три години. LUN коштує 223 запити — там доба розумніша за три години.
MIN_INTERVAL_HOURS = {"domria": 3, "lun": 24, "flombu": 24}
DEFAULT_INTERVAL_HOURS = 24
NOT_ENUMERABLE_REASON = {
    "olx": "видача обмежена 25 сторінками (~1100 із 2488) — перелік неповний",
    "blago": "сайт не відрізняє знятого планування від живого",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Snapshot:
    source: str
    taken_at: datetime
    ids: set[str]
    requests: int = 0

    @property
    def size(self) -> int:
        return len(self.ids)


def path_for(source: str) -> Path:
    return DIR / f"{source}.json"


def load(source: str) -> Snapshot | None:
    p = path_for(source)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Снапшот %s не читається (%s) — вважаємо, що його немає", source, e)
        return None
    return Snapshot(source=source, taken_at=datetime.fromisoformat(raw["taken_at"]),
                    ids=set(raw.get("ids") or []), requests=raw.get("requests", 0))


def save(snapshot: Snapshot) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    path_for(snapshot.source).write_text(json.dumps({
        "source": snapshot.source,
        "taken_at": snapshot.taken_at.isoformat(),
        "count": snapshot.size,
        "requests": snapshot.requests,
        "ids": sorted(snapshot.ids),
    }, ensure_ascii=False))


def capture(source: str, fetcher=None, browser=None) -> Snapshot:
    """Перелічує все, що джерело зараз публікує по нашому місту."""
    cls = REGISTRY[source]
    # Повна глибина: снапшот має сенс лише тоді, коли він повний. Часткова
    # видача зробила б «зниклими» всі оголошення з ненадрукованих сторінок.
    src = cls(fetcher=fetcher, browser=browser, mode="full")
    before = _requests_made(src)
    ids = {i for i in src.iter_ids() if i}
    return Snapshot(source=source, taken_at=_now(), ids=ids,
                    requests=max(0, _requests_made(src) - before))


def _requests_made(src) -> int:
    return src.stats.get("pages", 0)


@dataclass
class DiffReport:
    """Що дала різниця списків. `used=False` — порівняння не відбулось."""

    source: str
    used: bool
    reason: str = ""
    previous_size: int = 0
    current_size: int = 0
    requests: int = 0
    candidates: list[str] = None          # зовнішні ідентифікатори

    def __post_init__(self) -> None:
        if self.candidates is None:
            self.candidates = []


def compare(previous: Snapshot | None, current: Snapshot) -> DiffReport:
    """Порівнює переліки й повертає кандидатів на зникнення — не вироки."""
    report = DiffReport(source=current.source, used=False,
                        current_size=current.size, requests=current.requests,
                        previous_size=previous.size if previous else 0)
    if current.size < MIN_SIZE:
        report.reason = (f"перелік замалий ({current.size} < {MIN_SIZE}) — "
                         f"схоже на збій збору, а не на порожню видачу")
        return report
    if previous is None:
        report.reason = "перший перелік — порівнювати нема з чим, зберігаємо як базу"
        report.used = True          # зберегти можна, кандидатів просто немає
        return report
    if current.size < previous.size * SHRINK_GUARD:
        report.reason = (
            f"перелік впав з {previous.size} до {current.size} "
            f"(менше {SHRINK_GUARD:.0%}) — це майже напевно збій пагінації, "
            f"а не масове зняття; порівняння пропущено")
        log.error("%s: %s", current.source, report.reason)
        return report

    report.used = True
    report.candidates = sorted(previous.ids - current.ids)
    if len(report.candidates) > MAX_CANDIDATES:
        log.warning("%s: кандидатів %d — обмежуємо до %d за прогін",
                    current.source, len(report.candidates), MAX_CANDIDATES)
        report.candidates = report.candidates[:MAX_CANDIDATES]
    return report


def baseline_from_db(source: str) -> Snapshot | None:
    """Перелік, який база ВВАЖАЄ опублікованим, — база для першого порівняння.

    Без цього найперший прогін по кожному джерелу нічого не дає: він лише
    зберігає снапшот і чекає наступного. Але наше уявлення про опубліковане
    вже записане в базі, і порівняти з ним можна одразу. Ризику це не додає:
    різниця, як і завжди, дає лише кандидатів, яких підтверджує запит.
    """
    with session_scope() as s:
        ids = set(s.scalars(
            select(Listing.external_id).where(
                Listing.source == source, Listing.is_active.is_(True))).all())
    return Snapshot(source=source, taken_at=_now(), ids=ids) if ids else None


def candidate_listing_ids(source: str, external_ids: list[str]) -> list[int]:
    """Перекладає зовнішні ідентифікатори на наші, лишаючи тільки ще активні."""
    if not external_ids:
        return []
    with session_scope() as s:
        return list(s.scalars(
            select(Listing.id).where(
                Listing.source == source,
                Listing.external_id.in_(external_ids),
                Listing.is_active.is_(True))).all())


def hours_until_due(source: str) -> float:
    """Скільки годин лишилось до наступного переліку. 0 — можна зараз."""
    previous = load(source)
    if previous is None:
        return 0.0
    interval = MIN_INTERVAL_HOURS.get(source, DEFAULT_INTERVAL_HOURS)
    age = (_now() - previous.taken_at).total_seconds() / 3600
    return max(0.0, interval - age)


def run(sources: list[str] | None = None, confirm: bool = True,
        force: bool = False) -> dict:
    """Повний цикл: перелічити, порівняти, підтвердити кандидатів запитом.

    Повертає звіт із числами по кожному джерелу — скільки запитів витрачено
    на перелік, скільки кандидатів знайдено, скільки з них підтверджено як
    справді зняті.
    """
    from .verify import verify_batch

    names = [n for n in (sources or REGISTRY) if n in REGISTRY]
    report = {"sources": {}, "requests_enumerate": 0, "requests_confirm": 0,
              "candidates": 0, "delisted": 0, "skipped": {}}

    for name in names:
        if name not in ENUMERABLE:
            reason = NOT_ENUMERABLE_REASON.get(name, "перелік недоступний")
            report["skipped"][name] = reason
            log.info("%s: різниця списків не застосовна — %s", name, reason)
            continue

        if not force and (wait := hours_until_due(name)) > 0:
            report["skipped"][name] = (
                f"перелік свіжий, наступний через {wait:.1f} год")
            continue
        try:
            current = capture(name)
        except Exception as e:                                  # noqa: BLE001
            log.warning("%s: перелік не вдався: %s", name, e)
            report["sources"][name] = {"error": str(e)[:200]}
            continue

        previous = load(name) or baseline_from_db(name)
        diff = compare(previous, current)
        entry = {"previous": diff.previous_size, "current": diff.current_size,
                 "requests": diff.requests, "used": diff.used,
                 "reason": diff.reason, "candidates": len(diff.candidates),
                 "confirmed": 0, "still_alive": 0, "unclear": 0}
        report["requests_enumerate"] += diff.requests

        if diff.used:
            # Зберігаємо лише перелік, який пройшов перевірку на осудність:
            # зіпсований снапшот у ролі бази отруїв би й наступне порівняння.
            save(current)

        ids = candidate_listing_ids(name, diff.candidates) if confirm else []
        if ids:
            stats = verify_batch(limit=len(ids), ids=ids, reason="candidate")
            entry["confirmed"] = stats["delisted"]
            entry["still_alive"] = stats["alive"]
            entry["unclear"] = stats["unknown"]
            report["requests_confirm"] += stats["requests"]
            report["delisted"] += stats["delisted"]
        report["candidates"] += len(diff.candidates)
        report["sources"][name] = entry

    return report
