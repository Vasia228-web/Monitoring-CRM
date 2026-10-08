"""Район і ЖК за координатами — kNN по точках-орієнтирах (вимкнено до E11; D57).

Орієнтири — лише оголошення з точними координатами (rules.coords.precise_geo) і
ключем, визначеним ступенем «з джерела» (src / src_id / src_name). Самоперевірка на
кожному запуску: ціль відкладається разом з усіма точками ближче
validate_exclude_m (інакше «вгадує» копія того самого будинку); нижче порогу точності
ступінь не заповнює нічого.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict

_CELL = (0.004, 0.006)          # ≈ 445 × 440 м біля 48,9° пн. ш.


def distance_m(lat1, lon1, lat2, lon2) -> float:
    lat = math.radians((lat1 + lat2) / 2)
    return math.hypot((lat1 - lat2) * 111_320, (lon1 - lon2) * 111_320 * math.cos(lat))


class PointIndex:
    """Сітка точок (lat, lon, ключ, id оголошення)."""

    def __init__(self, refs) -> None:
        self.cells: dict[tuple[int, int], list] = defaultdict(list)
        self.n = 0
        for lat, lon, key, lid in refs:
            self.cells[self._cell(lat, lon)].append((lat, lon, key, lid))
            self.n += 1

    @staticmethod
    def _cell(lat, lon) -> tuple[int, int]:
        return int(math.floor(lat / _CELL[0])), int(math.floor(lon / _CELL[1]))

    def near(self, lat, lon, radius_m: float, *, exclude_m: float = 0.0,
             exclude_id: int | None = None) -> list[tuple[float, str, int]]:
        ci, cj = self._cell(lat, lon)
        span_i = int(math.ceil(radius_m / 111_320 / _CELL[0])) + 1
        span_j = int(math.ceil(radius_m / (111_320 * math.cos(math.radians(lat))) / _CELL[1])) + 1
        out = []
        for i in range(ci - span_i, ci + span_i + 1):
            for j in range(cj - span_j, cj + span_j + 1):
                for plat, plon, key, lid in self.cells.get((i, j), ()):
                    if lid == exclude_id:
                        continue
                    d = distance_m(lat, lon, plat, plon)
                    if d <= radius_m and d >= exclude_m:
                        out.append((d, key, lid))
        out.sort()
        return out


def vote_district(index: PointIndex, lat, lon, cfg, root, *, exclude_m: float = 0.0,
                  exclude_id: int | None = None) -> str | None:
    """Район за K найближчими в радіусі: ≥ min_refs точок і згода ≥ agree (за коренем)."""
    near = index.near(lat, lon, cfg.district_radius_m, exclude_m=exclude_m,
                      exclude_id=exclude_id)[: cfg.district_k]
    if len(near) < cfg.district_min_refs:
        return None
    roots = Counter(root(key) for _, key, _ in near)
    top, n = roots.most_common(1)[0]
    return top if n / len(near) >= cfg.district_agree else None


def vote_complex(index: PointIndex, lat, lon, cfg, *, exclude_m: float = 0.0,
                 exclude_id: int | None = None) -> str | None:
    """ЖК: ≥ min_refs точок у радіусі й одностайно."""
    near = index.near(lat, lon, cfg.complex_radius_m, exclude_m=exclude_m,
                      exclude_id=exclude_id)
    if len(near) < cfg.complex_min_refs:
        return None
    keys = {key for _, key, _ in near}
    return keys.pop() if len(keys) == 1 else None


def validate(index: PointIndex, refs, vote, *, exclude_m: float, sample: int,
             seed: int = 20261008, same=lambda a, b: a == b) -> dict:
    """Точність і покриття ступеня на відкладених орієнтирах (детермінована вибірка)."""
    refs = list(refs)
    if len(refs) > sample:
        refs = random.Random(seed).sample(refs, sample)
    answered = correct = 0
    for lat, lon, key, lid in refs:
        got = vote(lat, lon, exclude_m=exclude_m, exclude_id=lid)
        if got is None:
            continue
        answered += 1
        correct += same(got, key)
    return {"n": len(refs), "answered": answered, "correct": correct,
            "precision": round(correct / answered, 4) if answered else None,
            "coverage": round(answered / len(refs), 4) if refs else None}
