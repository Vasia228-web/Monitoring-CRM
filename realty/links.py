"""Посилання на оголошення: розбір, ключ «сайт:id», канонічна адреса (крок E6, D51).

Один модуль для всіх блоків (інтеграція, конфлікт «один ключ сайт:id», D47):

  * `parse(text)` — посилання (чи вставлений текст «Поділитися», чи голий id, чи
    явне «сімейство:id» — «olx:10BkYC», «ria:34616500», як у ?hl=<ключ>) →
    `Link` (сімейство, id, ключ, канонічна адреса, адреса перевірки) або
    `NotALink` із кодом причини — і ніколи не виняток, навіть на зламаній адресі
    («https://[olx.ua/…», повноширинні символи з месенджера). Чиста функція: без
    мережі, без бази, без журналу вхідного тексту (у ньому буває піддомен
    агенції з номером телефону чи секретний токен чату — D51);
  * `site_key(url)` — ключ для `listings.site_key` зі збереженої адреси:
    domria:…, olx:… (з урахуванням регістру), rieltor:…, lun:…, flombu:…,
    blago:…; None — якщо адреса не є оголошенням підтримуваного сайту;
  * `canonical_url`, `canonical_fetch_url` — одна адреса на оголошення: без m.
    і www (крім хостів, де канонічна форма з www), без query й #фрагмента,
    DOM.RIA /ru/ → /uk/, flombu /uk/prodazh/kvartyra-<id> → /uk/estate_deal_sales/<id>,
    rieltor без www (www дає 404 живим оголошенням);
  * `olx_token_to_numeric` / `numeric_to_olx_token` — id OLX: base62 з
    переставленими v/w (191 зі 191 живої сторінки, D51).

Усі вирази й списки — у `config/links.toml` (порт прототипу Етапу 0, 154 кейси).
Ключ у базі підтримують слухачі ORM (`realty/models.py`), разове заповнення —
`cli.py links reindex`, звірка — `cli.py links selftest`.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import SplitResult, parse_qsl, quote, unquote, urlsplit

from . import configfiles

TOPIC = "links"
# Як читається розбором (`via`) голий id — щоб `site_key` ніколи не брав його за адресу.
BARE_VIAS = frozenset({"bare_number", "bare_property", "bare_olx_token", "olx_numeric",
                       "family_key"})


@dataclass(frozen=True)
class Link:
    """Розпізнане посилання (або голий id).

    `key` — «сімейство:id»; для голого числа (просторів кілька) — None, а
    варіанти — у `candidates`. `parts` — інші частини адреси (slug, loc, cat,
    kind): для шаблонів адрес, не для журналів.
    """

    family: str
    id: str
    via: str
    key: str | None
    canonical_url: str | None
    fetch_url: str | None
    candidates: tuple[str, ...] = ()
    case_lost: bool = False
    parts: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class NotALink:
    """Не посилання на оголошення. `reason` — код причини:

    empty, no_url, unrecognized, unsupported_host, short_link, chat_link,
    not_listing (пошук, ЖК, каталог), not_flat (інша категорія чи вид),
    own_not_property (наш сайт, але не квартира).
    """

    reason: str
    family: str | None = None

    @property
    def ok(self) -> bool:
        return False


# --- Конфіг і скомпільовані вирази ---------------------------------------------------------


@dataclass(frozen=True)
class _Compiled:
    cfg: object
    scheme: re.Pattern
    app_link: re.Pattern
    android_split: re.Pattern
    first_url: re.Pattern
    case_lost: re.Pattern
    bare_property: re.Pattern
    bare_olx_numeric: re.Pattern
    bare_numeric: re.Pattern
    bare_olx_token: re.Pattern
    family_key: re.Pattern
    id_rx: dict
    query_rx: dict
    non_listing_rx: dict
    alphabet: str
    index: dict


_cache: dict[str, _Compiled] = {}
_cache_lock = threading.Lock()


def _compiled() -> _Compiled:
    """Конфіг (перечитується за mtime не частіше ніж раз на 60 с) і вирази,
    скомпільовані раз на версію конфігу."""
    cfg, digest = configfiles.get_with_hash(TOPIC)
    got = _cache.get(digest)
    if got is not None:
        return got
    with _cache_lock:
        got = _cache.get(digest)
        if got is None:
            fams = cfg.families
            got = _Compiled(
                cfg=cfg,
                scheme=re.compile(cfg.unwrap.scheme),
                app_link=re.compile(cfg.unwrap.app_link),
                android_split=re.compile(cfg.unwrap.android_app_split),
                first_url=re.compile(cfg.unwrap.first_url_in_text),
                case_lost=re.compile(cfg.olx.case_lost_regex),
                bare_property=re.compile(cfg.bare.property_prefixed),
                bare_olx_numeric=re.compile(cfg.bare.olx_numeric_prefixed),
                bare_numeric=re.compile(cfg.bare.numeric),
                bare_olx_token=re.compile(cfg.bare.olx_token),
                family_key=re.compile(cfg.bare.family_key),
                id_rx={f: [(n, re.compile(rx)) for n, rx in s.id_regex.items()]
                       for f, s in fams.items()},
                query_rx={f: [(n, re.compile(rx)) for n, rx in s.query_id.items()]
                          for f, s in fams.items()},
                non_listing_rx={f: [(n, re.compile(rx)) for n, rx in s.non_listing_regex.items()]
                                for f, s in fams.items()},
                alphabet=cfg.olx.alphabet,
                index={ch: i for i, ch in enumerate(cfg.olx.alphabet)},
            )
            _cache.clear()
            _cache[digest] = got
    return got


def config():
    """Чинний конфіг `config/links.toml`."""
    return _compiled().cfg


# --- id OLX: base62 з переставленими v/w ----------------------------------------------------


def numeric_to_olx_token(n: int) -> str:
    """931767753 → '113Bm9' (числовий id з підвалу сторінки → токен з адреси)."""
    c = _compiled()
    if n < 0:
        raise ValueError("id OLX — невід'ємне число")
    out = ""
    while n:
        n, r = divmod(n, 62)
        out = c.alphabet[r] + out
    return out or c.alphabet[0]


def olx_token_to_numeric(token: str) -> int:
    """'113Bm9' → 931767753. Регістр важливий; невідомий символ — ValueError."""
    c = _compiled()
    n = 0
    for ch in token:
        try:
            n = n * 62 + c.index[ch]
        except KeyError:
            raise ValueError(f"символ {ch!r} не з алфавіту id OLX") from None
    return n


# --- Розбір ---------------------------------------------------------------------------------


def _norm_host(netloc: str, prefixes) -> str:
    host = netloc.lower().rsplit("@", 1)[-1]
    if host.startswith("["):                         # [::1]:8000
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    host = host.strip(".")
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if host.startswith(p) and len(host) > len(p):
                host = host[len(p):]
                changed = True
    return host


def _split(url: str) -> SplitResult | None:
    """urlsplit без винятків: «https://[olx.ua/…» (недописаний IPv6) чи хост із
    символами, що змінюються за NFKC («olx.ua＃» з месенджера), — None."""
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _public_domain() -> str | None:
    raw = os.environ.get("PUBLIC_DOMAIN", "").strip().lower()
    raw = raw.removeprefix("https://").removeprefix("http://").strip("/")
    return raw.removeprefix("www.") or None


def _host_family(host: str, cfg) -> tuple[str | None, str | None]:
    """(сімейство, код відмови)."""
    h = cfg.hosts
    if host in h.reject:
        return None, h.reject[host]
    if host in h.short_links:
        return None, "short_link"
    if host in h.family:
        return h.family[host], None
    for domain, fam in h.subdomain_family.items():
        if host.endswith("." + domain):
            return fam, None
    if any(host.endswith(s) for s in h.own_suffixes):
        return "own", None
    pub = _public_domain()
    if pub and host == pub:
        return "own", None
    return None, "unsupported_host"


def _unwrap(url: str, c: _Compiled, depth: int = 0) -> str:
    """Посилання застосунків і обгортки переадресацій → внутрішня адреса."""
    cfg = c.cfg
    if depth > cfg.unwrap.max_depth:
        return url
    if m := c.app_link.match(url):
        return _unwrap(m.group("inner"), c, depth + 1)
    if m := c.android_split.match(url):
        return _unwrap(f"{m.group('scheme')}://{m.group('rest')}", c, depth + 1)
    p = _split(url)
    if p is None:
        return url
    host = _norm_host(p.netloc, cfg.hosts.strip_prefixes)
    fam, _ = _host_family(host, cfg)
    if fam is None and p.query:
        for _, value in parse_qsl(p.query, keep_blank_values=True):
            cand = value
            for _ in range(cfg.unwrap.max_decode + 1):
                if c.scheme.match(cand):
                    inner = _unwrap(cand, c, depth + 1)
                    inner_p = _split(inner)
                    if inner_p is not None and _host_family(
                            _norm_host(inner_p.netloc, cfg.hosts.strip_prefixes), cfg)[0]:
                        return inner
                    break
                decoded = unquote(cand)
                if decoded == cand:
                    break
                cand = decoded
    return url


def _fill(templates, parts: dict) -> str | None:
    """Перший шаблон, для якого відомі всі частини."""
    for tpl in templates:
        try:
            return tpl.format(**parts)
        except (KeyError, IndexError):
            continue
    return None


def _make_link(fam: str, ident: str, via: str, groups: dict, c: _Compiled,
               *, case_lost: bool = False, notes=()) -> Link:
    spec = c.cfg.families[fam]
    parts = {k: v for k, v in groups.items() if v is not None and k != "id"}
    # Частини шляху — після %-декодування: у шаблон адреси — знову закодовані,
    # інакше «a%3Fb-ID…» дав би адресу перевірки з «?» посеред шляху (хибне 404).
    fill = {**{k: quote(v, safe="-._~") for k, v in parts.items()}, "id": ident}
    key = None if case_lost else f"{fam}:{ident}"
    return Link(family=fam, id=ident, via=via, key=key,
                canonical_url=None if case_lost else _fill(spec.canonical_url, fill),
                fetch_url=None if case_lost else _fill(spec.fetch_url, fill),
                candidates=(key,) if key else (), case_lost=case_lost,
                parts=MappingProxyType(parts), notes=tuple(notes))


def _parse_family_key(t: str, c: _Compiled) -> Link | NotALink | None:
    """«olx:10BkYC», «domria:34616500», «ria:…» (псевдонім) — явний ключ; None — не
    ця форма (невідоме сімейство чи id не того вигляду: «id:931767753» — підвал OLX)."""
    m = c.family_key.match(t)
    if not m:
        return None
    cfg = c.cfg
    fam = cfg.bare.family_alias.get(m.group("fam"), m.group("fam"))
    ident = m.group("id")
    if fam not in cfg.families:
        return None
    if fam == "olx":
        if not c.bare_olx_token.match(ident):
            return None
    elif not ident.isdigit():
        return None
    if fam == "own":
        ident = str(int(ident))
    key = f"{fam}:{ident}"
    return Link(family=fam, id=ident, via="family_key", key=key,
                canonical_url=_fill(cfg.families[fam].canonical_url, {"id": ident}),
                fetch_url=None, candidates=(key,))


def _parse_bare(t: str, c: _Compiled) -> Link | NotALink:
    cfg = c.cfg
    if (fk := _parse_family_key(t, c)) is not None:
        return fk
    if m := c.bare_property.match(t):
        num = str(int(m.group("num")))
        return Link(family="own", id=num, via="bare_property", key=f"own:{num}",
                    canonical_url=_fill(cfg.families["own"].canonical_url, {"id": num}),
                    fetch_url=None, candidates=(f"own:{num}",))
    if m := c.bare_olx_numeric.match(t):
        token = numeric_to_olx_token(int(m.group("num")))
        return Link(family="olx", id=token, via="olx_numeric", key=f"olx:{token}",
                    canonical_url=None, fetch_url=None, candidates=(f"olx:{token}",),
                    notes=("числовий id OLX переведено в токен",))
    if m := c.bare_numeric.match(t):
        num = m.group("num")
        cands = []
        for fam in cfg.bare.number_families:
            cands.append(f"{fam}:{int(num) if fam == 'own' else num}")
        if len(num) == cfg.olx.numeric_digits:
            cands.append(f"olx:{numeric_to_olx_token(int(num))}")
        return Link(family="number", id=num, via="bare_number", key=None,
                    canonical_url=None, fetch_url=None,
                    candidates=tuple(dict.fromkeys(cands)))
    if c.bare_olx_token.match(t) and re.search(r"[A-Za-z]", t):
        prefix = cfg.bare.olx_id_prefix
        ids = [t[len(prefix):], t] if t.startswith(prefix) and len(t) > len(prefix) + 2 else [t]
        return Link(family="olx", id=ids[0], via="bare_olx_token", key=f"olx:{ids[0]}",
                    canonical_url=None, fetch_url=None,
                    candidates=tuple(f"olx:{x}" for x in ids))
    return NotALink("unrecognized")


def parse(text: str | None) -> Link | NotALink:
    """Посилання чи вставлений текст → `Link` або `NotALink` (див. докстрінг модуля).

    Ніколи не кидає ValueError: розбір адреси, якого не вдалося виконати, —
    NotALink('unrecognized'). Зламаний конфіг — ConfigError (окремо).
    """
    try:
        return _parse(text)
    except ValueError:
        return NotALink("unrecognized")


def _first_url(t: str, c: _Compiled) -> re.Match | None:
    """Перший фрагмент, схожий на адресу; наш домен (PUBLIC_DOMAIN) — і без схеми."""
    m = c.first_url.search(t)
    pub = _public_domain()
    if pub:
        own = re.search(rf"(?i)(?<![\w.-])(?:www\.)?{re.escape(pub)}/\S+", t)
        if own and (m is None or own.start() < m.start()):
            return own
    return m


def _parse(text: str | None) -> Link | NotALink:
    c = _compiled()
    cfg = c.cfg
    t = (text or "").strip().strip("'\"<>()[]").strip()
    if not t:
        return NotALink("empty")
    if "/" not in t:
        # Голий id («34616500», «ID: 931767753», «№123», «кв. 123»); з крапкою, але
        # не id — може бути адресою без шляху, її розбирає гілка нижче.
        bare = _parse_bare(t, c)
        if isinstance(bare, Link):
            return bare
        if "." not in t:
            return NotALink("no_url") if re.search(r"\s", t) else bare
    m = _first_url(t, c)
    if not m:
        return NotALink("no_url")
    url = m.group(0).rstrip(cfg.unwrap.trailing_punct)
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    url = _unwrap(url, c)
    p = _split(url)
    if p is None:
        return NotALink("unrecognized")
    host = _norm_host(p.netloc, cfg.hosts.strip_prefixes)
    fam, why = _host_family(host, cfg)
    if fam is None:
        return NotALink(why or "unsupported_host")
    path = unquote(p.path) or "/"
    spec = cfg.families[fam]
    for name, rx in c.id_rx[fam]:
        mm = rx.match(path)
        if not mm:
            continue
        groups = mm.groupdict()
        for group, allowed in spec.require.items():
            if groups.get(group) is not None and groups[group] not in allowed:
                return NotALink("not_flat", fam)
        return _make_link(fam, groups["id"], name, groups, c)
    if c.query_rx[fam] and p.query:
        params = dict(parse_qsl(p.query, keep_blank_values=True))
        for name, rx in c.query_rx[fam]:
            value = params.get(name)
            if value is not None and rx.match(value):
                return _make_link(fam, value, f"query:{name}", {}, c)
    if fam == "olx" and (mm := c.case_lost.match(path)):
        ident = mm.group("id")
        if ident.isdigit():
            # Регістр губиться лише в літерах: id із самих цифр однозначний.
            return _make_link(fam, ident, "case_lost_digits", {}, c)
        return _make_link(fam, ident, "case_lost", {}, c, case_lost=True,
                          notes=("адресу переведено в нижній регістр: регістр id OLX втрачено",))
    if fam == "own":
        return NotALink("own_not_property", fam)
    for _name, rx in c.non_listing_rx[fam]:
        if rx.match(path):
            return NotALink("not_listing", fam)
    return NotALink("not_listing", fam)


def site_key(url: str | None) -> str | None:
    """Ключ `listings.site_key` зі збереженої адреси оголошення, або None.

    Лише адреса (не голий id) підтримуваного сайту, що є оголошенням, з
    однозначним id (адреса OLX у нижньому регістрі — None: регістр id втрачено).
    """
    if not url:
        return None
    link = parse(url)
    if not isinstance(link, Link) or link.via in BARE_VIAS or link.key is None:
        return None
    if link.family not in _compiled().cfg.listing_families:
        return None
    return link.key


def canonical_url(url: str | None) -> str | None:
    """Канонічна адреса оголошення (одна на оголошення) або None."""
    link = parse(url) if url else None
    return link.canonical_url if isinstance(link, Link) and link.via not in BARE_VIAS else None


def canonical_fetch_url(url: str | None) -> str | None:
    """Адреса для запиту перевірки (DOM.RIA — картка API, rieltor — без www,
    flombu — www…/estate_deal_sales/<id>); None — не перевіряється (Благо) або не
    оголошення."""
    link = parse(url) if url else None
    return link.fetch_url if isinstance(link, Link) and link.via not in BARE_VIAS else None


def family_of(url: str | None) -> str | None:
    """Сімейство сайту, на який веде адреса (для звітів: lun → olx/rieltor/…)."""
    link = parse(url) if url else None
    return link.family if isinstance(link, Link) else None
