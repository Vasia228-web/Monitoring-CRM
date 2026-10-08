"""«Перевірити зараз» (Блок 5, E14, D59): ліміт на роль, повтор того самого ключа, постановка.

Сайт лише ставить завдання `link` у чергу ops.lookup_checks — у мережу не ходить
(D47 п. 3). Тут — рішення «ставити чи ні»:

  * повтор того самого оголошення в межах `check.same_key_reuse_minutes` — те
    саме завдання (і його результат), без нового запиту до сайту й без
    витрати ліміту;
  * ліміт — на роль за ковзну годину (`check.per_role_per_hour`, config/lookup.toml),
    рахується за рядками ops.db (переживає перезапуск сайту); 0 — кнопки для
    ролі немає. Роль береться з сесії входу на сервері, а не з запиту;
  * рішення й постановка — під одним замком процесу: дві одночасні кнопки не
    проскочать ліміт удвох;
  * замок циклу зайнятий — завдання одразу «deferred» (як і перевірка при
    відкритті, D50): ні процесу, ні мережі до кінця циклу.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from .. import ops
from . import queue

_lock = threading.Lock()
HOUR_S = 3600.0


@dataclass(frozen=True)
class Submitted:
    job_id: int | None
    state: str | None
    reused: bool = False
    code: str | None = None          # check_rate_limited | check_off_for_role
    retry_after_s: int | None = None


def role_limit(cfg, role: str | None) -> int:
    """Ліміт ролі; вхід вимкнено (локальна розробка, role None) — як у власника."""
    return int(cfg.check.per_role_per_hour.get(role or "owner", 0))


def submit(cfg, *, role: str | None, site_key: str, target: str | None,
           cycle_busy: bool) -> Submitted:
    """Поставити «Перевірити зараз» для ключа `site_key` (або повернути наявне завдання)."""
    role = role or "owner"
    key = queue.link_key(site_key)
    now = ops._now()
    reuse_since = now - timedelta(minutes=cfg.check.same_key_reuse_minutes)
    limit = role_limit(cfg, role)
    ops.init_ops()
    with _lock, ops.ops_session() as s:
        prev = s.scalars(select(ops.LookupCheck)
                         .where(ops.LookupCheck.kind == queue.KIND_LINK,
                                ops.LookupCheck.key == key,
                                ops.LookupCheck.created_at >= reuse_since)
                         .order_by(ops.LookupCheck.id.desc()).limit(1)).first()
        if prev is not None:
            return Submitted(prev.id, prev.state, reused=True)
        if limit <= 0:
            return Submitted(None, None, code="check_off_for_role")
        hour_ago = now - timedelta(seconds=HOUR_S)
        used, oldest = s.execute(
            select(func.count(), func.min(ops.LookupCheck.created_at))
            .where(ops.LookupCheck.kind == queue.KIND_LINK, ops.LookupCheck.role == role,
                   ops.LookupCheck.created_at >= hour_ago)).one()
        if used >= limit:
            wait = HOUR_S - (now - oldest).total_seconds() if oldest is not None else HOUR_S
            return Submitted(None, None, code="check_rate_limited",
                             retry_after_s=max(1, int(wait) + 1))
        state = "deferred" if cycle_busy else "queued"
        job = ops.LookupCheck(kind=queue.KIND_LINK, key=key, state=state, role=role,
                              target=target, created_at=now,
                              message="замок циклу зайнятий" if cycle_busy else None)
        s.add(job)
        s.flush()
        return Submitted(job.id, state)


def used_in_hour(role: str | None) -> int:
    """Скільки нових перевірок роль поставила за останню годину (для сторінки)."""
    since = ops._now() - timedelta(seconds=HOUR_S)
    ops.init_ops()
    with ops.ops_session() as s:
        return s.scalar(select(func.count()).select_from(ops.LookupCheck)
                        .where(ops.LookupCheck.kind == queue.KIND_LINK,
                               ops.LookupCheck.role == (role or "owner"),
                               ops.LookupCheck.created_at >= since)) or 0
