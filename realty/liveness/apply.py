"""Застосування вердиктів: журнал перевірок, зняття, повернення, ремонт посилань (E8, D52).

Порядок (інтеграція, конфлікт 7): мережева фаза вже скінчилась і результати в
пам'яті → запобіжник оцінює ВЕСЬ прогін (fuse.evaluate) → застосування пакетами
≤ `run.apply_batch_rows` рядків, кожен пакет — окрема коротка транзакція. Ключ
ніколи не ділиться між пакетами (інакше збій посередині лишив би ключ «змішаним»).

Вердикт ключа йде ВСІМ його рядкам (копіям LUN теж): кожен рядок отримує свій
check_event, а зміни стану — подію в listing_events:

  * alive   — last_checked, last_alive_at, check_failures = 0; знятий рядок без
              ручної позначки повертається: is_active = 1, delisted_at і
              source_removed_at = NULL (попередні — у події «returned», з точністю
              до мікросекунди, разом із причиною й id зняття, яке повернення
              скасувало); ремонт посилання — listings.probe_url (стара адреса — у
              події url_repaired);
  * removed — is_active = 0, delisted_at = зараз, source_removed_at — лише якщо
              порожньо; last_alive_at НЕ чіпаємо (межа інтервалу для строку
              продажу); попередні is_active, last_checked, check_failures — у події;
  * not_found — лише спроба (серію 404 рахує черга з check_events);
  * unknown — check_failures + 1.

Застаріла відповідь (рядок уже має новішу перевірку: last_checked, last_alive_at
чи delisted_at пізніші за час цієї відповіді — напр., процес перевірки при
відкритті встиг застосувати «живе», поки мережева фаза циклу ще тривала) — лише
журнал перевірки, стан не змінюється (рецензія E8, D52).

Під запобіжником (джерело рядка чи сайт ключа, fuse.held_for): журнал перевірки й
спроба пишуться, стан — ні (ні зняття, ні повернення, ні last_checked); ключ,
хоч один рядок якого з такого джерела, — цілком без змін стану. `last_seen` тут
не пишеться ніколи (D43: лише pipeline._upsert). Докази Блоків 3/4 з гачків — лише
туди, де порожньо.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from ..models import CheckEvent, Listing, ListingEvent
from . import fuse, policy as pol
from .signatures import ALIVE, NOT_FOUND, REMOVED, UNKNOWN

log = logging.getLogger(__name__)

FILL_ONLY_JSON = ("place_raw", "seller_evidence")
FILL_ONLY_SCALAR = ("seller_profile",)
# Пакет, якому SQLite відповів «database is locked» (інший процес довше за
# busy_timeout тримав замок запису), повторюємо стільки разів через стільки секунд
# (рецензія E9, D53). Безпечно: невдалий пакет відкочується цілком, попередні вже
# записані, а ключ ніколи не ділиться між пакетами.
LOCKED_RETRIES = 3
LOCKED_RETRY_WAIT_S = 5.0


class ApplyInterrupted(Exception):
    """Пакет так і не записався (замок бази): `rep` — те, що вже записано (попередні
    пакети), `pending` — результати, яких не застосовано (цей пакет і решта), щоб
    застосувати пізніше, не губити й не рахувати двічі."""

    def __init__(self, rep: "ApplyReport", pending: list, cause: BaseException) -> None:
        super().__init__(f"{type(cause).__name__}: {str(cause)[:300]}")
        self.rep = rep
        self.pending = pending
        self.cause = cause


@dataclass
class ApplyReport:
    checked: int = 0                 # рядків із журналом перевірки
    alive: int = 0
    delisted: int = 0                # переходів «актуальне → знято»
    restored: int = 0                # повернень
    unknown: int = 0                 # not_found + unknown
    not_found: int = 0
    repaired: int = 0
    held: int = 0                    # рядків, чий стан не змінено через запобіжник
    stale: int = 0                   # рядків із новішою перевіркою — стан не змінено
    captured: int = 0
    batches: int = 0
    longest_batch_s: float = 0.0
    trips: list = field(default_factory=list)
    held_sources: list = field(default_factory=list)
    by_source: dict = field(default_factory=dict)
    by_signature: dict = field(default_factory=dict)
    by_tier: dict = field(default_factory=dict)
    by_property: dict = field(default_factory=dict)   # квартира → лічильники (для завдань)
    # Хост ключа → ключів і рядків за наслідком (нічний звіт «по сайтах», E9, D53).
    by_host: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def merge(self, other: "ApplyReport") -> None:
        """Додати лічильники записаного пакета (запобіжник — лише в загальному звіті)."""
        for name in ("checked", "alive", "delisted", "restored", "unknown", "not_found",
                     "repaired", "held", "stale", "captured", "batches"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.longest_batch_s = max(self.longest_batch_s, other.longest_batch_s)
        for name in ("by_source", "by_signature", "by_tier", "by_property", "by_host"):
            _merge_counts(getattr(self, name), getattr(other, name))


def _merge_counts(into: dict, more: dict) -> None:
    for k, v in more.items():
        if isinstance(v, dict):
            _merge_counts(into.setdefault(k, {}), v)
        else:
            into[k] = into.get(k, 0) + v


def _batches(outcomes, size: int):
    batch, rows = [], 0
    for oc in outcomes:
        n = len(oc.item.rows)
        if batch and rows + n > size:
            yield batch
            batch, rows = [], 0
        batch.append(oc)
        rows += n
    if batch:
        yield batch


def _fill_only(row: Listing, capture: dict, now) -> bool:
    """Докази Блоків 3/4 — лише нові ключі, наявні значення не переписуються."""
    changed = False
    for name in FILL_ONLY_JSON:
        new = capture.get(name)
        if not isinstance(new, dict) or not new:
            continue
        old = dict(getattr(row, name) or {})
        add = {k: v for k, v in new.items() if k not in old and v not in (None, "", [], {})}
        if add:
            setattr(row, name, {**old, **add})
            changed = True
            if name == "seller_evidence":
                row.seller_evidence_at = now
    for name in FILL_ONLY_SCALAR:
        new = capture.get(name)
        if new and getattr(row, name) is None:
            setattr(row, name, str(new)[:96])
            changed = True
    return changed


def _bump(d: dict, *keys, by: int = 1) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = d.get(keys[-1], 0) + by


def apply_outcomes(outcomes, *, cfg, scope, run_id: int | None = None,
                   prior: dict | None = None) -> ApplyReport:
    """Записати результати прогону: запобіжник — спершу, далі пакети.

    `prior` — перевірки за вікно fuse.window_hours для малого прогону (не кроку
    циклу; fuse.window_counts)."""
    rep = ApplyReport()
    done = [oc for oc in outcomes if oc.verdict is not None]
    trips = fuse.evaluate(done, cfg, prior=prior)
    if trips:
        newly = fuse.trip(trips, run_id=run_id, mode=cfg.fuse.mode)
        for t in trips:
            log.error("ЗАПОБІЖНИК: %s — «знято» %s із %s (%s, за %s); нічого не знімаємо "
                      "й не повертаємо, доки власник не зніме запобіжник",
                      t.source, t.removed, t.checked, t.reason, t.scope)
        rep.trips = [{"source": t.source, "reason": t.reason, "checked": t.checked,
                      "removed": t.removed, "share": t.share, "scope": t.scope,
                      "new": t.source in newly}
                     for t in trips]
    held = fuse.held_sources()
    rep.held_sources = sorted(held)
    batches = list(_batches(done, cfg.run.apply_batch_rows))
    for n, batch in enumerate(batches):
        attempt = 0
        while True:
            # Лічильники пакета — окремо: відкочений пакет не має потрапити у звіт.
            brep = ApplyReport()
            started = time.perf_counter()
            try:
                with scope() as s:
                    ids = [rid for oc in batch for rid in oc.item.listing_ids]
                    rows = {r.id: r for r in s.scalars(select(Listing)
                                                       .where(Listing.id.in_(ids)))}
                    last_removal = _last_removals(s, [r.id for r in rows.values()
                                                      if not r.is_active])
                    pending_events: list[tuple[CheckEvent, ListingEvent]] = []
                    for oc in batch:
                        _apply_key(s, oc, rows, fuse.held_for(cfg, oc.item, held), brep,
                                   pending_events, run_id, last_removal)
                    if pending_events:
                        s.flush()
                        for check, event in pending_events:
                            event.check_event_id = check.id
                            s.add(event)
            except OperationalError as e:
                if "locked" in str(e) and attempt < LOCKED_RETRIES:
                    attempt += 1
                    log.warning("пакет застосування %d: база зайнята (%s) — повтор %d із %d "
                                "через %.0f с", n + 1, str(e)[:120], attempt, LOCKED_RETRIES,
                                LOCKED_RETRY_WAIT_S)
                    time.sleep(LOCKED_RETRY_WAIT_S)
                    continue
                raise ApplyInterrupted(rep, [oc for b in batches[n:] for oc in b], e) from e
            break
        brep.batches = 1
        brep.longest_batch_s = round(time.perf_counter() - started, 3)
        rep.merge(brep)
    return rep


def _last_removals(s, ids: list[int]) -> dict[int, tuple]:
    """Останнє зняття (подія removed) кожного знятого рядка пакета: (at, id, причина)
    — одним запитом на пакет; повернення записує, яке саме зняття воно скасувало."""
    out: dict[int, tuple] = {}
    if not ids:
        return out
    for lid, at, eid, reason in s.execute(
            select(ListingEvent.listing_id, ListingEvent.at, ListingEvent.id,
                   ListingEvent.reason)
            .where(ListingEvent.kind == "removed", ListingEvent.listing_id.in_(ids))
            .order_by(ListingEvent.at, ListingEvent.id)):
        out[lid] = (at, eid, reason)
    return out


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _newer(row: Listing, at) -> bool:
    """У рядка вже є відповідь, новіша за цю (застосована іншим процесом)."""
    return any(t is not None and t > at for t in (row.last_checked, row.last_alive_at,
                                                  row.delisted_at))


def _apply_key(s, oc, rows: dict, key_held: bool, rep: ApplyReport, pending, run_id,
               last_removal: dict | None = None) -> None:
    v, item, at = oc.verdict, oc.item, oc.at
    rep.by_signature[v.signature] = rep.by_signature.get(v.signature, 0) + 1
    _bump(rep.by_tier, item.tier, v.kind)
    host = rep.by_host.setdefault(item.host, {
        "keys": 0, "rows": 0, "alive": 0, "delisted": 0, "restored": 0, "repaired": 0,
        "unknown": 0, "not_found": 0, "held": 0, "stale": 0, "held_keys": 0})
    host["keys"] += 1
    host["held_keys"] += int(key_held)
    evidence = {**v.evidence, "signature": v.signature, "tier": item.tier,
                **({"run_id": run_id} if run_id is not None else {})}
    for plan_row in item.rows:
        row = rows.get(plan_row.id)
        if row is None:
            continue
        check = CheckEvent(listing_id=row.id, checked_at=at, code=int(v.code),
                           alive=v.alive_flag, reason=item.tier[:16], signature=v.signature)
        s.add(check)
        rep.checked += 1
        host["rows"] += 1
        src = rep.by_source.setdefault(row.source, {"checked": 0, "alive": 0, "delisted": 0,
                                                    "restored": 0, "unknown": 0, "held": 0})
        src["checked"] += 1
        prop = rep.by_property.setdefault(row.property_id, {
            "checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0})
        prop["checked"] += 1
        # Спроба — завжди: саме вона рухає чергу далі (і не назад у часі).
        if row.last_attempt is None or row.last_attempt < at:
            row.last_attempt = at
        if oc.capture and _fill_only(row, oc.capture, at):
            rep.captured += 1
        held_or_stale = key_held or _newer(row, at)
        if held_or_stale:
            if key_held:
                rep.held += 1
                src["held"] += 1
                host["held"] += 1
            else:
                rep.stale += 1
                host["stale"] += 1
            if v.kind in (NOT_FOUND, UNKNOWN):
                rep.unknown += 1
                src["unknown"] += 1
                prop["unknown"] += 1
                host["unknown"] += 1
            continue
        prev = {"prev_is_active": bool(row.is_active), "prev_last_checked": _iso(row.last_checked),
                "prev_check_failures": int(row.check_failures or 0)}
        if v.kind == ALIVE:
            rep.alive += 1
            src["alive"] += 1
            prop["alive"] += 1
            host["alive"] += 1
            row.check_failures = 0
            # «Перевірено» — лише за зрозумілої відповіді: на цій даті тримається
            # інтервал для аналізу виживання.
            row.last_checked = at
            row.last_alive_at = at
            if v.repaired_url and row.probe_url != v.repaired_url:
                pending.append((check, ListingEvent(
                    listing_id=row.id, at=at, kind="url_repaired",
                    reason=(v.repair_strategy or "")[:24], source=row.source,
                    site_key=row.site_key,
                    evidence={**evidence, "old_url": pol.safe_url(row.probe_url or item.url),
                              "new_url": pol.safe_url(v.repaired_url)})))
                row.probe_url = v.repaired_url
                rep.repaired += 1
                host["repaired"] += 1
            if not row.is_active and row.manual_active is None:
                prev.update(prev_delisted_at=_iso(row.delisted_at),
                            prev_source_removed_at=_iso(row.source_removed_at))
                removal = (last_removal or {}).get(row.id)
                if removal is not None and removal[0] <= at:
                    prev.update(prev_removal_at=_iso(removal[0]), prev_removal_event_id=removal[1],
                                prev_removal_reason=removal[2])
                else:
                    prev.update(prev_removal_at=None, prev_removal_event_id=None,
                                prev_removal_reason="legacy")
                row.is_active = True
                row.delisted_at = None
                row.source_removed_at = None
                pending.append((check, ListingEvent(
                    listing_id=row.id, at=at, kind="returned", reason=item.tier[:24],
                    source=row.source, site_key=row.site_key, evidence={**evidence, **prev})))
                rep.restored += 1
                src["restored"] += 1
                prop["restored"] += 1
                host["restored"] += 1
                log.info("Повернулось у продаж (%s): %s", item.tier,
                         pol.safe_url(row.original_url))
        elif v.kind == REMOVED:
            row.check_failures = 0
            row.last_checked = at
            if row.is_active:
                row.is_active = False
                row.delisted_at = at
                if v.source_removed_at is not None and row.source_removed_at is None:
                    row.source_removed_at = v.source_removed_at
                # `last_alive_at` НЕ чіпаємо: разом із `delisted_at` воно задає
                # інтервал, усередині якого оголошення зникло.
                pending.append((check, ListingEvent(
                    listing_id=row.id, at=at, kind="removed", reason=v.signature[:24],
                    source=row.source, site_key=row.site_key, evidence={**evidence, **prev})))
                rep.delisted += 1
                src["delisted"] += 1
                prop["delisted"] += 1
                host["delisted"] += 1
                log.info("Знято з продажу (%s, %s): %s", v.signature, item.tier,
                         pol.safe_url(row.original_url))
        elif v.kind == NOT_FOUND:
            rep.not_found += 1
            rep.unknown += 1
            src["unknown"] += 1
            prop["unknown"] += 1
            host["unknown"] += 1
            host["not_found"] += 1
        else:
            row.check_failures = (row.check_failures or 0) + 1
            rep.unknown += 1
            src["unknown"] += 1
            prop["unknown"] += 1
            host["unknown"] += 1

