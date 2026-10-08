"""Запобіжник перевірки актуальності — обидва тлумачення «20%» (E8, D52).

Правило ескалації промту: підпис «знято» спрацював на понад 20% перевірених одного
джерела за прогін → для джерела нічого не знімаємо, тривога в Telegram (сторож),
тримаємо, доки власник не зніме запобіжник на /status. Як рахувати 20% — питання
власнику ще відкрите, тож `fuse.mode`: literal (типово) і tiered (пропозиція плану).
На коді до E8 запобіжника не було — сім «знято» з тридцяти знімались усі.
"""
from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, add, clean_fuse, db, events, get, olx_url, ria_page, ria_url,
)

from realty import configfiles, verify  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")


def _mode(tmp_path, monkeypatch, mode: str):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for f in (configfiles.ROOT / "config").glob("*.toml"):
        shutil.copy(f, cfg_dir / f.name)
    path = cfg_dir / "liveness.toml"
    # Будь-яке поточне значення: з 08.10 у конфігу "tiered" (рішення власника, D55).
    path.write_text(re.sub(r'^mode = "\w+"', f'mode = "{mode}"', path.read_text(encoding="utf-8"),
                           count=1, flags=re.M), encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(cfg_dir))
    assert configfiles.load("liveness").fuse.mode == mode


def _domria(db, n, start=34100000, **kw):
    ids = []
    for i in range(n):
        rid = start + i
        ids.append(add(db, ria_url(rid), source="domria", external_id=str(rid), **kw))
    return ids


def test_literal_share_over_20pct_removes_nothing_and_holds_until_cleared(db, tmp_path,
                                                                          monkeypatch):
    """7 «знято» (410) із 30 перевірених OLX = 23% → для OLX нічого; rieltor того ж
    прогону — застосовано. На коді до E8 сім оголошень OLX знімались. Режим literal
    (з 08.10 у конфігу tiered: точкові перевірки там — не випадкові, D56)."""
    _mode(tmp_path, monkeypatch, "literal")
    ids = [add(db, olx_url(f"10Fz{i:03d}"), source="olx", external_id=f"f{i}")
           for i in range(30)]
    net = FakeNet({f"olx:10Fz{i:03d}": 410 if i < 7 else 200 for i in range(30)})
    other = add(db, "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13900001/",
                source="lun", external_id="r")
    net.responses["rieltor:13900001"] = 410
    stats = verify.verify_batch(ids=ids + [other], http=net)
    assert all(get(db, i).is_active for i in ids), "джерело під запобіжником — нічого не знято"
    assert get(db, other).is_active is False, "інші джерела того ж прогону застосовуються"
    from realty.liveness import fuse

    assert [t["source"] for t in stats["trips"]] == ["olx"]
    assert fuse.held_sources() == {"olx"}
    assert len([e for i in ids for e in events(db, i)]) == 0
    # Наступний прогін — так само нічого, доки власник не зняв.
    verify.verify_batch(ids=ids, http=net)
    assert all(get(db, i).is_active for i in ids)
    assert fuse.clear("olx", by="test") is True
    verify.verify_batch(ids=ids[:7], http=net)                   # 7 з 7 — n < min_checked
    assert sum(not get(db, i).is_active for i in ids[:7]) == 7


def test_small_runs_are_below_the_minimum_and_never_trip(db):
    """2 з 3 оголошень однієї квартири — не ознака зламаного підпису (n < min_checked)."""
    ids = _domria(db, 3, start=34200000)
    net = FakeNet({f"domria:{34200000 + i}": (200, ria_page(34200000 + i, archived=i < 2))
                   for i in range(3)})
    verify.verify_batch(ids=ids, http=net)
    assert [get(db, i).is_active for i in ids] == [False, False, True]
    from realty.liveness import fuse
    assert fuse.held_sources() == set()


def test_tiered_mode_lets_hinted_candidates_through(db, tmp_path, monkeypatch):
    """Пропозиція плану: 60 «зниклих з переліку» з 85% архіву + 40 сліпих без зняттів —
    зняття застосовано; у literal той самий прогін тримав би джерело."""
    from realty.liveness import fuse

    _mode(tmp_path, monkeypatch, "tiered")
    hinted = _domria(db, 60, start=34300000, absent_since=datetime(2026, 10, 7))
    blind = _domria(db, 40, start=34400000)
    archived = {34300000 + i for i in range(51)}                 # 51 з 60 = 85%
    net = FakeNet({f"domria:{r}": (200, ria_page(r, archived=r in archived))
                   for r in [*range(34300000, 34300060), *range(34400000, 34400040)]})
    verify.verify_batch(http=net)                                # повний прогін ярусами
    assert fuse.held_sources() == set()
    assert sum(not get(db, i).is_active for i in hinted) == 51
    assert all(get(db, i).is_active for i in blind)


