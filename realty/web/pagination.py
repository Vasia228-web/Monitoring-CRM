"""Перелистування списку.

Два правила, без яких пагінація непомітно бреше.

По-перше, **стабільний порядок**. У базі повно записів з однаковою ціною: у
видачі підряд стоять кілька по $146 500. Якщо сортувати тільки за ціною,
порядок усередині такої групи не визначений, і база може віддавати їх щоразу
по-різному. Наслідок — при перелистуванні одні записи з'являються двічі, інші
зникають зовсім, і виглядає це як загадковий баг. Тому в кінці кожного
сортування стоїть унікальний ключ.

По-друге, **нарізка робиться базою**, а не в пам'яті: вибирати 15 тисяч
записів, щоб показати сорок, — це і повільно, і зайва пам'ять на кожен
перегляд.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

PAGE_SIZES = (50, 100, 200)
DEFAULT_PAGE_SIZE = 50
# Скільки номерів показувати навколо поточного, перш ніж згортати середину.
WINDOW = 2


def clamp_size(value) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE


def clamp_page(value, pages: int) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(page, max(1, pages)))


@dataclass(frozen=True)
class Page:
    """Стан перелистування — усе, що потрібно шаблону."""

    number: int
    size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.size)) if self.total else 1

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def first(self) -> int:
        return self.offset + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.offset + self.size, self.total)

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    def numbers(self, window: int = WINDOW) -> list[int | None]:
        """Номери зі згорнутою серединою: 1 … 4 5 6 … 310.

        `None` означає пропуск. Перша й остання сторінки видимі завжди — без
        них не дістатися до країв списку одним кліком.
        """
        pages = self.pages
        if pages <= 2 * window + 5:
            return list(range(1, pages + 1))
        shown = {1, pages}
        shown.update(range(max(1, self.number - window),
                           min(pages, self.number + window) + 1))
        out: list[int | None] = []
        previous = 0
        for n in sorted(shown):
            if previous and n > previous + 1:
                out.append(None)
            out.append(n)
            previous = n
        return out


def build(total: int, page, per_page) -> Page:
    size = clamp_size(per_page)
    pages = max(1, math.ceil(total / size)) if total else 1
    return Page(number=clamp_page(page, pages), size=size, total=total)
