"""Карантин: жоден запис не потрапляє у видачу без перевірки.

Фізично окремої таблиці немає — статус зберігається полем `quality_status`.
Це той самий карантин, але без дублювання схеми й переписування пайплайна:
щойно зібраний запис має статус `pending` і невидимий для інтерфейсу, доки
перевірка не переведе його в `ok`, `review` або `rejected`.

Escalation: якщо в одному прогоні джерела відхилено понад `ESCALATION_RATE`
записів, пакет не приймається взагалі — формується звіт із прикладами.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..models import Listing
from .rules import (
    ESCALATION_MIN_BATCH, ESCALATION_RATE, Thresholds, load_thresholds, validate,
)

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class GateReport:
    """Підсумок перевірки — у форматі, який показує /status."""

    accepted: int = 0
    review: int = 0
    rejected: int = 0
    by_source: dict[str, dict] = field(default_factory=lambda: defaultdict(
        lambda: {"accepted": 0, "review": 0, "rejected": 0}))
    escalated: list[str] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.accepted + self.review + self.rejected

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "review": self.review,
                "rejected": self.rejected, "total": self.total,
                "by_source": {k: dict(v) for k, v in self.by_source.items()},
                "escalated": self.escalated, "samples": self.samples[:10]}

    def render(self) -> str:
        lines = ["", "=" * 62, "КОНТРОЛЬ ЯКОСТІ", "=" * 62]
        for src, st in sorted(self.by_source.items()):
            total = sum(st.values())
            bad = st["rejected"]
            share = f"{100 * bad / total:.1f}%" if total else "—"
            lines.append(f"  {src:<8} прийнято={st['accepted']:<5} "
                         f"на перегляд={st['review']:<4} відхилено={st['rejected']:<4} "
                         f"({share})")
        lines.append("-" * 62)
        lines.append(f"  Разом: прийнято {self.accepted}, на перегляді {self.review}, "
                     f"відхилено {self.rejected}")
        if self.escalated:
            lines.append("")
            lines.append(f"  ЕСКАЛАЦІЯ: {', '.join(self.escalated)} — пакет не прийнято")
            for s in self.samples[:5]:
                lines.append(f"    {s['source']}: {s['reason']}")
        lines.append("=" * 62)
        return "\n".join(lines)


class QualityGate:
    """Перевіряє пакет записів перед записом у базу."""

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds or load_thresholds()
        self.report = GateReport()

    def _previous_price(self, session, rec: dict) -> float | None:
        row = session.scalar(
            select(Listing.price_usd).where(
                Listing.source == rec.get("source"),
                Listing.external_id == str(rec.get("external_id")),
            )
        )
        return row

    def screen(self, session, records: list[dict]) -> list[dict]:
        """Розставляє статуси. Повертає записи, придатні до запису в базу.

        Відхилені теж повертаються — вони зберігаються зі статусом `rejected`,
        бо видаляти дані не можна: причина відмови має лишитись видимою.
        """
        per_source: dict[str, list[dict]] = defaultdict(list)
        for rec in records:
            per_source[rec.get("source", "?")].append(rec)

        out: list[dict] = []
        for source, batch in per_source.items():
            decisions = []
            for rec in batch:
                verdict, reasons = validate(
                    rec, self.thresholds, self._previous_price(session, rec)
                )
                decisions.append((rec, verdict, reasons))

            bad = sum(1 for _, v, _ in decisions if v == "rejected")
            if len(batch) >= ESCALATION_MIN_BATCH and bad / len(batch) > ESCALATION_RATE:
                # Пакет підозрілий цілком: швидше зламався парсер, ніж джерело
                # раптом почало публікувати сміття. Нічого не приймаємо.
                self.report.escalated.append(
                    f"{source} ({bad} із {len(batch)}, {100*bad/len(batch):.0f}%)")
                # Приклади беремо саме з відхилених, а не з початку пакета:
                # інакше звіт про ескалацію виходить без жодної причини.
                rejected_only = [(r, rs) for r, v, rs in decisions if v == "rejected"]
                for rec, reasons in rejected_only[:5]:
                    self.report.samples.append(
                        {"source": source, "url": (rec.get("original_url") or "")[:90],
                         "reason": "; ".join(reasons), "verdict": "rejected"})
                log.error("ЕСКАЛАЦІЯ %s: відхилено %d із %d — пакет не прийнято",
                          source, bad, len(batch))
                continue

            for rec, verdict, reasons in decisions:
                rec["quality_status"] = "ok" if verdict == "ok" else verdict
                rec["quality_reason"] = "; ".join(reasons) or None
                rec["quality_checked_at"] = _now()
                key = {"ok": "accepted", "review": "review", "rejected": "rejected"}[verdict]
                self.report.by_source[source][key] += 1
                setattr(self.report, key, getattr(self.report, key) + 1)
                if verdict != "ok" and len(self.report.samples) < 20:
                    self.report.samples.append(
                        {"source": source, "url": (rec.get("original_url") or "")[:90],
                         "reason": "; ".join(reasons), "verdict": verdict})
                out.append(rec)
        return out
