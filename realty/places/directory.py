"""Довідник районів і ЖК: пошук за назвою й id, підписи, ієрархія (E10, D57).

Будується з трьох перевірених тем config/places/*.toml. Перевірки довідника цілком
(те, чого не бачить схема одного файлу): псевдонім (після нормалізації) веде лише до
однієї сутності; назва району не збігається з ігнорованою; район ЖК і батьківський
район існують; id DOM.RIA і LUN не дублюються (крім `ria_id_by_name`). Колізія —
DirectoryError: `cli.py config check` і `cli.py places check` її показують, і
розгортання зупиняється.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from .normalize import normalizer

log = logging.getLogger(__name__)

UNKNOWN = "_unknown"      # «не визначено» у фільтрі (NULL у базі)
NONE = "_none"            # «не в ЖК»


class DirectoryError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:5]))
        self.problems = problems


@dataclass
class Directory:
    districts: dict
    complexes: dict
    ignore: dict                      # ключ назви → вид
    rules: object
    ria_by_name: frozenset
    version: str
    _district_names: dict = field(default_factory=dict)   # ключ назви → ключ району
    _complex_names: dict = field(default_factory=dict)    # ключ назви → ключ ЖК
    _complex_by_ria: dict = field(default_factory=dict)
    _complex_by_lun: dict = field(default_factory=dict)
    _district_by_ria: dict = field(default_factory=dict)
    _district_by_lun: dict = field(default_factory=dict)
    _children: dict = field(default_factory=dict)          # район → діти
    _within: dict = field(default_factory=dict)            # парасолька → ЖК усередині

    # --- побудова ---------------------------------------------------------------------------

    @classmethod
    def build(cls, districts_cfg, complexes_cfg, rules) -> "Directory":
        norm = normalizer(rules)
        problems: list[str] = []
        districts = {d.key: d for d in districts_cfg.district}
        complexes = {c.key: c for c in complexes_cfg.complex}
        ignore: dict[str, str] = {}
        for item in districts_cfg.ignore:
            k = norm.key(item.name, "district")
            if not k:
                problems.append(f"ignore {item.name!r}: після нормалізації порожньо")
                continue
            if k in ignore and ignore[k] != item.kind:
                problems.append(f"ignore {item.name!r}: та сама назва з іншим видом")
            ignore[k] = item.kind

        d_names: dict[str, str] = {}
        for d in districts.values():
            for raw in (d.name, *d.aliases):
                k = norm.key(raw, "district")
                if not k:
                    problems.append(f"district {d.key}: назва {raw!r} після нормалізації порожня")
                    continue
                if k in ignore:
                    problems.append(f"district {d.key}: {raw!r} збігається з ігнорованою назвою")
                prev = d_names.get(k)
                if prev is not None and prev != d.key:
                    problems.append(f"district {d.key}: {raw!r} уже веде до {prev}")
                d_names[k] = d.key
        d_ria, d_lun = {}, {}
        for d in districts.values():
            for rid in d.ria_ids:
                if rid in d_ria and d_ria[rid] != d.key:
                    problems.append(f"district {d.key}: ria_id {rid} уже в {d_ria[rid]}")
                d_ria[rid] = d.key
            for lid in d.lun_ids:
                if lid in d_lun and d_lun[lid] != d.key:
                    problems.append(f"district {d.key}: lun_id {lid} уже в {d_lun[lid]}")
                d_lun[lid] = d.key

        c_names: dict[str, str] = {}
        for c in complexes.values():
            if c.district and c.district not in districts:
                problems.append(f"complex {c.key}: район {c.district!r} — такого немає")
            for raw in (c.name, *c.aliases):
                k = norm.key(raw, "complex")
                if not k:
                    problems.append(f"complex {c.key}: назва {raw!r} після нормалізації порожня")
                    continue
                prev = c_names.get(k)
                if prev is not None and prev != c.key:
                    problems.append(f"complex {c.key}: {raw!r} уже веде до {prev}")
                c_names[k] = c.key
        by_name = frozenset(complexes_cfg.ria_id_by_name)
        c_ria, c_lun = {}, {}
        for c in complexes.values():
            for rid in c.ria_ids:
                if rid in c_ria and c_ria[rid] != c.key and rid not in by_name:
                    problems.append(f"complex {c.key}: ria_id {rid} уже в {c_ria[rid]} "
                                    f"(id з кількома назвами — у ria_id_by_name)")
                c_ria.setdefault(rid, c.key)
            for lid in c.lun_ids:
                if lid in c_lun and c_lun[lid] != c.key:
                    problems.append(f"complex {c.key}: lun_id {lid} уже в {c_lun[lid]}")
                c_lun[lid] = c.key
        if problems:
            raise DirectoryError(problems)

        children: dict[str, list[str]] = {}
        for d in districts.values():
            if d.parent:
                children.setdefault(d.parent, []).append(d.key)
        within: dict[str, list[str]] = {}
        for c in complexes.values():
            if c.within:
                within.setdefault(c.within, []).append(c.key)
        return cls(districts=districts, complexes=complexes, ignore=ignore, rules=rules,
                   ria_by_name=by_name, version=_version(districts_cfg, complexes_cfg, rules),
                   _district_names=d_names, _complex_names=c_names, _complex_by_ria=c_ria,
                   _complex_by_lun=c_lun, _district_by_ria=d_ria, _district_by_lun=d_lun,
                   _children=children, _within=within)

    # --- пошук ------------------------------------------------------------------------------

    def name_key(self, text: str | None, kind: str = "district") -> str:
        return normalizer(self.rules).key(text, kind)

    def match_district(self, name: str | None) -> tuple[str, str | None]:
        """('district', ключ) | ('ignore', вид) | ('unknown', None) | ('empty', None)."""
        k = self.name_key(name, "district")
        if not k:
            return "empty", None
        if k in self._district_names:
            return "district", self._district_names[k]
        if k in self.ignore:
            return "ignore", self.ignore[k]
        return "unknown", None

    def match_complex(self, name: str | None) -> tuple[str, str | None]:
        """('complex', ключ) | ('unknown', None) | ('empty', None)."""
        k = self.name_key(name, "complex")
        if not k:
            return "empty", None
        key = self._complex_names.get(k)
        return ("complex", key) if key else ("unknown", None)

    def complex_by_ria(self, rid) -> str | None:
        """ЖК за id DOM.RIA; id з кількома назвами (`ria_id_by_name`) — None (за назвою)."""
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return None
        if rid in self.ria_by_name:
            return None
        return self._complex_by_ria.get(rid)

    def complex_by_lun(self, lid) -> str | None:
        try:
            return self._complex_by_lun.get(int(lid))
        except (TypeError, ValueError):
            return None

    def district_by_lun(self, lid) -> str | None:
        try:
            return self._district_by_lun.get(int(lid))
        except (TypeError, ValueError):
            return None

    def district_by_ria(self, rid) -> str | None:
        try:
            return self._district_by_ria.get(int(rid))
        except (TypeError, ValueError):
            return None

    # --- ієрархія й підписи -------------------------------------------------------------------

    def root(self, key: str | None) -> str | None:
        d = self.districts.get(key) if key else None
        if d is None:
            return key
        return d.parent or d.key

    def family(self, key: str) -> tuple[str, ...]:
        """Район і його діти — фільтр «Центр» показує й «Центр (Німецька колонія)»."""
        return (key, *self._children.get(key, ()))

    def complex_family(self, key: str) -> tuple[str, ...]:
        """ЖК і (для парасольки) ЖК усередині неї."""
        return (key, *self._within.get(key, ()))

    def children(self, key: str) -> tuple[str, ...]:
        return tuple(self._children.get(key, ()))

    def label(self, key: str | None) -> str | None:
        """Назва району для сайту: «Центр», дитина — «Центр (Німецька колонія)»."""
        d = self.districts.get(key) if key else None
        if d is None:
            return None
        if d.parent and d.parent in self.districts:
            return f"{self.districts[d.parent].name} ({d.name})"
        return d.name

    def area(self, key: str | None) -> str | None:
        d = self.districts.get(key) if key else None
        return d.area if d is not None else None

    def complex_label(self, key: str | None) -> str | None:
        if not key or key == NONE:
            return None
        c = self.complexes.get(key)
        return c.name if c is not None else None

    def complex_display(self, key: str | None, *, prefix: bool = True) -> str | None:
        """Назва ЖК для сайту: «ЖК Senat»; парасолька — без «ЖК» («Житловий район
        Княгинин», а не «ЖК Житловий район Княгинин»; рецензія E10). `prefix=False` —
        там, де «ЖК» вже в підписі поля (фільтр «ЖК»): лише назва."""
        c = self.complexes.get(key) if key and key != NONE else None
        if c is None:
            return None
        return f"ЖК {c.name}" if prefix and c.kind != "umbrella" else c.name

    def complex_district(self, key: str | None) -> str | None:
        c = self.complexes.get(key) if key else None
        return (c.district or None) if c is not None else None

    def related(self, a: str, b: str) -> str:
        """Як співвідносяться два ЖК квартири: same | umbrella | phase | different.

        umbrella — парасолька й ЖК усередині неї (не протиріччя); phase — різні черги
        одного ЖК (спільна group) → вид перевірки complex_phase; different → complex.
        """
        if a == b:
            return "same"
        ca, cb = self.complexes.get(a), self.complexes.get(b)
        if ca is None or cb is None:
            return "different"
        if ca.within == b or cb.within == a or (ca.within and ca.within == cb.within
                                                and (ca.kind == "umbrella" or cb.kind == "umbrella")):
            return "umbrella"
        if ca.group and ca.group == cb.group:
            return "phase"
        return "different"

    def known_names(self, kind: str) -> dict[str, str]:
        """{ключ назви: показна назва} — для підказок «схоже на…» на /status."""
        if kind == "district":
            return {k: self.label(v) or v for k, v in self._district_names.items()}
        return {k: self.complexes[v].name for k, v in self._complex_names.items()}


def _version(*cfgs) -> str:
    from .. import configfiles

    canon = json.dumps([configfiles._plain(c) for c in cfgs], sort_keys=True,
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


# --- Поточний довідник процесу ----------------------------------------------------------------


def load() -> Directory:
    """Довідник для кроку циклу й команд cli: читається на старті (`configfiles.load`)."""
    from .. import configfiles

    return Directory.build(configfiles.load("places/districts"),
                           configfiles.load("places/complexes"),
                           configfiles.load("places/rules"))


_site_cache: dict = {}


def current() -> Directory | None:
    """Довідник для сайту: перечитування за mtime (configfiles.get), раз на зміну
    версій збирається заново. Зламаний конфіг — останній чинний (або None)."""
    from .. import configfiles

    try:
        got = [configfiles.get_with_hash(n) for n in
               ("places/districts", "places/complexes", "places/rules")]
    except configfiles.ConfigError as e:
        log.error("config/places/*.toml не читається — райони й ЖК без довідника: %s", e)
        return _site_cache.get("dir")
    parts = [value for value, _ in got]
    stamp = tuple(digest for _, digest in got)
    if _site_cache.get("stamp") != stamp:
        try:
            _site_cache["dir"] = Directory.build(*parts)
        except DirectoryError as e:
            log.error("довідник районів і ЖК не зібрався: %s", e)
        _site_cache["stamp"] = stamp
    return _site_cache.get("dir")
