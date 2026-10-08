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
тільки за явним сигналом.

Блок 1 (крок E8, D52): різниця більше не підтверджує кандидатів сама і не
порівнює лише «вчора/сьогодні» — кандидат перевірявся один раз, і 1 236 DOM.RIA
після одного 200 більше не перевірялись (Етап 0, D45). Тепер ПОВНИЙ перелік
ставить позначку `listings.absent_since` усім актуальним рядкам, яких у ньому
немає (DOM.RIA — за ключем «domria:<id>» на рядках УСІХ джерел, бо LUN теж веде
на DOM.RIA; LUN і flombu — за external_id своїх рядків), і знімає її з тих, що
є. Позначка переживає збереження снапшота; перевіряє наступний крок «перевірка
актуальності» ярусом підказаних за графіком run.absent_backoff_hours, доки
оголошення або знято за явним сигналом, або знову в переліку. Неповний перелік
(FetchError посеред DOM.RIA, помилки сторінок LUN/flombu) нікого не позначає й
не зберігається. Пороги — config/liveness.toml [snapshot].
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, update

from . import configfiles
from .config import DATA_DIR
from .db import session_scope
from .models import Listing
from .sources import REGISTRY

log = logging.getLogger(__name__)

DIR = Path(DATA_DIR) / "snapshots"

_SNAP = configfiles.load("liveness").snapshot
# Якщо новий перелік раптом виявився значно меншим за попередній, це майже
# напевно збій збору, а не масове зняття оголошень. Такий перелік не йде на
# порівняння: інакше одна невдала пагінація позначила б тисячі «зниклих» і
# витратила б на них порції перевірок.
SHRINK_GUARD = _SNAP.shrink_guard
# Мінімальний розмір, за якого порівняння взагалі має сенс.
MIN_SIZE = _SNAP.min_size
# Стеля НОВИХ позначок «зник із переліку» за один прогін: захист від того, щоб
# дивна поведінка сайту не перетворилась на лавину запитів до нього ж.
MAX_CANDIDATES = _SNAP.max_candidates_per_run
# Рядків на транзакцію позначок (інтеграційний план: пакети ≤200 рядків).
MARK_BATCH = configfiles.load("liveness").run.apply_batch_rows

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
MIN_INTERVAL_HOURS = dict(_SNAP.interval_hours)
DEFAULT_INTERVAL_HOURS = max(MIN_INTERVAL_HOURS.values())
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
    # Перелік дійшов до кінця без помилок (E8). Неповний не зберігається й нікого
    # не позначає; збережений до E8 файл без цієї позначки вважається неповним
    # для перевірки існування (правило повторного 404).
    complete: bool = True

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
                    ids=set(raw.get("ids") or []), requests=raw.get("requests", 0),
                    complete=raw.get("complete") is True)


def save(snapshot: Snapshot) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path_for(snapshot.source).with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "source": snapshot.source,
        "taken_at": snapshot.taken_at.isoformat(),
        "count": snapshot.size,
        "requests": snapshot.requests,
        "complete": snapshot.complete,
        "ids": sorted(snapshot.ids),
    }, ensure_ascii=False))
    tmp.replace(path_for(snapshot.source))


def capture(source: str, fetcher=None, browser=None) -> Snapshot:
    """Перелічує все, що джерело зараз публікує по нашому місту.

    `complete` — False, якщо джерело дорогою мало помилки (stats.errors) або
    перелік обірвався (stats.enum_incomplete, DOM.RIA iter_ids на FetchError).
    """
    cls = REGISTRY[source]
    # Повна глибина: снапшот має сенс лише тоді, коли він повний. Часткова
    # видача зробила б «зниклими» всі оголошення з ненадрукованих сторінок.
    src = cls(fetcher=fetcher, browser=browser, mode="full")
    before = _requests_made(src)
    ids = {i for i in src.iter_ids() if i}
    complete = not src.stats.get("errors") and not src.stats.get("enum_incomplete")
    return Snapshot(source=source, taken_at=_now(), ids=ids,
                    requests=max(0, _requests_made(src) - before), complete=complete)


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


def hours_until_due(source: str) -> float:
    """Скільки годин лишилось до наступного переліку. 0 — можна зараз."""
    previous = load(source)
    if previous is None:
        return 0.0
    interval = MIN_INTERVAL_HOURS.get(source, DEFAULT_INTERVAL_HOURS)
    age = (_now() - previous.taken_at).total_seconds() / 3600
    return max(0.0, interval - age)


