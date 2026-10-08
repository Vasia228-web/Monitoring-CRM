"""Ключ плану й вердикт ↔ JSON: файл плану смуги й рядки її результатів (E9, D53).

Смуга — окремий процес: план (WorkItem з рядками ключа, серією 404) вона читає з
файла, а кожен результат дописує рядком JSON. Диригент тримає свій план у пам'яті й
зіставляє результат за номером ключа в плані смуги (`i`), тож назад у рядку йде лише
вердикт, час відповіді й докази Блоків 3/4 (уже без тіла сторінки).
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime

from ..liveness.queue import Row, WorkItem
from ..liveness.signatures import Verdict

_ROW_DATES = ("delisted_at", "last_seen", "last_attempt", "last_checked", "absent_since",
              "viewed_at")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def row_to_json(row: Row) -> dict:
    d = asdict(row)
    for name in _ROW_DATES:
        d[name] = _iso(d[name])
    return d


def row_from_json(d: dict) -> Row:
    kw = {f.name: d[f.name] for f in fields(Row)}
    for name in _ROW_DATES:
        kw[name] = _dt(kw[name])
    return Row(**kw)


def item_to_json(item: WorkItem) -> dict:
    return {"key": item.key, "host": item.host, "url": item.url, "tier": item.tier,
            "rows": [row_to_json(r) for r in item.rows],
            "streak404": [_iso(t) for t in item.streak404], "jobs": list(item.jobs),
            "body_cap": int(item.body_cap)}


def item_from_json(d: dict) -> WorkItem:
    return WorkItem(key=d["key"], host=d["host"], url=d["url"], tier=d["tier"],
                    rows=tuple(row_from_json(r) for r in d["rows"]),
                    streak404=tuple(_dt(t) for t in d["streak404"]),
                    jobs=tuple(d.get("jobs") or ()), body_cap=int(d.get("body_cap") or 0))


def verdict_to_json(v: Verdict) -> dict:
    return {"kind": v.kind, "signature": v.signature, "code": int(v.code),
            "evidence": v.evidence, "source_removed_at": _iso(v.source_removed_at),
            "repaired_url": v.repaired_url, "repair_strategy": v.repair_strategy}


def verdict_from_json(d: dict) -> Verdict:
    return Verdict(kind=d["kind"], signature=d["signature"], code=int(d["code"]),
                   evidence=d.get("evidence") or {},
                   source_removed_at=_dt(d.get("source_removed_at")),
                   repaired_url=d.get("repaired_url"),
                   repair_strategy=d.get("repair_strategy"))
