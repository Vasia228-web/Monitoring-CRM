"""Друк інвентаризації у вигляді таблиці для читання людиною."""
from __future__ import annotations

from . import inventory

COND = {"renovated": "з ремонтом", "needs_repair": "без ремонту",
        "unknown": "не визначено"}
MARKET = {"primary": "новобудова", "secondary": "вторинка", "unknown": "не визначено"}


def _dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def _pct(part: int, whole: int) -> str:
    """Частка у відсотках. Порожня база — штатний стан, а не помилка."""
    return f"{round(100 * part / whole)}%" if whole else "—"


def render(data: dict | None = None) -> str:
    d = data or inventory.run()
    m, h, lc, seg = d["masters"], d["history"], d["lifecycle"], d["segments"]
    out: list[str] = []
    add = out.append

    add("=" * 74)
    add("ІНВЕНТАРИЗАЦІЯ БАЗИ ПЕРЕД ПОБУДОВОЮ АНАЛІТИКИ")
    add("=" * 74)

    add("\n1. ОБ'ЄМ")
    add(f"  Унікальних майстер-об'єктів      {m['properties']}")
    add(f"  З них зведені з кількох джерел   {m['merged_from_several_sources']}")
    add(f"  Оголошень без майстер-запису     {m['listings_without_master']}")
    add(f"  Майстрів з відомим районом       {m['with_district']}")

    add("\n2. ГЛИБИНА ІСТОРІЇ ЦІН")
    add(f"  Найраніший запис                 {_dt(h['first'])}")
    add(f"  Найпізніший запис                {_dt(h['last'])}")
    add(f"  Період спостережень              {h['span_days']} днів")
    add(f"  Усього подій ціни                {h['events']}")
    add(f"  Оголошень зі зміною ціни         {h['listings_with_price_change']}")
    add(f"  Майстер-об'єктів зі зміною ціни  {h['masters_with_price_change']}"
        f"  ← по стількох є що будувати")

    add("\n3. ЖИТТЄВИЙ ЦИКЛ")
    add(f"  Оголошень усього                 {lc['listings']}")
    add(f"  Знято з продажу (delisted)       {lc['delisted']} ({lc['delisted_share']}%)")
    add(f"  Хоч раз перевірено на живість    {lc['ever_checked']} ({lc['check_coverage']}%)")
    median = lc["observed_days_median"]
    add(f"  Медіана прожитого до зняття      "
        f"{median if median is not None else '—'} днів (за {lc['delisted']} спостережень)")
    cr = d.get("check_rate") or {}
    add(f"  Темп перевірки за добу           {cr.get('per_day', '—')}")
    add(f"  Повний обхід бази займе          "
        f"{cr.get('full_cycle_days') or '—'} днів")

    add("\n4. СЕГМЕНТИ (кімнатність × стан × ринок), майстер-об'єкти")
    header = "об'єктів"
    add(f"  {'кімнат':<8}{'стан':<14}{'ринок':<14}{header:>9}{'медіана $/м²':>14}")
    for row in seg["table"][:14]:
        rooms = "—" if row["rooms"] is None else (
            f"{row['rooms']}+" if row["rooms"] == 4 else str(row["rooms"]))
        add(f"  {rooms:<8}{COND[row['condition']]:<14}{MARKET[row['market']]:<14}"
            f"{row['n']:>9}{row['median_ppsqm']:>14}")
    s3, s4 = seg["rooms_condition_market"], seg["with_district"]
    add(f"\n  Комбінацій ≥{inventory.REPORT_MIN} об'єктів: "
        f"{s3['combos_ok']} із {s3['combos']} — покривають {s3['covered']} "
        f"із {s3['total']} об'єктів ({_pct(s3['covered'], s3['total'])})")
    add(f"  Те саме з районом:            "
        f"{s4['combos_ok']} із {s4['combos']} — покривають {s4['covered']} "
        f"із {s4['total']} ({_pct(s4['covered'], s4['total'])})")

    sv = d["survivorship"]
    add("\n5. ЧИ МОЖНА БРАТИ ДАТУ ПУБЛІКАЦІЇ ЗА ВІСЬ ЧАСУ")
    add(f"  Оголошень з датою публікації     {sv['with_published_at']}")
    add(f"  Діапазон                         {_dt(sv['earliest'])} … {_dt(sv['latest'])}")
    for key in ("0-30", "31-90", "91-180", "181-365", "365+"):
        add(f"    вік {key:<9} днів              {sv['by_age'].get(key, 0)}")
    add(f"  Частка молодших за 90 днів       {sv['share_last_90d']}%")
    add(f"  Частка старших за рік            {sv['share_older_1y']}%")

    add("\n6. СКЛАД ДЖЕРЕЛ (чому наївне порівняння джерел хибне)")
    add(f"  {'джерело':<10}{'оголошень':>10}{'наївна медіана':>17}{'новобудов':>12}")
    for source, v in d["sources"].items():
        add(f"  {source:<10}{v['n']:>10}{v['naive_median_ppsqm']:>17}"
            f"{str(v['primary_share']) + '%':>12}")

    add("\n7. ЗАЛЕЖНІСТЬ ЦІНИ ЗА м² ВІД ПЛОЩІ (всередині однієї кімнатності)")
    for rooms, v in d["area_effect"].items():
        add(f"  {rooms}к (n={v['n']}): медіани $/м² по квартилях площі "
            f"{v['band_medians']} — розкид {v['spread_pct']}%")
    return "\n".join(out)
