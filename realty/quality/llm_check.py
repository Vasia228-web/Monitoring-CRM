"""Незалежна перевірка LLM-фолбеку: відповідь моделі звіряється з парсером.

Модель кличемо, коли парсеру бракує критичних полів. Але зазвичай ЧАСТИНУ
полів парсер таки дістав — і це безкоштовний контроль: якщо модель по-іншому
прочитала ціну чи площу, які парсер узяв зі структурованого поля сайту, то й
кімнатам, які вона «доповнила», вірити нема підстав. Без цієї звірки модель
фактично судила сама себе.

Правило консервативне: розбіжність у будь-якому спільному полі — нічого з
відповіді моделі не беремо, запис іде в карантин із поясненням.
"""
from __future__ import annotations

from ..models import Condition, MarketType
from ..normalize import to_usd

# Допуски: ціна — округлення й перерахунок валют; площа — «85» проти «85,4».
PRICE_REL_TOL = 0.03
AREA_ABS_TOL = 1.0
AREA_REL_TOL = 0.03


def _value(x):
    return getattr(x, "value", x)


def compare(rec: dict, got) -> tuple[list[str], list[str]]:
    """Повертає (поля, які вдалося звірити; описи розбіжностей).

    Звіряємо лише там, де щось дали ОБИДВА — порожнє поле парсера модель
    якраз і мала заповнити, з ним звіряти нічого.
    """
    compared: list[str] = []
    conflicts: list[str] = []

    if rec.get("price") and getattr(got, "price", None):
        mine = to_usd(rec["price"], (rec.get("currency") or "USD").upper())
        theirs = to_usd(got.price, (got.currency or rec.get("currency") or "USD").upper())
        if mine and theirs:
            compared.append("price")
            if abs(mine - theirs) > PRICE_REL_TOL * max(mine, theirs):
                conflicts.append(f"ціна: парсер {rec['price']:,.0f} {rec.get('currency') or 'USD'}, "
                                 f"LLM {got.price:,.0f} {got.currency or '?'}")

    if rec.get("rooms") and getattr(got, "rooms", None):
        compared.append("rooms")
        if int(rec["rooms"]) != int(got.rooms):
            conflicts.append(f"кімнат: парсер {rec['rooms']}, LLM {got.rooms}")

    if rec.get("area_total") and getattr(got, "area_total", None):
        compared.append("area_total")
        a, b = float(rec["area_total"]), float(got.area_total)
        if abs(a - b) > max(AREA_ABS_TOL, AREA_REL_TOL * max(a, b)):
            conflicts.append(f"площа: парсер {a:g} м², LLM {b:g} м²")

    mine_market = _value(rec.get("market_type"))
    theirs_market = getattr(got, "market_type", None)
    if (mine_market not in (None, MarketType.UNKNOWN.value)
            and theirs_market in (MarketType.PRIMARY.value, MarketType.SECONDARY.value)):
        compared.append("market_type")
        if mine_market != theirs_market:
            conflicts.append(f"ринок: парсер {mine_market}, LLM {theirs_market}")

    mine_cond = _value(rec.get("condition"))
    theirs_cond = getattr(got, "condition", None)
    if (mine_cond not in (None, Condition.UNKNOWN.value)
            and theirs_cond in (Condition.RENOVATED.value, Condition.NEEDS_REPAIR.value)):
        compared.append("condition")
        if mine_cond != theirs_cond:
            conflicts.append(f"стан: парсер {mine_cond}, LLM {theirs_cond}")

    return compared, conflicts