def test_literal_mode_counts_every_tier(db, tmp_path, monkeypatch):
    """Той самий прогін у literal: частка рахується по всіх перевірених."""
    _mode(tmp_path, monkeypatch, "literal")
    hinted = _domria(db, 60, start=34500000, absent_since=datetime(2026, 10, 7))
    _domria(db, 40, start=34600000)
    archived = {34500000 + i for i in range(51)}
    net = FakeNet({f"domria:{r}": (200, ria_page(r, archived=r in archived))
                   for r in [*range(34500000, 34500060), *range(34600000, 34600040)]})
    stats = verify.verify_batch(http=net)
    assert all(get(db, i).is_active for i in hinted)
    from realty.liveness import fuse
    assert fuse.held_sources() == {"domria"}, stats["trips"]


def test_two_canaries_reporting_removed_hold_the_source(db, tmp_path, monkeypatch):
    """Контрольні — свіжі в стрічці: два з них «знято» тримають джерело навіть у tiered."""
    from realty.liveness import fuse, queue

    _mode(tmp_path, monkeypatch, "tiered")
    now = datetime(2026, 10, 8, 3, 0)
    monkeypatch.setattr(queue, "_now", lambda: now)
    fresh = _domria(db, 4, start=34700000, last_seen=datetime(2026, 10, 8, 1, 0))
    _domria(db, 30, start=34800000, last_seen=datetime(2026, 9, 1))
    net = FakeNet({**{f"domria:{34700000 + i}": (200, ria_page(34700000 + i, archived=i < 2))
                      for i in range(4)},
                   **{f"domria:{34800000 + i}": (200, ria_page(34800000 + i))
                      for i in range(30)}})
    stats = verify.verify_batch(http=net)
    assert stats["tiers"]["dom.ria.com"].get("canary") == 4
    assert fuse.held_sources() == {"domria"}
    assert all(get(db, i).is_active for i in fresh)


def test_shipped_config_follows_the_owners_decision():
    """Рішення власника 08.10 (D55): 20% — лише серед випадкових і контрольних
    перевірок (tiered), а банер на БУДЬ-ЯКОМУ відомо живому — зупинка."""
    cfg = configfiles.load("liveness").fuse
    assert cfg.mode == "tiered" and cfg.canary_trip_min == 1


def test_one_canary_reporting_removed_holds_the_source(db, tmp_path, monkeypatch):
    """Один відомо живий з банером — уже зупинка (у tiered)."""
    from realty.liveness import fuse, queue

    _mode(tmp_path, monkeypatch, "tiered")
    now = datetime(2026, 10, 8, 3, 0)
    monkeypatch.setattr(queue, "_now", lambda: now)
    fresh = _domria(db, 4, start=34710000, last_seen=datetime(2026, 10, 8, 1, 0))
    _domria(db, 30, start=34810000, last_seen=datetime(2026, 9, 1))
    net = FakeNet({**{f"domria:{34710000 + i}": (200, ria_page(34710000 + i, archived=i == 0))
                      for i in range(4)},
                   **{f"domria:{34810000 + i}": (200, ria_page(34810000 + i))
                      for i in range(30)}})
    verify.verify_batch(http=net)
    assert fuse.held_sources() == {"domria"}
    assert all(get(db, i).is_active for i in fresh)


def test_fuse_alerts_through_the_watchdog_until_released(db):
    from realty import watchdog
    from realty.liveness import fuse

    _domria(db, 25, start=34900000)
    net = FakeNet({f"domria:{34900000 + i}": (200, ria_page(34900000 + i, archived=True))
                   for i in range(25)})
    # Крок циклу ярусами: «знято» на 20 випадкових з 20 — тримати (tiered, D56).
    verify.verify_batch(http=net)
    alerts = {a.key: a for a in watchdog.check_liveness(datetime(2026, 10, 8, 12, 0))}
    assert "liveness-fuse:domria" in alerts
    assert "/status" in alerts["liveness-fuse:domria"].text
    fuse.clear("domria", by="test")
    keys = {a.key for a in watchdog.check_liveness(datetime(2026, 10, 8, 12, 0))}
    assert "liveness-fuse:domria" not in keys


# --- Рецензія E8 (D52) --------------------------------------------------------------------------


