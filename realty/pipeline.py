"""Оркестратор: збір із джерел -> LLM-фолбек -> запис у БД."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import or_, select

from . import ops
from .config import SOURCES, enabled_sources
from .db import init_db, session_scope
from .fetcher import BrowserFetcher, FetchError, Fetcher
from .llm import LLMExtractor
from .models import Condition, Listing, MarketType, PriceEvent
from .normalize import compute_price_per_sqm, to_uah, to_usd
from .sources import REGISTRY
from .sources.base import BaseSource

log = logging.getLogger(__name__)

# Поля, заради яких має сенс турбувати LLM (рівні 2 і 3).
CRITICAL = ("price", "rooms", "area_total")
# Поля, заради яких варто зазирнути на сторінку оголошення, але не платити за
# виклик моделі: сторінка деталей часто містить опис, з якого їх видно.
DESIRABLE = ("market_type", "condition")


def _gaps(rec: dict) -> tuple[bool, bool]:
    """(бракує критичних полів, бракує бажаних)."""
    critical = any(not rec.get(f) for f in CRITICAL)
    desirable = any(
        getattr(rec.get(f), "value", rec.get(f)) in (None, "unknown") for f in DESIRABLE
    )
    return critical, desirable


@dataclass
class RunReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    enriched: int = 0
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    llm_tokens: tuple[int, int] = (0, 0)
    per_source: dict[str, dict] = field(default_factory=dict)

    def render(self) -> str:
        lines = ["", "=" * 62, "ЗВІТ ПРО ЗБІР", "=" * 62]
        for name, st in self.per_source.items():
            lines.append(
                f"  {name:<8} стор.={st.get('pages', 0):<4} зібрано={st.get('kept', 0):<5} "
                f"нових={st.get('new', 0):<5} "
                f"поза містом={st.get('skipped_geo', 0):<5} помилок={st.get('errors', 0)}"
            )
        lines += [
            "-" * 62,
            f"  Додано: {self.inserted}   Оновлено: {self.updated}   "
            f"Пропущено: {self.skipped}",
            f"  Дібрано зі сторінок деталей: {self.enriched}   "
            f"LLM-викликів: {self.llm_calls}"
            + (f" ({self.llm_tokens[0]}+{self.llm_tokens[1]} токенів, "
               f"${self.llm_cost_usd:.4f})" if self.llm_calls else ""),
            "=" * 62,
        ]
        return "\n".join(lines)


class Pipeline:
    def __init__(self, sources: list[str] | None = None, use_llm: bool = True,
                 mode: str = "fresh", start_pages: dict[str, int] | None = None,
                 on_page=None, trigger: str = "cli") -> None:
        self.source_names = sources or enabled_sources()
        # "fresh" — щоденний інкрементальний прогін по свіжих оголошеннях;
        # "full"  — одноразовий історичний збір без стелі глибини.
        self.mode = mode
        self.start_pages = start_pages or {}
        self.on_page = on_page
        # Звідки прийшов прогін: розклад, кнопка в дашборді чи термінал.
        self.trigger = trigger
        self.report = RunReport()
        self.llm = LLMExtractor() if use_llm else None
        self._http: Fetcher | None = None
        self._browser: BrowserFetcher | None = None

    # --- LLM-фолбек -----------------------------------------------------------

    def _get_browser(self, delay: float = 2.5) -> BrowserFetcher:
        """Один браузер на весь прогін: Playwright sync API не дозволяє
        запустити другий екземпляр із контексту першого."""
        if self._browser is None:
            self._browser = BrowserFetcher(delay=delay)
        return self._browser

    def _get_http(self, delay: float = 1.5) -> Fetcher:
        if self._http is None:
            self._http = Fetcher(delay=delay)
        return self._http

    def _page_text(self, rec: dict) -> str | None:
        """Завантажує сторінку оголошення для повторного вилучення."""
        url = rec.get("original_url")
        if not url:
            return None
        cfg = SOURCES.get(rec.get("source", ""))
        try:
            if cfg and cfg.needs_browser:
                return self._get_browser(cfg.delay).render(url, settle_ms=2000)
            return self._get_http(cfg.delay if cfg else 1.5).get(url)
        except FetchError as e:
            log.debug("Сторінку %s не завантажено (%s)", url, e)
            return None
        except Exception as e:
            # Мережевий шар може кинути що завгодно; добір із деталей — річ
            # необов'язкова й ніколи не має зупиняти збір.
            log.warning("Сторінку %s не завантажено (%s): %s", url, type(e).__name__,
                        str(e)[:160])
            return None

    @staticmethod
    def _recompute(rec: dict) -> dict:
        """Перераховує похідні величини після доповнення полів."""
        cur = rec.get("currency") or "USD"
        rec["price_usd"] = to_usd(rec.get("price"), cur)
        rec["price_uah"] = to_uah(rec.get("price"), cur)
        rec["price_per_sqm"] = compute_price_per_sqm(
            rec["price_usd"], rec.get("area_total"), rec.get("price_per_sqm")
        )
        return rec

    def complete(self, rec: dict, src: BaseSource) -> dict:
        """Три рівні вилучення: стрічка -> сторінка деталей -> LLM.

        Сторінка завантажується щонайбільше один раз і обслуговує обидва
        резервні рівні.
        """
        missing_critical, missing_desirable = _gaps(rec)
        if not (missing_critical or missing_desirable):
            return rec

        has_enrich = type(src).enrich is not BaseSource.enrich
        llm_ready = bool(self.llm and self.llm.available)
        # Модель кличемо лише заради критичних полів; бажані того не варті.
        if not has_enrich and not (llm_ready and missing_critical):
            return rec  # діставати нема чим — не турбуємо джерело

        html = self._page_text(rec)
        if not html:
            return rec

        if has_enrich:
            before = dict(rec)
            rec = self._recompute(src.enrich(rec, html))
            if rec.get("detail_enriched") and before != rec:
                self.report.enriched += 1
            missing_critical, _ = _gaps(rec)
        return self._apply_llm(rec, html) if (llm_ready and missing_critical) else rec

    def _apply_llm(self, rec: dict, html: str) -> dict:
        """Доповнює ЛИШЕ відсутні поля; наявні дані парсера не перезаписує."""
        if not self.llm or not self.llm.available:
            return rec
        got = self.llm.extract(html, rec.get("original_url", ""))
        if got is None:
            return rec

        changed = False
        for f in ("price", "rooms", "area_total", "location"):
            if not rec.get(f) and getattr(got, f, None):
                rec[f] = getattr(got, f)
                changed = True
        if got.currency and not rec.get("price") is None and rec.get("currency") in (None, "USD"):
            rec["currency"] = got.currency
        if rec.get("market_type") in (None, MarketType.UNKNOWN) and got.market_type:
            rec["market_type"] = MarketType(got.market_type)
            changed = True
        if rec.get("condition") in (None, Condition.UNKNOWN) and got.condition:
            rec["condition"] = Condition(got.condition)
            changed = True

        if changed:
            rec["llm_extracted"] = True
            self._recompute(rec)
        return rec

    # --- Запис --------------------------------------------------------------

    @staticmethod
    def _upsert(session, rec: dict) -> str:
        """Вставляє або оновлює оголошення. Ключ — (джерело, зовнішній id).

        Зміну ціни фіксуємо окремим записом в історії — саме заради цього
        повторні прогони оновлюють запис, а не створюють новий.
        """
        fields = {c.name for c in Listing.__table__.columns} - {"id", "first_seen"}
        payload = {k: v for k, v in rec.items() if k in fields}
        existing = session.scalar(
            select(Listing).where(
                Listing.source == rec["source"], Listing.external_id == rec["external_id"]
            )
        )
        if existing is None:
            listing = Listing(**payload)
            session.add(listing)
            session.flush()
            session.add(PriceEvent(
                listing_id=listing.id, source=listing.source, price=listing.price,
                currency=listing.currency, price_usd=listing.price_usd,
            ))
            return "inserted"

        old_usd = existing.price_usd
        for k, v in payload.items():
            # Не затираємо вже відомі значення порожніми.
            if v is not None or getattr(existing, k) is None:
                setattr(existing, k, v)
        new_usd = existing.price_usd
        if old_usd is not None and new_usd is not None and abs(old_usd - new_usd) > 1:
            session.add(PriceEvent(
                listing_id=existing.id, source=existing.source, price=existing.price,
                currency=existing.currency, price_usd=new_usd,
            ))
            log.info("Ціна змінилась: %s %s -> %s $", existing.original_url[:60],
                     round(old_usd), round(new_usd))
        return "updated"

    CHUNK = 200          # проміжні коміти, щоб збій не з'їдав години роботи

    def _write(self, batch: list[dict]) -> None:
        """Записує пакет, ізолюючи кожен запис окремою точкою збереження.

        Без цього одна помилка цілісності отруювала транзакцію: подальші
        upsert-и падали, підсумковий commit теж, і `session_scope` відкочував
        увесь пакет. На повному прогоні так було втрачено понад 5 000
        оголошень LUN через єдиний конфлікт посилань.
        """
        for start in range(0, len(batch), self.CHUNK):
            chunk = batch[start:start + self.CHUNK]
            with session_scope() as s:
                for rec in chunk:
                    try:
                        with s.begin_nested():
                            action = self._upsert(s, rec)
                        setattr(self.report, action, getattr(self.report, action) + 1)
                    except Exception as e:
                        self.report.skipped += 1
                        log.warning("Не записано %s: %s",
                                    rec.get("original_url"), str(e)[:160])

    # --- Прогін -------------------------------------------------------------

    @staticmethod
    def _known_ids(session, source: str) -> set[str]:
        """Ідентифікатори джерела, які вже є в базі."""
        return {
            str(x) for x in session.scalars(
                select(Listing.external_id).where(Listing.source == source)
            )
        }

    def _source_for(self, name: str, cache: dict) -> BaseSource | None:
        if name not in cache:
            cls = REGISTRY.get(name)
            if cls is None:
                return None
            cfg = SOURCES.get(name)
            browser = self._get_browser(cfg.delay) if (cfg and cfg.needs_browser) else None
            cache[name] = cls(browser=browser, mode=self.mode)
        return cache[name]

    def backfill(self, limit: int = 300) -> RunReport:
        """Дозбирує вже збережені записи, яким бракує критичних полів.

        Потрібно, коли новий рівень вилучення з'явився після того, як частину
        оголошень уже зібрано: у стрічці джерела їх може вже не бути.
        """
        init_db()
        cache: dict[str, BaseSource] = {}
        updatable = [c.name for c in Listing.__table__.columns
                     if c.name not in ("id", "source", "external_id", "first_seen")]
        with session_scope() as s:
            stmt = select(Listing).where(or_(
                *[getattr(Listing, f).is_(None) for f in CRITICAL],
                Listing.market_type == MarketType.UNKNOWN,
                Listing.condition == Condition.UNKNOWN,
            ))
            if self.source_names:
                stmt = stmt.where(Listing.source.in_(self.source_names))
            rows = s.scalars(stmt.limit(limit)).all()
            log.info("Записів із прогалинами: %d", len(rows))
            for row in rows:
                src = self._source_for(row.source, cache)
                if src is None:
                    continue
                rec = {c.name: getattr(row, c.name) for c in Listing.__table__.columns}
                out = self.complete(rec, src)
                changed = False
                for field in updatable:
                    value = out.get(field)
                    if value is not None and getattr(row, field) != value:
                        setattr(row, field, value)
                        changed = True
                if changed:
                    self.report.updated += 1

        for src in cache.values():
            if src._fetcher is not None:
                src._fetcher.close()
        self._record_llm_cost()
        for obj in (self._http, self._browser):
            if obj is not None:
                obj.close()
        return self.report

    def _llm_snapshot(self) -> tuple[int, int, int, float]:
        if not self.llm:
            return (0, 0, 0, 0.0)
        return (self.llm.calls, self.llm.in_tokens, self.llm.out_tokens, self.llm.cost_usd)

    def _record_source_run(self, run_id: int, name: str, stats: dict,
                           llm_before: tuple, counts_before: tuple,
                           message: str | None = None) -> None:
        """Закриває запис прогону в телеметрії (окрема база, не основна)."""
        calls, tin, tout, cost = self._llm_snapshot()
        req = ops.take_counts(name)
        ops.finish_run(
            run_id,
            status="failed" if (stats.get("errors") or message) else "ok",
            message=message or stats.get("last_error"),
            pages=stats.get("pages", 0), kept=stats.get("kept", 0),
            new=stats.get("new", 0), errors=stats.get("errors", 0),
            inserted=self.report.inserted - counts_before[0],
            updated=self.report.updated - counts_before[1],
            requests_ok=req["ok"], requests_failed=req["failed"],
            requests_blocked=req["blocked"],
            llm_calls=calls - llm_before[0],
            llm_in_tokens=tin - llm_before[1],
            llm_out_tokens=tout - llm_before[2],
            llm_cost_usd=round(cost - llm_before[3], 6),
        )
        ops.beat(f"завершено: {name}", busy=False)

    def _record_llm_cost(self) -> None:
        if not self.llm:
            return
        self.report.llm_calls = self.llm.calls
        self.report.llm_tokens = (self.llm.in_tokens, self.llm.out_tokens)
        self.report.llm_cost_usd = self.llm.cost_usd

    def run(self) -> RunReport:
        init_db()
        for name in self.source_names:
            cls = REGISTRY.get(name)
            if cls is None:
                log.warning("Невідоме джерело: %s", name)
                continue
            log.info("--- Джерело: %s (%s) ---", name, self.mode)
            run_id = ops.start_run(name, self.mode, self.trigger)
            ops.beat(f"збір: {name}", busy=True)
            ops.take_counts(name)          # починаємо лічити з нуля
            llm_before = self._llm_snapshot()
            before = (self.report.inserted, self.report.updated)
            cfg = SOURCES.get(name)
            browser = self._get_browser(cfg.delay) if (cfg and cfg.needs_browser) else None
            with session_scope() as s:
                known = self._known_ids(s, name)
            src = cls(browser=browser, mode=self.mode, known_ids=known,
                      start_page=self.start_pages.get(name, 0), on_page=self.on_page)
            batch: list[dict] = []
            failure: str | None = None
            try:
                for rec in src.run():
                    if not rec:
                        continue
                    batch.append(self.complete(rec, src))
            except Exception as e:
                # Одне джерело не має забирати з собою решту: те, що встигли
                # зібрати, зберігаємо, помилку записуємо, йдемо далі.
                failure = f"{type(e).__name__}: {str(e)[:300]}"
                src.stats["errors"] += 1
                log.exception("Джерело %s перервано: %s", name, failure)
            finally:
                # Запис прогону закривається завжди — інакше він назавжди
                # лишиться «виконується» і дашборд показуватиме хибний
                # зелений сигнал.
                try:
                    self._write(batch)
                except Exception:
                    log.exception("Не вдалося записати пакет %s", name)
                self.report.per_source[name] = dict(src.stats)
                self._record_source_run(run_id, name, src.stats, llm_before, before,
                                        message=failure)
                if src._fetcher is not None:
                    src._fetcher.close()  # браузер спільний — закриємо в кінці

        self._record_llm_cost()
        for obj in (self._http, self._browser):
            if obj is not None:
                obj.close()
        return self.report
