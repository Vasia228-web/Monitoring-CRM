#!/usr/bin/env python
"""Чернетка довідника районів і ЖК з копії бази — лише читання (Блок 4, крок E10, D57).

  python scripts/places_seed.py --db КОПІЯ.db --out ТЕКА [--report звіт.json]

Пише ТЕКА/districts.toml і ТЕКА/complexes.toml (config/places/rules.toml береться з
репозиторію — за ним нормалізуються назви). Це ЧЕРНЕТКА: результат переглядається
очима й комітиться в config/places/; далі довідник правиться в TOML (нове написання —
псевдонім із рядком у Deviations), а не цим скриптом.

Що робить сам (за даними, з доказом у коментарях TOML):
  * райони DOM.RIA (listings.district) — по одному запису; мітки LUN (сегмент
    `location`), що збігаються з ними після нормалізації (латинська «i» тощо), —
    автоматично;
  * мікрорайони, які є лише в LUN, — батьківський район DOM.RIA лише за доказом
    ≥ rules.link.min_share при n ≥ rules.link.min_n у квартирах, де є обидва джерела
    (внутрішнє рішення D57); інакше — окремий район. Голос квартири — найчастіший
    район DOM.RIA серед назв, які довідник визнає районом: «ЖК Винагородний» у полі
    району (ignore) чи невідома назва не голосують (рецензія E10: Патріот рахувався
    7/9 замість 7/8);
  * ЖК — по id DOM.RIA (identity.complex); id з тією самою назвою й точками ближче
    MERGE_M — один ЖК (Lake Park 12652/9160); з різними точками — різні ЖК з
    уточненням у назві, а спільна назва — ні до кого (Затишний, Парковий);
  * прив'язка ЖК → район: голоси поля району DOM.RIA рядків цього id і міток LUN у
    квартирах із ним — лише назви районів (ignore й невідомі не голосують), мікрорайон
    із батьком — за батька (Патріот → Бам); ≥ min_share при n ≥ min_n.

Що взято з ручного перегляду Етапу 0 (D45, wf1_results.json «districts») — таблиці
нижче: села громади, орієнтири й POI, вулиці-мітки, парасольки й черги ЖК, ЖК лише з
тексту, ручні псевдоніми (≈30), «Міське озеро» — орієнтир, не район (D57).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles  # noqa: E402
from realty.places.normalize import Normalizer  # noqa: E402

# --- Ручний перегляд Етапу 0 ---------------------------------------------------------------

# Райони DOM.RIA (словник закритий: 20 назв; Етап 0) → стабільні ключі.
RIA_DISTRICTS = {
    "Центр": "tsentr", "Княгинин": "kniahynyn", "Пасічна": "pasichna",
    "Набережна": "naberezhna", "Каскад": "kaskad", "Майзлі": "maizli",
    "Арсенал": "arsenal", "Бам": "bam", "Брати": "braty", "Софіївка": "sofiivka",
    "Опришівці": "opryshivtsi", "Кант": "kant",
    "Коновальця Чорновола": "konovaltsia-chornovola", "Позитрон": "pozytron",
    "Рінь": "rin", "Будівельників": "budivelnykiv", "Городок": "horodok",
    "Гірка": "hirka", "Вокзал": "vokzal", "Кішлак": "kishlak",
}
# Мікрорайони, що є лише в LUN (план Блоку 4) — батько лише за доказом.
LUN_ONLY = {
    "Німецька колонія": "nimetska-koloniia", "Бельведер": "belveder", "Патріот": "patriot",
    "Новий світ": "novyi-svit", "Південний бульвар": "pivdennyi-bulvar",
    "Тисменицька": "tysmenytska", "Надрічна": "nadrichna", "Залізничний": "zaliznychnyi",
    "Чорновола": "chornovola",
}
# Села Івано-Франківської міської громади (рішення власника 4, D46): окремі «райони» з
# позначкою «громада»; склад не звірено з КАТОТТГ (verified = false).
VILLAGES = {
    "Крихівці": "krykhivtsi", "Вовчинець": "vovchynets", "Микитинці": "mykytyntsi",
    "Угорники": "uhornyky", "Хриплин": "khryplyn", "Чукалівка": "chukalivka",
    "Угринів": "uhryniv", "Підпечери": "pidpechery", "Загвіздя": "zahvizdia",
    "Черніїв": "cherniiv",
}
# Поза громадою: видно в «усе», не видно в «тільки місто» (D57).
OUTSIDE = {"Лисець": "lysets", "Дніпропетровська область": "dnipropetrovska-oblast"}
# Відомі назви, що не є районом.
IGNORE = [
    ("Міське озеро", "landmark"),   # орієнтир-місцевість: перекриває Набережну, Центр, Кант, Бам
    ("Івано-Франківськ", "city"), ("Івано-Франківська область", "region"),
    ("Івано-Франківський район", "region"),
    ("ЖК Винагородний", "complex"),
    # POI LUN (поле poi.name і мітки) — найближчий орієнтир, не район.
    ("ТЦ Панорама PLAZA", "poi"), ("Парк ім. Тараса Шевченка", "poi"),
    ("Франківський драмтеатр", "poi"), ("Драмтеатр", "poi"),
    ("Собор Преображення Господнього", "poi"), ('ТЦ "Велмарт"', "poi"), ("Велмарт", "poi"),
    ('ТРЦ "Велес"', "poi"), ("ТЦ Велес", "poi"), ('ТЦ "Арсен"', "poi"),
    ("Залізничний вокзал", "poi"), ("Парк воїнів-визволителів", "poi"),
    ("Парк Воїнів-інтернаціоналістів", "poi"), ("Івано-Франківська ратуша", "poi"),
    ("Ратуша", "poi"), ("Стометрівка", "poi"), ("ТЦ White", "poi"), ("ТЦ Пасаж", "poi"),
    ("ТЦ Флагман", "poi"), ("Епіцентр", "poi"), ("Metro", "poi"),
    ("Меморіальний сквер", "poi"), ("Площа Ринок", "poi"),
    ("Пам'ятник Степану Бандері", "poi"), ("Кладовище", "poi"), ("Ковзанка Цунамі", "poi"),
    # Вулиці на місці мітки LUN.
    ("Набережна вулиця ім. Василя Стефаника", "street"),
    ("Набережна імені Василя Стефаника вулиця", "street"),
    ("Набережна р. Бистриця Надвірнянська", "street"), ("Галицька вулиця", "street"),
    ("Левинського І. вулиця", "street"), ("Хриплинська вулиця", "street"),
    ("Стуса Василя вулиця", "street"), ("Ленкавського вулиця", "street"),
    ("Комунальна вулиця", "street"), ("Надрічна вулиця", "street"),
    ("Княгинин вулиця", "street"),
]

# ЖК: ручні рішення.
# Парасольки («житловий район»): поступаються конкретному ЖК усередині.
UMBRELLAS = {
    7504: ("kniahynyn", "Житловий район Княгинин"),   # «ЖК Княгинин» DOM.RIA — уся забудова
}
EXTRA_UMBRELLAS = [("manhattan-district", "Житловий район Manhattan")]   # LUN geoEntities
WITHIN = {8216: "kniahynyn", 5556: "manhattan-district", 7845: "manhattan-district"}
# Однакові id з різними назвами: назва з тим самим id і тими самими точками — псевдонім.
SAME_ID_ALIASES = {11599: ["вул. Гетьмана Дорошенка, 28а"],
                   7504: ["Житловий район Княгинин"]}
# Черги одного ЖК (вид перевірки complex_phase), крім знайдених автоматично.
GROUPS = {5556: "manhattan", 7845: "manhattan"}
# ЖК, яких немає в полях DOM.RIA (Благо, LUN, лише текст; Етап 0).
EXTRA_COMPLEXES = [
    "Noveli", "Bydlení Pražská",
    "Долішній", "Краківський", "Європейський Сіті", "Асторія", "Wawel", "Royal Hall",
    "Perfect House", "Місто Мрій", "Новатор", "Веселка Річкова", "Premiere", "Комфорт Зона",
    "Калинова Слобода",
    # «ЖК Винагородний» стоїть у полі району DOM.RIA (ignore, вид complex) — як доказ ЖК
    # його бере resolve, тож сутність потрібна (рецензія E10).
    "Винагородний",
]
# Ручні псевдоніми (Етап 0, ≈30; перевірено очима за списком назв). Назва → назва в полі.
ALIASES = {
    "City by blago": "City", "Сіті Благо": "City", "Сіті by blago": "City", "Сіті": "City",
    "Юван": "U One", "Ю Ван": "U One", "UOne": "U One", "Юніон": "Union",
    "Мануфактура": "Містечко Мануфактура", "Віденський": "Квартал Віденський",
    "Магнолія": "Magnolia Park", "Магнолія Парк": "Magnolia Park",
    "Гідропарк": "HydroPark DeLuxe", "Шоколад": "Сhocolate", "Chocolate": "Сhocolate",
    "Галицький": "Квартал Галицький", "Галицький 2": "Квартал Галицький-2",
    "Різдвяний": "Квартал Різдвяний", "Прованс": "Provance Home",
    "Клубне 12": "Клубне містечко 12", "Княгинин-Центр": "Kniahynyn-Center",
    "Княгинин Центр": "Kniahynyn-Center", "Містечко Соборне": "Соборне",
    "Дем'янів Лаз": "Левада Дем'янів Лаз", "Містечко Паркове": "Паркове містечко",
    "Gr Hall": "Grand Hall", "Гранд Хол": "Grand Hall", "Comft City": "Комфорт Сіті",
    "Comfort City": "Комфорт Сіті", "Городок Южный": "Містечко Південне",
    "Park Avenue": "Park Avenue premium",
    "Парк Авеню": "Park Avenue premium", "Вавель": "Wawel", "Лайт Хоум": "Light home",
    # Етап 0, таблиця «15 найгірших прикладів».
    "Манхеттен": "Manhattan", "Манхетен": "Manhattan", "Манхетин": "Manhattan",
    "Manhatten": "Manhattan", "Манхеттен Ап": "Manhattan Up", "Manhattan AP": "Manhattan Up",
    "Скайгарден": "SKYGARDEN", "Скай Гарден": "SKYGARDEN", "Sky Garden": "SKYGARDEN",
    "Skaygarden": "SKYGARDEN", "Шепіт": "SHEPIT", "Сенат": "Senat", "Соната": "Sonata",
    "Фемілі Плаза": "Family Plaza", "Фемелі Плаза": "Family Plaza",
    "Familly Plaza": "Family Plaza", "Фемілі Плаза 2": "Family Plaza 2",
    "Familly Plaza 2": "Family Plaza 2", "Kniahynyn Center": "Kniahynyn-Center",
    # Російські написання й кирилиця латинських назв (OLX «Назва ЖК» — вільний текст;
    # рецензія E10). Голе «Липки» — НЕ псевдонім: так пишуть і Липки 2, і Липки PREMIUM.
    "Манхэттен": "Manhattan", "Скандинавия": "Скандинавія",
    "Местечко Центральное": "Містечко Центральне", "Сентрал Парк": "Central Park",
    "Комфорт Парк": "Comfort Park",
}
# Псевдоніми районів, яких немає серед міток LUN (російські написання; рецензія E10).
DISTRICT_ALIASES = {"pasichna": ["Пасечная"]}
# Точки двох id з однаковою назвою ближче за це — один ЖК (Етап 0: Lake Park 12652/9160
# і Урожайний 9157/9678 — та сама точка; Затишний і Парковий — різні вулиці).
MERGE_M = 150.0

TR = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
      "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
      "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
      "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
      "ю": "iu", "я": "ia", "ы": "y", "э": "e", "ё": "e", "ъ": "", "'": "", "’": ""}


def slug(text: str) -> str:
    t = "".join(TR.get(ch, ch) for ch in text.lower())
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return re.sub(r"-+", "-", t)[:60].strip("-") or "x"


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def toml_list(items) -> str:
    return "[" + ", ".join(toml_str(x) if isinstance(x, str) else str(x) for x in items) + "]"


def lun_label(loc: str | None) -> str | None:
    """Мітка місцевості LUN — останній сегмент `location` (прототип Етапу 0, gaz.lun_area)."""
    if not loc:
        return None
    segs = [s.strip() for s in loc.split(",") if s.strip()]
    if not segs:
        return None
    if len(segs) == 1:
        return None if segs[0] == "Івано-Франківськ" else segs[0]
    if len(segs) == 2:
        last = segs[1]
        if re.search(r"\d", last) or last.lower().startswith("будинок"):
            return None
        return last
    return segs[-1]


def dist_m(a, b) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 111_320, (a[1] - b[1]) * 111_320 * math.cos(lat))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--report")
    args = p.parse_args()
    rules = configfiles.load("places/rules")
    norm = Normalizer(rules.normalize)
    link_share, link_n = rules.link.min_share, rules.link.min_n
    con = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT id, source, property_id, district, location, complex_name, "
        "json_extract(identity, '$.complex'), json_extract(identity, '$.lat'), "
        "json_extract(identity, '$.lon'), coalesce(manual_active, is_active) "
        "FROM listings").fetchall()
    report: dict = {}

    # --- райони ------------------------------------------------------------------------------
    ria_by_prop: dict[int, Counter] = defaultdict(Counter)
    lun_by_prop: dict[int, Counter] = defaultdict(Counter)
    label_counts: Counter = Counter()
    ria_counts: Counter = Counter()
    # Голос DOM.RIA — лише назва, яку довідник визнає районом (канонічна назва); «ЖК
    # Винагородний» у полі району, невідомі назви — не голосують (рецензія E10).
    ria_canon = {norm.key(n, "district"): n for n in RIA_DISTRICTS}

    def ria_name(raw: str | None) -> str | None:
        return ria_canon.get(norm.key(raw, "district")) if raw else None

    for _id, src, pid, dist, loc, *_ in rows:
        if src == "domria" and dist:
            ria_counts[dist] += 1
            if pid and ria_name(dist):
                ria_by_prop[pid][ria_name(dist)] += 1
        if src == "lun":
            lab = lun_label(loc)
            if lab:
                label_counts[lab] += 1
                if pid:
                    lun_by_prop[pid][lab] += 1
    ria_key = {norm.key(n, "district"): k for n, k in RIA_DISTRICTS.items()}
    # Мітка LUN → район DOM.RIA тієї ж квартири (голоси за квартирами).
    cross: dict[str, Counter] = defaultdict(Counter)
    for pid, labs in lun_by_prop.items():
        if pid not in ria_by_prop:
            continue
        top = ria_by_prop[pid].most_common(1)[0][0]
        for lab in labs:
            cross[lab][top] += 1
    report["lun_label_vs_ria"] = {lab: dict(c) for lab, c in cross.items()}

    def evidence(lab_names) -> Counter:
        c = Counter()
        for lab, cnt in cross.items():
            if norm.key(lab, "district") in {norm.key(x, "district") for x in lab_names}:
                c.update(cnt)
        return c

    districts = []        # (key, name, area, parent, verified, aliases, comment)
    ignore_keys = {norm.key(n, "district") for n, _ in IGNORE}
    known_keys = set(ignore_keys)
    aliases_of: dict[str, list[str]] = defaultdict(list)
    for lab in sorted(label_counts):
        k = norm.key(lab, "district")
        if k in ria_key and lab not in RIA_DISTRICTS:
            aliases_of[ria_key[k]].append(lab)
    for name, key in RIA_DISTRICTS.items():
        ev = evidence([name])
        n = sum(ev.values())
        agree = ev.get(name, 0)
        comment = (f"DOM.RIA: {ria_counts.get(name, 0)} оголошень за весь час; мітки LUN "
                   f"({', '.join([name] + sorted(set(aliases_of[key]) - {name}))}): "
                   f"{sum(label_counts[l] for l in label_counts if norm.key(l, 'district') == norm.key(name, 'district'))}"
                   + (f"; у квартирах з DOM.RIA збіг {agree}/{n} ({agree / n:.0%})" if n else ""))
        districts.append((key, name, "city", "", True,
                          sorted(set(aliases_of[key]) - {name}) + DISTRICT_ALIASES.get(key, []),
                          comment))
        known_keys.add(norm.key(name, "district"))
    parents = {}
    for name, key in LUN_ONLY.items():
        ev = evidence([name])
        n = sum(ev.values())
        top, k = ev.most_common(1)[0] if ev else (None, 0)
        parent = RIA_DISTRICTS.get(top) if n >= link_n and k / max(n, 1) >= link_share else ""
        parents[name] = (parent, top, k, n)
        comment = (f"лише LUN: {sum(label_counts[l] for l in label_counts if norm.key(l, 'district') == norm.key(name, 'district'))} "
                   f"оголошень; у квартирах з DOM.RIA: "
                   + (", ".join(f"{d} {c}" for d, c in ev.most_common(4)) if ev else "немає")
                   + (f" → батько {top} ({k}/{n} = {k / n:.0%} ≥ {link_share:.0%}, n ≥ {link_n})"
                      if parent else
                      f" → окремий район (доказ {k}/{n} < {link_share:.0%} чи n < {link_n})"
                      if n else " → окремий район (доказу немає)"))
        aliases = sorted({lab for lab in label_counts
                          if norm.key(lab, "district") == norm.key(name, "district")} - {name})
        aliases += DISTRICT_ALIASES.get(key, [])
        districts.append((key, name, "city", parent, True, aliases, comment))
        known_keys.add(norm.key(name, "district"))
    # Мікрорайон із батьком голосує за батька (прив'язка ЖК → район нижче).
    rollup = {name: v[1] for name, v in parents.items() if v[0]}
    for table, area, verified in ((VILLAGES, "hromada", False), (OUTSIDE, "outside", False)):
        for name, key in table.items():
            labs = sorted({lab for lab in label_counts
                           if norm.key(lab, "district") == norm.key(name, "district")} - {name})
            cnt = sum(label_counts[l] for l in label_counts
                      if norm.key(l, "district") == norm.key(name, "district"))
            what = ("село громади (рішення власника 4, D46); склад не звірено з КАТОТТГ"
                    if area == "hromada" else "поза громадою (D57)")
            districts.append((key, name, area, "", verified, labs,
                              f"{what}; міток LUN: {cnt}"))
            known_keys.add(norm.key(name, "district"))
    unknown_labels = {lab: n for lab, n in label_counts.items()
                      if norm.key(lab, "district") not in known_keys}
    report["unknown_labels"] = unknown_labels

    # --- ЖК ----------------------------------------------------------------------------------
    by_id: dict[int, dict] = {}
    blago_names: Counter = Counter()
    other_names: Counter = Counter()
    for _id, src, pid, dist, loc, cname, cid, lat, lon, active in rows:
        if src == "blago" and cname:
            blago_names[cname] += 1
        if src == "domria" and cid and str(cid).startswith("ria:"):
            rid = int(str(cid).split(":")[1])
            e = by_id.setdefault(rid, {"names": Counter(), "votes": Counter(), "pts": [],
                                       "props": set(), "n": 0})
            e["n"] += 1
            if cname:
                e["names"][cname] += 1
            if ria_name(dist):
                e["votes"][ria_name(dist)] += 1
            if lat and lon:
                e["pts"].append((lat, lon))
            if pid:
                e["props"].add(pid)
        elif src == "domria" and cname:
            other_names[cname] += 1
    # Голоси міток LUN у квартирах з цим id.
    for e in by_id.values():
        e["lun_votes"] = Counter()
        for pid in e["props"]:
            for lab, c in lun_by_prop.get(pid, {}).items():
                k = norm.key(lab, "district")
                for name in list(RIA_DISTRICTS) + list(LUN_ONLY):
                    if norm.key(name, "district") == k:
                        e["lun_votes"][rollup.get(name, name)] += c
    # Головна назва id: найчастіша (як показана); ключі назв id.
    for rid, e in by_id.items():
        e["main"] = e["names"].most_common(1)[0][0] if e["names"] else f"ria:{rid}"
        e["keys"] = {norm.key(n, "complex") for n in e["names"]}
        e["center"] = ((sum(p[0] for p in e["pts"]) / len(e["pts"]),
                        sum(p[1] for p in e["pts"]) / len(e["pts"])) if e["pts"] else None)
    # Id з кількома різними назвами (після нормалізації й ручних псевдонімів того ж id).
    by_name_ids = []
    for rid, e in by_id.items():
        allowed = {norm.key(a, "complex") for a in SAME_ID_ALIASES.get(rid, [])}
        if len(e["keys"] - allowed) > 1:
            by_name_ids.append(rid)
    by_name_ids.sort()
    # Сутності: id з однаковою головною назвою — злиття за точками.
    groups_by_key: dict[str, list[int]] = defaultdict(list)
    for rid, e in by_id.items():
        groups_by_key[norm.key(e["main"], "complex")].append(rid)
    entities = []          # dict(key, name, ids, kind, within, group, aliases, comment, votes)
    ambiguous_names = []
    for nkey, ids in sorted(groups_by_key.items(), key=lambda kv: -sum(by_id[i]["n"] for i in kv[1])):
        ids = sorted(ids)
        clusters: list[list[int]] = []
        for rid in ids:
            c0 = by_id[rid]["center"]
            for cl in clusters:
                c1 = by_id[cl[0]]["center"]
                if c0 and c1 and dist_m(c0, c1) <= MERGE_M:
                    cl.append(rid)
                    break
            else:
                clusters.append([rid])
        split = len(clusters) > 1
        if split:
            ambiguous_names.append(by_id[ids[0]]["main"])
        for cl in clusters:
            main = max(cl, key=lambda r: by_id[r]["n"])
            e = by_id[main]
            raw_name = re.sub(r"^\s*ЖК\s+", "", e["main"]).strip()
            name = raw_name
            if split:
                street = None
                for r in cl:
                    loc = con.execute(
                        "SELECT location FROM listings WHERE json_extract(identity,'$.complex') = ? "
                        "AND location IS NOT NULL LIMIT 1", (f"ria:{r}",)).fetchone()
                    if loc:
                        street = re.sub(r"^вул\.\s*", "", loc[0].split(",")[0])
                        street = re.sub(r"\(.*$", "", street).strip()   # «(Радянської Армії)»
                        break
                name = f"{raw_name} ({street or 'ria ' + str(main)})"
            votes = Counter()
            lun_votes = Counter()
            names = Counter()
            n = 0
            for r in cl:
                votes.update(by_id[r]["votes"])
                lun_votes.update(by_id[r]["lun_votes"])
                names.update(by_id[r]["names"])
                n += by_id[r]["n"]
            entities.append({"ids": cl, "name": name, "main_raw": raw_name, "split": split,
                             "names": names, "votes": votes, "lun_votes": lun_votes, "n": n})
    # Прив'язка до району.
    for ent in entities:
        total = Counter()
        for d, c in ent["votes"].items():
            total[d] += c
        for d, c in ent["lun_votes"].items():
            total[d] += c
        n = sum(total.values())
        top, k = total.most_common(1)[0] if total else (None, 0)
        key_of = {**RIA_DISTRICTS, **LUN_ONLY}
        ok = n >= link_n and k / max(n, 1) >= link_share and top in key_of
        ent["district"] = key_of[top] if ok else ""
        ent["link_note"] = (
            (f"район: {top} {k}/{n} ({k / n:.0%})" if n else "район: голосів немає")
            + (f" — DOM.RIA {dict(ent['votes'].most_common(3))}" if ent["votes"] else "")
            + (f", LUN {dict(ent['lun_votes'].most_common(3))}" if ent["lun_votes"] else "")
            + ("" if ok else f" → не прив'язано (< {link_share:.0%} чи n < {link_n})"))
    # Ключі й види.
    used = set()
    for ent in entities:
        rid0 = ent["ids"][0]
        if any(r in UMBRELLAS for r in ent["ids"]):
            r = next(r for r in ent["ids"] if r in UMBRELLAS)
            ent["key"], ent["name"] = UMBRELLAS[r]
            ent["kind"] = "umbrella"
        else:
            ent["kind"] = "address" if re.match(r"^(вул\.|по вул|на )", ent["main_raw"]) else "named"
            base = slug(ent["name"])
            ent["key"] = base if base not in used else f"{base}-{rid0}"
        used.add(ent["key"])
        ent["within"] = next((WITHIN[r] for r in ent["ids"] if r in WITHIN), "")
        ent["group"] = next((GROUPS[r] for r in ent["ids"] if r in GROUPS), "")
    # Черги: «X 2», «X (5 черга)», «X-2», «X II» → група X, якщо X теж є.
    base_of = {}
    for ent in entities:
        k = norm.key(ent["main_raw"], "complex")
        b = re.sub(r"\s*(\d+\s*черга|\d+|ii|iii|iv)$", "", k).strip()
        base_of[ent["key"]] = b
    keys_by_name = {norm.key(e["main_raw"], "complex"): e for e in entities}
    for ent in entities:
        b = base_of[ent["key"]]
        # Адреси («вул. Галицька, 92» і «вул. Галицька») — різні будинки, а не черги.
        if ent["kind"] == "address" or keys_by_name.get(b, {}).get("kind") == "address":
            continue
        if b != norm.key(ent["main_raw"], "complex") and b in keys_by_name and not ent["group"]:
            head = keys_by_name[b]
            g = head["group"] or head["key"]
            head["group"] = g
            ent["group"] = g
    # Благо, ЖК лише з тексту, парасольки LUN.
    known = {norm.key(n, "complex"): e for e in entities for n in [e["name"], *e["names"]]
             if not e["split"]}
    blago_alias = defaultdict(list)
    new_entities = []
    for name, cnt in sorted(blago_names.items(), key=lambda kv: -kv[1]):
        k = norm.key(name, "complex")
        target = ALIASES.get(name)
        if target:
            k = norm.key(target, "complex")
        if k in known:
            if norm.key(name, "complex") != norm.key(known[k]["name"], "complex"):
                blago_alias[known[k]["key"]].append(name)
            continue
        if name not in EXTRA_COMPLEXES:
            EXTRA_COMPLEXES.append(name)
    for name in EXTRA_COMPLEXES:
        if norm.key(name, "complex") in known:
            continue
        ent = {"ids": [], "name": name, "main_raw": name, "split": False, "names": Counter(),
               "votes": Counter(), "lun_votes": Counter(), "n": 0, "district": "",
               "link_note": "лише Благо / LUN / текст (Етап 0) — без району", "kind": "named",
               "within": "", "group": "", "key": slug(name)}
        new_entities.append(ent)
        known[norm.key(name, "complex")] = ent
    for key, name in EXTRA_UMBRELLAS:
        ent = {"ids": [], "name": name, "main_raw": name, "split": False, "names": Counter(),
               "votes": Counter(), "lun_votes": Counter(), "n": 0, "district": "",
               "link_note": "парасолька з geoEntities LUN (проба Етапу 0)", "kind": "umbrella",
               "within": "", "group": "", "key": key}
        new_entities.append(ent)
    entities += new_entities
    # Псевдоніми: назви з полів (крім головної), ручні, Благо, той самий id.
    for ent in entities:
        al = set()
        for n_ in ent["names"]:
            if ent["split"]:
                continue
            al.add(n_)
        for r in ent["ids"]:
            al.update(SAME_ID_ALIASES.get(r, []))
        al.update(blago_alias.get(ent["key"], []))
        for a, t in ALIASES.items():
            if norm.key(t, "complex") in {norm.key(ent["name"], "complex"),
                                          norm.key(ent["main_raw"], "complex")} and not ent["split"]:
                al.add(a)
        # Назви з полів, що належать id з кількома назвами, — лише головна назва id
        # (решта визначається за назвою як окремі сутності чи нерозпізнані).
        if any(r in by_name_ids for r in ent["ids"]):
            al = {a for a in al if norm.key(a, "complex") in
                  {norm.key(ent["main_raw"], "complex")}
                  | {norm.key(x, "complex") for r in ent["ids"] for x in SAME_ID_ALIASES.get(r, [])}
                  | {norm.key(a2, "complex") for a2, t in ALIASES.items()
                     if norm.key(t, "complex") == norm.key(ent["main_raw"], "complex")}}
        # Лише ті, що дають НОВИЙ ключ (решта збігається з назвою після нормалізації).
        seen = {norm.key(ent["name"], "complex")}
        out = []
        for a in sorted(al, key=lambda s: (s.casefold(), s)):
            k = norm.key(a, "complex")
            if k and k not in seen:
                seen.add(k)
                out.append(a)
        ent["aliases"] = out

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_districts(out / "districts.toml", districts)
    _write_complexes(out / "complexes.toml", entities, by_name_ids, ambiguous_names)
    report.update({"districts": len(districts), "complexes": len(entities),
                   "linked": sum(1 for e in entities if e["district"]),
                   "ria_id_by_name": by_name_ids, "ambiguous_names": ambiguous_names,
                   "parents": {k: list(v) for k, v in parents.items()}})
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "lun_label_vs_ria"},
                     ensure_ascii=False, indent=1))
    return 0


HEAD_D = """# Райони Івано-Франківська, села міської громади, відомі назви, що не є районом
# (Блок 4, крок E10, D57). Чернетку зроблено scripts/places_seed.py з копії бази Етапу 0
# (лише читання) і переглянуто очима; далі правиться тут — нове написання додається
# псевдонімом з рядком у implementation-notes.md (Deviations).
#
# key — стабільний латинський ключ (стоїть в адресах ?district=… і в базі); name — як
# показувати; area: city | hromada (село громади, рішення власника 4, D46) | outside
# (поза громадою — видно в «усе»); parent — батьківський район лише за доказом (≥80% при
# n≥5, rules.link; D57); verified = false — склад не звірено з КАТОТТГ; aliases — інші
# написання (латинську «i» нормалізація зводить сама, тут — те, що лише довідник знає);
# ria_ids / lun_ids — id району DOM.RIA і мікрорайону LUN (порожньо, доки не з'являться
# в place_raw: на копії Етапу 0 їх немає).
"""

HEAD_C = """# ЖК Івано-Франківська (Блок 4, крок E10, D57). Чернетка — scripts/places_seed.py з
# копії бази Етапу 0 (лише читання), переглянуто очима; далі правиться тут.
#
# key — стабільний ключ (?complex=…, listings.complex_key); name — як показувати (без
# «ЖК»); district — район лише за згоди ≥80% при n≥5 (голоси — у коментарі: поле району
# DOM.RIA рядків цього id і мітки LUN у квартирах із ним), "" — не прив'язано (видно на
# /status); kind: named | address («ЖК вул. …» DOM.RIA) | umbrella («житловий район»,
# поступається ЖК усередині); within — парасолька; group — черги одного ЖК (вид
# перевірки зведення complex_phase); ria_ids — id ЖК DOM.RIA (identity.complex);
# lun_ids — geoId residential_complex LUN; aliases — інші написання.
"""


def _write_districts(path: Path, districts) -> None:
    lines = [HEAD_D]
    for key, name, area, parent, verified, aliases, comment in districts:
        lines += ["", f"# {comment}", "[[district]]", f"key = {toml_str(key)}",
                  f"name = {toml_str(name)}", f"area = {toml_str(area)}",
                  f"parent = {toml_str(parent)}", f"verified = {'true' if verified else 'false'}",
                  f"aliases = {toml_list(aliases)}", "ria_ids = []", "lun_ids = []"]
    lines += ["", "# Відомі назви, що НЕ є районом: орієнтир, POI, вулиця, місто, ЖК у полі району."]
    for name, kind in IGNORE:
        lines += ["", "[[ignore]]", f"name = {toml_str(name)}", f"kind = {toml_str(kind)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_complexes(path: Path, entities, by_name_ids, ambiguous) -> None:
    lines = [HEAD_C, "",
             "# id ЖК DOM.RIA з кількома різними назвами — ЖК визначається за назвою (Етап 0).",
             f"ria_id_by_name = {toml_list(by_name_ids)}"]
    if ambiguous:
        lines.append("# Однакова назва — різні ЖК (різні точки): назва без уточнення ні до кого не "
                     "веде: " + ", ".join(ambiguous))
    for e in entities:
        names = ", ".join(f"{n} {c}" for n, c in e["names"].most_common(4))
        lines += ["", f"# {e['n']} оголошень DOM.RIA" + (f" ({names})" if names else "")
                  + f"; {e['link_note']}",
                  "[[complex]]", f"key = {toml_str(e['key'])}", f"name = {toml_str(e['name'])}",
                  f"district = {toml_str(e['district'])}", f"kind = {toml_str(e['kind'])}",
                  f"within = {toml_str(e['within'])}", f"group = {toml_str(e['group'])}",
                  f"ria_ids = {toml_list(sorted(e['ids']))}", "lun_ids = []",
                  f"aliases = {toml_list(e['aliases'])}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
