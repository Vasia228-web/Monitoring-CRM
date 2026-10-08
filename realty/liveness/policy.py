"""Політики хостів Блоку 1: який хост, яким методом, яку адресу питати (E8, D52).

Усе — з `config/liveness.toml` (хости, паузи, підписи) і `config/links.toml`
(розбір посилань, ключ «сайт:id», канонічні адреси). Одиниця перевірки — ключ
`listings.site_key` (domria:…, olx:…, rieltor:…, lun:…, flombu:…, blago:…); рядок,
чия адреса не розбирається (ключа немає), перевіряється сам по собі під ключем
«row:<id>» — так зламаний links.toml чи нова форма адреси не викидають рядок із
перевірки мовчки.
"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from .. import configfiles, links

TOPIC = "liveness"
ROW_KEY_PREFIX = "row:"


def load():
    """Конфіг для кроку циклу чи нічної роботи — один раз на старті (суворо)."""
    return configfiles.load(TOPIC)


def load_with_hash():
    return configfiles.load_with_hash(TOPIC)


def current():
    """Чинний конфіг для довгоживучого процесу (сайт): перечитування за mtime."""
    return configfiles.get(TOPIC)


def row_key(listing_id: int) -> str:
    return f"{ROW_KEY_PREFIX}{int(listing_id)}"


def is_row_key(key: str) -> bool:
    return key.startswith(ROW_KEY_PREFIX)


def family_of_key(key: str | None) -> str | None:
    if not key or is_row_key(key):
        return None
    return key.split(":", 1)[0]


def id_of_key(key: str | None) -> str | None:
    if not key or is_row_key(key):
        return None
    return key.split(":", 1)[1]


def _norm_netloc(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def host_for_family(cfg, family: str | None) -> str | None:
    if not family:
        return None
    for host, spec in cfg.hosts.items():
        if spec.family == family:
            return host
    return None


def host_of_url(cfg, url: str | None) -> str | None:
    """Хост політики для адреси: за сімейством розбору, інакше — за доменом.

    Піддомени агенцій rieltor (часом це номер телефону) зводяться до rieltor.ua:
    розбір links знає їх за `hosts.subdomain_family`, а запасний шлях — за
    суфіксом домену.
    """
    if not url:
        return None
    try:
        family = links.family_of(url)
    except configfiles.ConfigError:
        family = None
    host = host_for_family(cfg, family)
    if host is not None:
        return host
    netloc = _norm_netloc(url)
    if netloc in cfg.hosts:
        return netloc
    for known in cfg.hosts:
        if netloc.endswith("." + known):
            return known
    return None


def host_for(cfg, key: str | None, url: str | None) -> str | None:
    """Хост ключа: для «сайт:id» — за сімейством, для «row:<id>» — за адресою."""
    family = family_of_key(key)
    if family is not None:
        host = host_for_family(cfg, family)
        if host is not None:
            return host
    return host_of_url(cfg, url)


def checkable_hosts(cfg) -> dict:
    return {h: spec for h, spec in cfg.hosts.items() if spec.checkable}


def is_checkable(cfg, url: str | None) -> bool:
    host = host_of_url(cfg, url)
    return host is not None and cfg.hosts[host].checkable


def probe_url(cfg, host: str, url: str) -> str:
    """Адреса запиту для хоста: канонічна сторінка (DOM.RIA /uk/…html) чи адреса
    перевірки (rieltor без www, flombu з www, lun.ua напряму). Не розібралась —
    як є (ключ «row:<id>»)."""
    spec = cfg.hosts[host]
    try:
        if spec.probe == "canonical":
            got = links.canonical_url(url)
        elif spec.probe == "fetch":
            got = links.canonical_fetch_url(url)
        else:
            got = None
    except configfiles.ConfigError:
        got = None
    return got or url


def pace(cfg, host: str, mode: str = "cycle") -> float:
    """Пауза між запитами до хоста: цикл — `delay`; нічна смуга — не швидше за
    повний збір джерела (інтеграція, конфлікт 4: max(delay, full_delay))."""
    spec = cfg.hosts[host]
    if mode == "night" and spec.pace_source:
        from ..config import SOURCES

        src = SOURCES.get(spec.pace_source)
        if src is not None:
            return max(spec.delay, src.full_delay or src.delay)
    return spec.delay


@lru_cache(maxsize=1)
def _family_hosts_digest(digest: str) -> dict[str, str]:
    cfg = configfiles.get("links")
    out: dict[str, str] = {}
    for host, fam in cfg.hosts.family.items():
        out.setdefault(fam, host)
    return out


def safe_url(url: str | None) -> str | None:
    """Адреса для доказів і журналів: без query й #фрагмента, піддомен агенції →
    домен сайту, телефони в шляху → «[телефон]» (інтеграція, прогалина 8).

    Повна адреса потрібна лише запиту; у listing_events і на /status вона не
    потрібна — а query несе токени, піддомен rieltor буває номером телефону.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    try:
        family = links.family_of(url)
        if family:
            digest = configfiles.get_hash("links")
            host = _family_hosts_digest(digest).get(family, host)
    except configfiles.ConfigError:
        pass
    path = parts.path
    try:
        from .. import privacy

        path = privacy.redact_phones(path) or path
    except configfiles.ConfigError:
        path = ""
    return urlunsplit((parts.scheme or "https", host, path, "", ""))