def test_release_lets_the_held_removals_through(db):
    """25 знятих DOM.RIA тримають джерело; власник відпустив → наступний крок циклу
    (ярус held) знімає їх із подіями ria_archive і запобіжник знову НЕ спрацьовує.
    На коді до виправлення ярус held (за побудовою ~100% «знято») тримав джерело щоразу."""
    from realty.liveness import fuse

    ids = _domria(db, 25, start=35200000)
    net = FakeNet({f"domria:{35200000 + i}": (200, ria_page(35200000 + i, archived=True))
                   for i in range(25)})
    first = verify.verify_batch(http=net)                       # крок циклу ярусами
    assert first["tiers"]["dom.ria.com"].get("random") == 20, first["tiers"]
    assert fuse.held_sources() == {"domria"}
    assert all(get(db, i).is_active for i in ids)
    assert fuse.clear("domria", by="owner:/status") is True
    stats = verify.verify_batch(http=net)                       # крок циклу ярусами
    assert stats["tiers"]["dom.ria.com"].get("held") == 25, stats["tiers"]
    assert stats["trips"] == [] and fuse.held_sources() == set()
    assert sum(not get(db, i).is_active for i in ids) == 25
    assert {e.reason for i in ids for e in events(db, i)} == {"ria_archive"}


def test_fuse_counts_by_the_host_whose_signature_fired(db):
    """Підпис DOM.RIA «зламався» (усі сторінки — архів із банером): джерело domria
    спрацьовує, і ключі DOM.RIA, що мають лише рядки LUN, теж лишаються актуальними —
    вони не розчиняються в пулі lun серед живих копій OLX. На коді до виправлення ці 5
    знімались."""
    from realty.liveness import fuse

    resp, ids, lun_only = {}, [], []
    for i in range(25):
        rid = 35300000 + i
        ids.append(add(db, ria_url(rid), source="domria", external_id=str(rid)))
        resp[f"domria:{rid}"] = (200, ria_page(rid, archived=True))
    for i in range(5):
        rid = 35400000 + i
        lid = add(db, ria_url(rid), source="lun", external_id=f"lr{i}")
        lun_only.append(lid)
        resp[f"domria:{rid}"] = (200, ria_page(rid, archived=True))
    for i in range(30):
        tok = f"10Ok{i:02d}"
        ids.append(add(db, olx_url(tok), source="lun", external_id=f"lo{i}"))
        resp[f"olx:{tok}"] = 200
    stats = verify.verify_batch(ids=ids + lun_only, http=FakeNet(resp))
    assert all(get(db, lid).is_active for lid in lun_only)
    assert "domria" in fuse.held_sources()
    assert {t["source"]: t["scope"] for t in stats["trips"]}.get("domria") in ("source", "host")
    # Утриманий сайт: сліпий обхід його ключів (і лише-LUN теж) не витрачає запитів.
    from realty.liveness import policy as pol, queue

    with db() as s:
        plan = queue.plan_run(s, pol.load(), held_sources=fuse.held_sources())
    assert not [i for i in plan.items if i.host == "dom.ria.com" and i.tier == "sweep"]


def test_small_runs_add_up_within_the_window(db, tmp_path, monkeypatch):
    """Перевірки по одній квартирі (як при відкритті) набирають n ≥ min_checked разом із
    перевірками за вікно fuse.window_hours: зламаний підпис знімає не більше
    min_checked − 1, далі джерело тримається. На коді до виправлення — усі 25."""
    from realty.liveness import fuse

    _mode(tmp_path, monkeypatch, "literal")
    ids = _domria(db, 25, start=35500000)
    net = FakeNet({f"domria:{35500000 + i}": (200, ria_page(35500000 + i, archived=True))
                   for i in range(25)})
    for lid in ids:
        verify.verify_batch(ids=[lid], http=net, reason="opened")
    removed = sum(not get(db, i).is_active for i in ids)
    assert removed <= 19, removed
    assert "domria" in fuse.held_sources()


def test_fuse_history_keeps_every_trip_and_release(db):
    """Повторне спрацювання переписує поточний стан, але історія лишається:
    trip → clear → trip — три рядки ops.liveness_fuse_log, у знятті — хто відпустив."""
    from realty.liveness import fuse

    t = fuse.Trip("olx", "share", 30, 7, 0.2333, ["https://www.olx.ua/x"])
    assert fuse.trip([t], run_id=1, mode="literal") == ["olx"]
    assert fuse.clear("olx", by="owner:/status") is True
    assert fuse.trip([t], run_id=2, mode="literal") == ["olx"]
    log = fuse.history(10)
    assert [(r["action"], r["run_id"]) for r in log] == [("trip", 2), ("clear", 1), ("trip", 1)]
    assert log[1]["by"] == "owner:/status"
