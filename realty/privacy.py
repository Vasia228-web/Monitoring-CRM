"""Телефони в описах і назвах → «[телефон]» (рішення власника 5, D46; крок E6, D51).

Розпізнавання — не одна регулярка (план Блоку 3; усі списки й вирази — у
`config/privacy.toml`):

  1. посилання-контакти (viber://…, tel:, wa.me/…, t.me/+…, з %2B замість «+») —
     цілком, але лише один номер: ціна чи кількість кімнат одразу після
     посилання лишаються;
  2. ряди груп цифр і символів маски з роздільниками (пробіли, зокрема тонкі й
     нерозривні, крапки, дефіси й тире всіх видів, «_», дужки — не довше
     `max_sep_run`). Усередині ряду шукається підпослідовність груп, що є
     номером: так номер одразу після ціни («45 000 067 000 00 01») ріжеться, а
     ціна лишається.
     * Цифри: 12 із «380», 13 із «3800» (зайвий 0: «+380 067 …»), 11 із «80» або
       10 із «0» → національний номер 0XXXXXXXXX; друга цифра — не 0; код
       оператора (`mobile_codes`) або району (03x–06x); форма груп — з білого
       списку `national_shapes` (або одна група з 10 цифр; або +380 — за будь-якої
       форми, але лише коли номер — увесь ряд: інакше «4149 3805 1234 5678»
       різалась би посередині). Форма відсіює ціни («(80) 3 600 000.00 грн» →
       1-1-3-3-2) і дати з часом. Форма з десяти одиночних цифр — лише з тим
       самим роздільником усюди, пробіли не рахуються («0.6.7.0…», «0. 6.7.0…»;
       не «0.5 0.6 0.7 0.8 0.9»). «+» між групами
       («+380XX-XXX+XX-XX») — лише всередині номера +380. Цифри інших письмен
       (повноширинні «０６７…», математичні) читаються як звичайні.
     * Маска («0X******XX», «067-***-**-**», «+380 67 *** ** **»): 0/380 на
       початку, національна частина — від 10 до `max_mask_national_positions`
       позицій, масок ≥ `min_mask_chars` (для +380 рівно з 12 позиціями —
       ≥ `min_mask_chars_full_international`). Форма груп — та сама перевірка, що
       й для цифр (одна група, білий список, +380 цілим рядом): «05.10.2024 ****»
       — дата, а не маска. Замасковані номери вже не повні, але частина цифр
       лишається видимою — ріжемо й їх.
     Ряд не закінчується перед «:цифра» і не починається після «цифра:» — це
     час чи кадастровий номер («0510100000:01:002:0123»), не телефон.
  3. короткий міський номер «75-00-01» — лише одразу після «тел/моб/дзвоніть»
     (інакше це дата).

Свідомо НЕ ріжемо (краще пропустити екзотичний номер, ніж зіпсувати ціну чи
дату): номер, написаний словами; 9 цифр без 0 («67 000 00 01»); номер,
склеєний із латинськими літерами («…01abc», «Viber0670000001»); групи через
«/» чи перенесення рядка; літера «О»/«O» замість нуля; цифри-емодзі
(«0️⃣6️⃣7️⃣…»); форми груп поза білим списком (3-4-3, 2-2-2-2-2, 1-3-3-3 —
`cli.py privacy scan` показує їх серед «майже номерів»); маска довша за
`max_mask_national_positions` (лишається цілою, не різаною); гарячі лінії 0 800;
номери інших країн. Імена не чіпаємо (рішення 5 — лише телефони).

Заміна ідемпотентна: у «[телефон]» немає цифр. `pattern_of` дає форму
знайденого фрагмента без цифр номера (лише незмінний префікс 380/80/0) — для
перегляду замін у `cli.py privacy scan`; самі номери не друкуються й не
пишуться ніде. Той самий звіт показує «майже номери»: ряди з 10–13 цифр, що
схожі на номер, але відкинуті (код, форма) або розділені чимось, крім
роздільників (`scan.loose_gap_max`: «/», «+», «⏎», «;» …) — щоб нові форми
можна було знайти, а не вгадати.

Де діє: слухачі ORM на Listing.description і Listing.title, а також запаска
для DML через сесію (`realty/models.py`) — кожен запис через ORM із будь-якого
джерела й будь-якого шляху.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass

from . import configfiles

TOPIC = "privacy"


@dataclass(frozen=True)
class Hit:
    """Один замінений фрагмент: вид, форма національних груп, форма без цифр."""

    kind: str          # link:<назва> | number | mask | short
    shape: str         # «3-3-2-2» для номерів, «» для решти
    pattern: str       # напр. «+38 (0XX) XXX-XX-XX» — без цифр номера


@dataclass(frozen=True)
class NearMiss:
    """Ряд, схожий на номер, але відкинутий (для звіту промахів, без цифр).

    `reason`: code — код оператора/району не чинний; shape — форма груп не з
    білого списку (`shape` — довжини груп); loose — групи розділені не
    роздільником (`shape` — форма фрагмента без цифр, як `pattern_of`).
    """

    shape: str
    reason: str


@dataclass(frozen=True)
class _Compiled:
    cfg: object
    run: re.Pattern
    token: re.Pattern
    short: list
    keyword: re.Pattern
    links: list
    loose: re.Pattern
    mobile: frozenset
    area: frozenset
    shapes: frozenset
    masks: frozenset
    seps: frozenset


_cache: dict[str, _Compiled] = {}
_explicit: dict[int, tuple[object, _Compiled]] = {}
_lock = threading.Lock()


def _class(chars: str) -> str:
    return "".join(re.escape(ch) for ch in chars)


def _compile(cfg) -> _Compiled:
    p = cfg.phone
    mask = _class(p.mask_chars)
    sep = f"[{_class(p.separators)}]{{1,{p.max_sep_run}}}"
    if p.plus_inside_international:
        # «+» між цифрами — роздільник ряду; номер із ним приймає лише _judge (+380).
        sep = rf"(?:{sep}|(?<=\d)\+(?=\d))"
    edge = f"0-9A-Za-z_{mask}"
    # Шматки [цифри/маска] і роздільники — неперетинні класи символів, тож вираз
    # не має експоненційного перебору навіть на довгих рядах цифр (IBAN, картки).
    # Цифри й маску всередині шматка («06******01») ділить на групи `token`.
    # «%2B» — закодований «+» (посилання, вставлені як текст).
    run = re.compile(rf"(?<![{edge}])(?<!\d:)(?P<pre>\+\s?|%2[Bb])?"
                     rf"(?P<body>\(?[\d{mask}]+(?:{sep}[\d{mask}]+)*\)?)"
                     rf"(?![{edge}])(?!:\d)")
    short = []
    for shape in p.short_local_shapes:
        parts = [rf"\d{{{n}}}" for n in shape.split("-")]
        short.append(re.compile(r"(?<![\d.\-])" + r"[\-. ]".join(parts) + r"(?![\-. ]?\d)"))
    return _Compiled(
        cfg=cfg, run=run, token=re.compile(rf"\d+|[{mask}]+"), short=short,
        keyword=re.compile(p.short_local_keyword_regex),
        links=[(name, re.compile(rx)) for name, rx in p.link_regex.items()],
        loose=re.compile(rf"\d+(?:[\W_]{{1,{cfg.scan.loose_gap_max}}}\d+)*"),
        mobile=frozenset(p.mobile_codes), area=frozenset(p.area_first_digits),
        shapes=frozenset(p.national_shapes), masks=frozenset(p.mask_chars),
        seps=frozenset(p.separators))


def _compiled(cfg=None) -> _Compiled:
    if cfg is not None:
        # Явно переданий конфіг (тести, перевірка правки): компілюємо раз на об'єкт.
        got = _explicit.get(id(cfg))
        if got is None or got[0] is not cfg:
            with _lock:
                if len(_explicit) > 16:
                    _explicit.clear()
                got = _explicit[id(cfg)] = (cfg, _compile(cfg))
        return got[1]
    cfg, digest = configfiles.get_with_hash(TOPIC)
    got = _cache.get(digest)
    if got is None:
        with _lock:
            got = _cache.get(digest)
            if got is None:
                got = _compile(cfg)
                _cache.clear()
                _cache[digest] = got
    return got


def config():
    """Чинний конфіг `config/privacy.toml`."""
    return _compiled().cfg


# --- Форми без цифр -------------------------------------------------------------------------


def _ascii(s: str) -> str:
    """Цифри будь-якого письма («０», «𝟎», «٠») → 0–9; решта як є (довжина та сама)."""
    if s.isascii():
        return s
    return "".join(str(unicodedata.digit(ch)) if ch.isdecimal() else ch for ch in s)


def _keep_prefix(digits: str) -> int:
    """Скільки перших цифр — незмінний префікс (380 / 80 / 0), а не сам номер."""
    if digits.startswith("380"):
        return 3
    if digits.startswith("80"):
        return 2
    if digits.startswith("0"):
        return 1
    return 0


def pattern_of(fragment: str, masks: str = "*xхXХ•") -> str:
    """«+38 (067) 000-00-01» → «+38 (0XX) XXX-XX-XX»: цифри номера → X, маска → *.

    Лишається лише префікс 380/80/0 — він однаковий для всіх номерів країни.
    """
    digits = _ascii("".join(ch for ch in fragment if ch.isdecimal()))
    keep = _keep_prefix(digits)
    out, seen = [], 0
    for ch in fragment:
        if ch.isdecimal():
            out.append(digits[seen] if seen < keep else "X")
            seen += 1
        elif ch.isdigit():                           # «²», «①» — не цифри номера
            out.append("X")
        elif ch in masks:
            out.append("*")
        else:
            out.append(ch)
    return "".join(out)


# --- Розпізнавання --------------------------------------------------------------------------


def _national(positions: str) -> tuple[int, str] | None:
    """(скільки позицій відкинути — код країни, національний номер) або None."""
    n = len(positions)
    if n == 13 and positions.startswith("3800"):     # «+380 067 …» — зайвий 0
        return 3, positions[3:]
    if n == 12 and positions.startswith("380"):
        return 2, positions[2:]
    if n == 11 and positions.startswith("80"):
        return 1, positions[1:]
    if n == 10 and positions.startswith("0"):
        return 0, positions
    return None


def _national_masked(positions: str, max_nat: int) -> tuple[int, str] | None:
    """Те саме для замаскованого номера: довжина маски часто не збігається з
    числом цифр («06********01» — 12 позицій, OLX буває й 13–14), тож
    національна частина — від 10 до `max_nat` позицій. Маска (≥ min_mask_chars
    символів) у ціні чи даті не трапляється."""
    if positions.startswith("3800"):
        drop = 3
    elif positions.startswith("380"):
        drop = 2
    elif positions.startswith("80"):
        drop = 1
    elif positions.startswith("0"):
        drop = 0
    else:
        return None
    nat = positions[drop:]
    return (drop, nat) if 10 <= len(nat) <= max_nat else None


def _national_groups(groups: list[str], drop: int) -> list[str]:
    out = []
    for g in groups:
        if drop >= len(g):
            drop -= len(g)
            continue
        out.append(g[drop:])
        drop = 0
    return out


def _code_ok(nat: str, c: _Compiled) -> bool:
    second = nat[1]
    if second == "0" or not second.isdigit():
        return second in c.masks
    third = nat[2]
    if third.isdigit():
        return nat[1:3] in c.mobile or second in c.area
    return second in c.area or any(code.startswith(second) for code in c.mobile)


def _judge(groups: list[str], c: _Compiled, *, sgroups: list[str], gaps: list[str],
           whole: bool) -> tuple[str, str, str | None]:
    """('number'|'mask'|'', національна форма, причина відмови).

    `groups` — шматки (цифри й маска окремо), `sgroups` — групи між
    роздільниками (цифри й маска впритул — одна група), `gaps` — роздільники між
    шматками, `whole` — вікно є всім рядом.
    """
    p = c.cfg.phone
    positions = _ascii("".join(groups))
    nmask = sum(ch in c.masks for ch in positions)
    nat = (_national_masked(positions, p.max_mask_national_positions) if nmask
           else _national(positions))
    if nat is None:
        return "", "", None
    drop, number = nat
    intl = drop >= 2                                 # +380 / 380 / +380 0…
    shape = "-".join(str(len(g)) for g in _national_groups(sgroups, drop))
    any_shape = intl and whole and p.international_any_shape
    if not intl and any("+" in g for g in gaps):
        return "", shape, None                       # «+» усередині — лише в +380…
    if nmask:
        need = p.min_mask_chars
        if drop == 2 and len(positions) == 12:
            need = min(need, p.min_mask_chars_full_international)
        if nmask < need or not _code_ok(number, c):
            return "", shape, None
        if "-" in shape and shape not in c.shapes and not any_shape:
            return "", shape, None                   # «05.10.2024 ****» — дата
        return "mask", shape, None
    if not _code_ok(number, c):
        return "", shape, "code"
    if "-" not in shape:                             # одна група з 10 цифр
        return "number", shape, None
    if set(shape.split("-")) == {"1"} and len({g.strip() for g in gaps}) > 1:
        return "", shape, "shape"                    # «0.5 0.6 0.7 0.8 0.9»
    if any_shape:                                    # +380 цілим рядом — будь-яка форма
        return "number", shape, None
    if shape in c.shapes:
        return "number", shape, None
    return "", shape, "shape"


def _scan_run(text: str, m: re.Match, c: _Compiled, misses: list | None):
    """[(початок, кінець, вид, форма)] у межах одного ряду."""
    base = m.start("body")
    toks = [(t.start() + base, t.end() + base, t.group())
            for t in c.token.finditer(m.group("body"))]
    # Цифри й маска впритул («06********01») — один шматок: маскований номер
    # беремо лише цілими шматками, інакше лишився б хвіст «[телефон]01».
    glued = [k > 0 and toks[k][0] == toks[k - 1][1] for k in range(len(toks))]
    gap = [""] + [text[toks[k - 1][1]:toks[k][0]] for k in range(1, len(toks))]
    limit = 3 + c.cfg.phone.max_mask_national_positions   # «3800» + маска
    found = []
    near: NearMiss | None = None
    i = 0
    while i < len(toks):
        hit = None
        for j in range(min(len(toks), i + limit), i, -1):
            groups = [t[2] for t in toks[i:j]]
            if sum(len(g) for g in groups) > limit:
                continue
            masked = any(ch in c.masks for g in groups for ch in g)
            if masked and (glued[i] or (j < len(toks) and glued[j])):
                continue
            sgroups: list[str] = []
            for k in range(i, j):
                if k > i and glued[k]:
                    sgroups[-1] += toks[k][2]
                else:
                    sgroups.append(toks[k][2])
            kind, shape, reason = _judge(groups, c, sgroups=sgroups, gaps=gap[i + 1:j],
                                         whole=(i == 0 and j == len(toks)))
            if kind:
                hit = (j, kind, shape)
                break
            if reason and near is None:
                near = NearMiss(shape, reason)
        if hit is None:
            i += 1
            continue
        j, kind, shape = hit
        start = m.start() if i == 0 else toks[i][0]
        end = toks[j - 1][1]
        frag = text[start:end]
        # Дужки навколо коду оператора: «(067) …» без лишньої «(» перед заміною;
        # дужка, що відкриває ширший вираз («(067 000 00 01 агент)»), лишається.
        if frag.count(")") > frag.count("(") and start > 0 and text[start - 1] == "(":
            start -= 1
        elif frag.count("(") > frag.count(")"):
            if end < len(text) and text[end] == ")":
                end += 1
            elif frag.startswith("("):
                start += 1
        found.append((start, end, kind, shape))
        i = j
    # Один «майже номер» на ряд і лише якщо в ряду номера не знайшлось — для
    # звіту промахів (`cli.py privacy scan`), без цифр.
    if near is not None and not found and misses is not None:
        misses.append(near)
    return found


def _loose(text: str, c: _Compiled) -> list[NearMiss]:
    """Групи цифр, розділені НЕ роздільником («067/000/00/01», «0_67…» до правки
    конфігу, «⏎»), що разом дають номер (0/80/380, чинний код), — лише для звіту.

    Береться текст ПІСЛЯ заміни: те, що вже замінено, сюди не потрапляє; ряди, які
    суворий розбір і так бачить цілими (усі проміжки — роздільники), — теж ні
    (їх відмову показує `_scan_run` як code/shape). «+» між групами поза +380
    суворий розбір відкидає мовчки — такі ряди тут.
    """
    p = c.cfg.phone
    out = []
    for m in c.loose.finditer(text):
        run = m.group()
        groups = [(g.start(), g.end()) for g in re.finditer(r"\d+", run)]
        if len(groups) < 2:
            continue
        i = 0
        while i < len(groups):
            hit = None
            # Вікна з ≥2 груп і ≤13 цифр (довший номер — «3800…»), від найдовшого.
            ends, total = [], groups[i][1] - groups[i][0]
            for j in range(i + 1, len(groups)):
                total += groups[j][1] - groups[j][0]
                if total > 13:
                    break
                ends.append(j + 1)
            for j in reversed(ends):
                digits = _ascii("".join(run[a:b] for a, b in groups[i:j]))
                nat = _national(digits)
                if nat is None or not _code_ok(nat[1], c):
                    continue
                gaps = [run[groups[k - 1][1]:groups[k][0]] for k in range(i + 1, j)]
                # «+» суворий розбір приймає лише всередині +380 — решту показуємо.
                strict = all(len(g) <= p.max_sep_run and set(g) <= c.seps for g in gaps)
                if not strict:
                    hit = j
                    frag = run[groups[i][0]:groups[j - 1][1]]
                    out.append(NearMiss(pattern_of(frag, p.mask_chars), "loose"))
                break
            i = hit if hit else i + 1
    return out


def _replace(text: str, spans, replacement: str) -> str:
    out, pos = [], 0
    for start, end in spans:
        out.append(text[pos:start])
        out.append(replacement)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def find(text: str | None, *, cfg=None, misses: list | None = None) -> tuple[str | None, list[Hit]]:
    """(текст із заміною, знайдені фрагменти) — незалежно від `phone.enabled`.

    `cfg` — інший конфіг (тести, перевірка правки до розгортання); `misses` —
    список, куди дописати відкинуті «майже номери» (форма й причина).
    """
    if not text:
        return text, []
    c = _compiled(cfg)
    repl = c.cfg.phone.replacement
    hits: list[Hit] = []

    # 1. Посилання-контакти — цілком.
    for name, rx in c.links:
        spans = []
        for m in rx.finditer(text):
            number = re.sub(r"(?i)%2B", "+", m.group())
            digits = re.sub(r"\D", "", number)
            hits.append(Hit(f"link:{name}", "", f"<{name}> " + pattern_of(digits)))
            spans.append((m.start(), m.end()))
        if spans:
            text = _replace(text, spans, repl)

    # 2. Ряди цифр і масок.
    spans = []
    for m in c.run.finditer(text):
        if not any(ch.isdigit() for ch in m.group()):
            continue
        for start, end, kind, shape in _scan_run(text, m, c, misses):
            frag = re.sub(r"(?i)%2B", "+", text[start:end])
            hits.append(Hit(kind, shape, pattern_of(frag, c.cfg.phone.mask_chars)))
            spans.append((start, end))
    if spans:
        text = _replace(text, spans, repl)

    # 3. Короткі міські — лише одразу після слова-ключа.
    for rx in c.short:
        spans = []
        for m in rx.finditer(text):
            before = text[max(0, m.start() - 30):m.start()]
            if c.keyword.search(before):
                hits.append(Hit("short", "", pattern_of(m.group())))
                spans.append((m.start(), m.end()))
        if spans:
            text = _replace(text, spans, repl)

    if misses is not None:
        misses.extend(_loose(text, c))
    return text, hits


def redact_phones(text: str | None, *, cfg=None) -> str | None:
    """Текст, у якому номери телефонів замінено на `phone.replacement`.

    Вимкнено в конфігу (`phone.enabled = false`) — текст як є.
    """
    if not text:
        return text
    c = _compiled(cfg)
    if not c.cfg.phone.enabled:
        return text
    return find(text, cfg=c.cfg)[0]


def redact_field(field: str, value):
    """Для слухачів ORM: значення поля Listing після заміни (лише поля з `phone.fields`)."""
    if not isinstance(value, str) or not value:
        return value
    c = _compiled()
    if not c.cfg.phone.enabled or field not in c.cfg.phone.fields:
        return value
    return find(value, cfg=c.cfg)[0]


def fields() -> tuple[str, ...]:
    return _compiled().cfg.phone.fields
