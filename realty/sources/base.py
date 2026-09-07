"""Спільний контракт для всіх джерел."""
from __future__ import annotations

import abc
import logging
from typing import Iterator

from ..config import SOURCES, SourceConfig
from ..fetcher import BrowserFetcher, Fetcher
from ..models import Condition, MarketType
from ..normalize import compute_price_per_sqm, to_uah, to_usd

log = logging.getLogger(__name__)


class BaseSource(abc.ABC):
    """Джерело віддає словники, готові до запису в `Listing`.

    Обов'язки нащадка: сходити по дані, розібрати їх і залишити ЛИШЕ об'єкти
    в Івано-Франківську. Нормалізацію валют і ціни за м² робить `finalize`.
    """

    name: str = "base"

    def __init__(self, fetcher: Fetcher | None = None,
                 browser: BrowserFetcher | None = None, mode: str = "fresh",
                 known_ids: set[str] | None = None, start_page: int = 0,
                 on_page=None) -> None:
        self.cfg: SourceConfig = SOURCES.get(self.name, SourceConfig(self.name)).paced(mode)
        self.mode = mode
        # Ідентифікатори, які вже є в базі: за ними інкрементальний прогін
        # розуміє, чи є на сторінці щось нове.
        self.known_ids: set[str] = known_ids or set()
        # Що фактично побачили в цьому прогоні — потрібно, щоб після повного
        # обходу зрозуміти, які оголошення з видачі зникли.
        self.seen_ids: set[str] = set()
        self._fetcher = fetcher
        self._browser = browser
        self._page_new = 0
        self._stop = False
        # Повний збір триває годинами, тому має відновлюватись із тієї
        # сторінки, на якій його перервали.
        self.start_page = start_page
        self.current_page = start_page
        self._on_page = on_page
        self.stats = {"seen": 0, "kept": 0, "new": 0, "skipped_geo": 0,
                      "errors": 0, "needs_llm": 0, "pages": 0}

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher(delay=self.cfg.delay, label=self.name)
        return self._fetcher

    @property
    def browser(self) -> BrowserFetcher:
        if self._browser is None:
            self._browser = BrowserFetcher(delay=self.cfg.delay, label=self.name)
        return self._browser

    # --- Посторінковий обхід --------------------------------------------------

    @property
    def stop_requested(self) -> bool:
        """Чи припинити обхід достроково."""
        return self._stop

    def begin_page(self, page: int | None = None) -> None:
        self._page_new = 0
        self.stats["pages"] += 1
        if page is not None:
            self.current_page = page

    def end_page(self) -> None:
        """Оцінює сторінку й вирішує, чи є сенс іти далі.

        Рання зупинка дозволена лише джерелам, які віддають найновіші
        оголошення першими. Для решти сторінка без новинок нічого не означає:
        нове може лежати й глибше.
        """
        if self._on_page is not None:
            self._on_page(self.name, self.current_page)
        if self.mode == "fresh" and self.cfg.supports_recency and self._page_new == 0:
            log.info("%s: сторінка без нових оголошень — зупиняємось", self.name)
            self._stop = True

    @abc.abstractmethod
    def iter_listings(self) -> Iterator[dict]:
        """Видає сирі, вже розібрані записи (по одному оголошенню)."""

    def finalize(self, rec: dict) -> dict:
        """Доводить запис до схеми: валюти, ціна за м², типи enum."""
        rec.setdefault("source", self.name)
        currency = (rec.get("currency") or "USD").upper()
        price = rec.get("price")
        rec["currency"] = currency
        rec["price_usd"] = to_usd(price, currency)
        rec["price_uah"] = to_uah(price, currency)
        rec["price_per_sqm"] = compute_price_per_sqm(
            rec.get("price_usd"), rec.get("area_total"), rec.get("price_per_sqm")
        )
        if not isinstance(rec.get("market_type"), MarketType):
            rec["market_type"] = MarketType(rec.get("market_type") or "unknown")
        if not isinstance(rec.get("condition"), Condition):
            rec["condition"] = Condition(rec.get("condition") or "unknown")
        return rec

    def enrich(self, rec: dict, html: str) -> dict:
        """Рівень 2: доповнити запис зі сторінки оголошення.

        Типово нічого не робить — джерела, чиї стрічки вже віддають повні
        дані, цього не потребують. Перевизначає той, кому є що дібрати.
        """
        return rec

    @staticmethod
    def is_complete(rec: dict) -> bool:
        """Чи вистачає полів, щоб не смикати LLM-фолбек."""
        return bool(rec.get("price") and rec.get("rooms") and rec.get("area_total"))

    def run(self) -> Iterator[dict]:
        """Обгортка з підрахунком статистики та ізоляцією помилок."""
        try:
            for rec in self.iter_listings():
                self.stats["seen"] += 1
                if not rec.get("original_url"):
                    continue
                self.seen_ids.add(str(rec.get("external_id")))
                if str(rec.get("external_id")) not in self.known_ids:
                    self.stats["new"] += 1
                    self._page_new += 1
                out = self.finalize(rec)
                if not self.is_complete(out):
                    self.stats["needs_llm"] += 1
                self.stats["kept"] += 1
                yield out
        except Exception as e:
            self.stats["errors"] += 1
            # Текст помилки потрібен дашборду: інакше прогін позначений як
            # невдалий, але без жодної підказки, що саме сталося.
            self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            log.exception("Джерело %s впало: %s", self.name, e)
