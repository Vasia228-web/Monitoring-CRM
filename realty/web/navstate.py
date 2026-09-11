"""Перенесення стану фільтрів між вкладками.

Стан живе в адресі — це дає і кнопку «назад», і перезавантаження, і
посилання, яке можна передати. Але цього замало: якщо посилання у верхній
навігації ведуть на голі `/` та `/processing`, перехід між вкладками стирає
все, що людина щойно вибрала. Саме через це вимога «стан у URL» виглядала
виконаною, хоча на практиці стан не переживав жодного кліку по вкладці.

Тут задано, які параметри розуміє кожна сторінка, і будуються посилання, що
несуть стан із собою — рівно в тому обсязі, який цільова сторінка вміє
прочитати.
"""
from __future__ import annotations

from urllib.parse import urlencode

# Порядок ключів фіксований, щоб адреса не змінювалась від перестановки —
# інакше однакові вибірки давали б різні посилання.
LIST_KEYS = ("condition", "market", "source", "rooms",
             "price_min", "price_max", "sort", "per_page", "page")
ANALYTICS_KEYS = ("rooms", "condition", "market")

# Які параметри розуміє кожна сторінка. Сторінка стану не приймає нічого.
PAGE_KEYS: dict[str, tuple[str, ...]] = {
    "/": LIST_KEYS,
    "/processing": LIST_KEYS,
    "/analytics": ANALYTICS_KEYS,
    "/status": (),
}

# Значення, які нічого не фільтрують: у адресу їх не пишемо, щоб посилання
# лишалось читабельним.
EMPTY = ("", None)


def carry(path: str, state: dict | None) -> str:
    """Адреса сторінки `path` зі станом, який вона здатна прочитати."""
    keys = PAGE_KEYS.get(path)
    if not keys or not state:
        return path
    pairs = []
    for key in keys:
        value = state.get(key)
        if value in EMPTY:
            continue
        # Перша сторінка й типовий розмір — це і є замовчування, писати їх зайве.
        if key == "page" and str(value) == "1":
            continue
        pairs.append((key, value))
    query = urlencode(pairs, doseq=False)
    return f"{path}?{query}" if query else path


def reset_url(path: str) -> str:
    """Скидання фільтрів лишає людину на тій самій сторінці.

    Раніше кнопка вела на `/` незалежно від того, звідки її натиснули, тож зі
    сторінки «В обробці» вона працювала як «піти звідси».
    """
    return path if path in PAGE_KEYS else "/"
