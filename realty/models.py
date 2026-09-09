"""Схема даних: одне оголошення про продаж квартири."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, Index, case,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    manual_active: Mapped[bool | None] = mapped_column(Boolean, index=True)
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, index=True)

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
    quality_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
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
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Listing {self.source}:{self.external_id} {self.price_usd}$ {self.rooms}к>"


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
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    listings: Mapped[list["Listing"]] = relationship(
        "Listing", backref="property", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Property {self.rooms}к {self.area_total}м² "
                f"{self.street or '?'} × {self.sources_count} джерел>")


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
