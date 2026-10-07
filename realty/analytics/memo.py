"""Пам'ять знімка «Аналітики»: що вже пораховано для цього знімка, не рахуємо вдруге.

Навіщо. Сторінка «Аналітика» на кожен запит заново рахувала те, що від запиту
не залежить: пари «новобудова/вторинка», дні на ринку, проксі ліквідності й
зрізи (по кімнатах, стану, ринку — кожен ДВІЧІ), а крива виживання для
фільтра — повним перерахунком. Знімок (`cache.Snapshot.universe`) між
запитами той самий, тож і результат той самий: досить порахувати раз і
тримати поруч зі знімком. Новий знімок — нова пам'ять (вона живе в об'єкті
`Universe`), тож застарілих чисел тут бути не може: що пам'ять, що
перерахунок дають один і той самий результат з того самого знімка.

Ключі, що приходять із запиту (фільтри «Аналітики»), — це довільні рядки
відвідувача, тому кривих тримаємо не більше за `analytics.km_memo_keys` із
config/speed.toml (LRU): 80 комбінацій фільтрів + 39 сегментів на копії
Етапу 0 = 119 → 128 (D48).

Значення в пам'яті СПІЛЬНІ між запитами: той, хто їх отримав, не змінює їх
(сторінка квартири копіює словник кривої, перш ніж дописати підпис сегмента).
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Hashable, TypeVar

T = TypeVar("T")


def curve_capacity() -> int:
    """Скільки кривих тримати — з config/speed.toml (суворий завантажувач, D47 п. 1).

    ConfigError іде далі навмисно: виклик у `segments._remembered` тоді рахує
    криву без пам'яті, а не валить сторінку (D49).
    """
    from .. import configfiles

    return configfiles.get("speed").analytics.km_memo_keys


class Memo:
    """Невеликий LRU з лічильниками влучань — один на знімок.

    Обчислення — ПОЗА замком: два одночасні запити з однаковим ключем можуть
    порахувати двічі (результат однаковий, бо вхід — той самий знімок), але
    жоден запит не чекає на чужий довгий розрахунок під замком.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: OrderedDict[Hashable, object] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable, compute: Callable[[], T], capacity: int) -> T:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]          # type: ignore[return-value]
        value = compute()
        with self._lock:
            self.misses += 1
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > max(1, capacity):
                self._data.popitem(last=False)
        return value

    def __len__(self) -> int:
        return len(self._data)