def mark_absent(source: str, current: Snapshot, *, now: datetime | None = None,
                cap: int | None = None) -> dict:
    """Позначки `absent_since` за ПОВНИМ переліком: поставити зниклим, зняти присутнім.

    DOM.RIA — за ключем «domria:<id>» на рядках усіх джерел (LUN теж веде на
    DOM.RIA); інші — за external_id рядків свого джерела. Нові позначки — лише
    актуальним рядкам і не більше `cap` (MAX_CANDIDATES) за прогін; присутнім —
    знімаються з будь-яких рядків. Записується лише absent_since (D43: last_seen —
    тільки pipeline._upsert); пакети ≤ MARK_BATCH рядків.
    """
    now = now or _now()
    cap = MAX_CANDIDATES if cap is None else cap
    if source == "domria":
        prefix = "domria:"
        stmt = select(Listing.id, Listing.site_key, Listing.is_active, Listing.absent_since) \
            .where(Listing.site_key.like(prefix + "%"))
        def present(row):                                       # noqa: E306
            return row[1][len(prefix):] in current.ids
    else:
        stmt = select(Listing.id, Listing.external_id, Listing.is_active, Listing.absent_since) \
            .where(Listing.source == source)
        def present(row):                                       # noqa: E306
            return str(row[1]) in current.ids
    with session_scope() as s:
        rows = s.execute(stmt.order_by(Listing.id)).all()
    to_mark = [r[0] for r in rows if r[2] and r[3] is None and not present(r)]
    to_clear = [r[0] for r in rows if r[3] is not None and present(r)]
    capped = len(to_mark) > cap
    if capped:
        log.warning("%s: зниклих із переліку %d — позначаємо %d за прогін",
                    source, len(to_mark), cap)
        to_mark = to_mark[:cap]
    for ids, value in ((to_mark, now), (to_clear, None)):
        for start in range(0, len(ids), MARK_BATCH):
            chunk = ids[start:start + MARK_BATCH]
            with session_scope() as s:
                s.execute(update(Listing).where(Listing.id.in_(chunk))
                          .values(absent_since=value))
    with session_scope() as s:
        cond = Listing.site_key.like("domria:%") if source == "domria" \
            else Listing.source == source
        total = s.scalar(select(func.count()).select_from(Listing).where(
            cond, Listing.is_active.is_(True), Listing.absent_since.isnot(None))) or 0
    return {"newly_absent": len(to_mark), "back_in_list": len(to_clear),
            "absent_active": int(total), "capped": capped}


def run(sources: list[str] | None = None, confirm: bool = True,
        force: bool = False) -> dict:
    """Повний цикл: перелічити, перевірити на осудність, зберегти, позначити зниклих.

    Нікого не знімає й не перевіряє сам: зниклих бере наступний крок «перевірка
    актуальності» (ярус absent). `confirm` лишився для сумісності викликів і
    нічого не змінює.
    """
    names = [n for n in (sources or REGISTRY) if n in REGISTRY]
    report = {"sources": {}, "requests_enumerate": 0, "newly_absent": 0,
              "back_in_list": 0, "skipped": {}}

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

        entry = {"previous": 0, "current": current.size, "requests": current.requests,
                 "used": False, "complete": current.complete, "reason": "",
                 "candidates": 0, "newly_absent": 0, "back_in_list": 0,
                 "absent_active": None}
        report["requests_enumerate"] += current.requests
        report["sources"][name] = entry
        if not current.complete:
            entry["reason"] = ("перелік неповний (помилки сторінок) — не зберігаю й "
                               "нікого не позначаю")
            log.error("%s: %s", name, entry["reason"])
            continue
        previous = load(name) or baseline_from_db(name)
        diff = compare(previous, current)
        entry.update(previous=diff.previous_size, used=diff.used, reason=diff.reason,
                     candidates=len(diff.candidates))
        if not diff.used:
            continue
        # Зберігаємо лише перелік, який пройшов перевірку на осудність:
        # зіпсований снапшот у ролі бази отруїв би й наступне порівняння.
        save(current)
        marks = mark_absent(name, current)
        entry.update(marks)
        report["newly_absent"] += marks["newly_absent"]
        report["back_in_list"] += marks["back_in_list"]
    return report
