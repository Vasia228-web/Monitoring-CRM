"""Перевірка актуальності через ПУБЛІЧНИЙ шлях verify_batch із фальшивою мережею (E8, D52).

Кожен тест тут падає на коді до E8 саме на поведінці (інтеграція, прогалина 12):
  * сторінка DOM.RIA з банером «Оголошення видалено…» — старий HEAD 200 → «живе»;
  * один 404 — старий код знімав одразу;
  * вердикт ключа «сайт:id» — старий перевіряв і знімав лише рядок із черги,
    копії LUN лишались активними (446 змішаних ключів OLX, Етап 0);
  * повернення — без події й без журналу причин;
  * повторний 404 з перевіркою існування й ремонт посилання — не існували.
Фальшива мережа відповідає й на `check` (новий шлях), і на `probe` (старий).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401 — фікстури db, clean_fuse
    FakeNet, add, at, checks, clean_fuse, db, events, get, olx_url, ria_page, ria_url,
    rieltor_url,
)

from realty import verify  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")


def _run(net, ids, reason="sweep"):
    return verify.verify_batch(ids=ids, http=net, reason=reason)


def test_ria_banner_page_delists_and_records_the_source_date(db):
    """Старий HEAD бачив 200 і лишав зняте активним (Етап 0: 18 з 18)."""
    lid = add(db, ria_url(34000001), source="domria", external_id="34000001")
    page = ria_page(34000001, archived=True, deleted_ts=1791241205)
    net = FakeNet({"domria:34000001": (200, page)})
    stats = _run(net, [lid])
    row = get(db, lid)
    assert row.is_active is False and row.delisted_at is not None
    assert row.source_removed_at == datetime(2026, 10, 5, 23, 0, 5)
    assert row.last_alive_at is None, "межу інтервалу для строку продажу не чіпаємо"
    assert stats["delisted"] == 1
    assert net.calls and net.calls[0][1] == "GET", "DOM.RIA — GET сторінки, не HEAD"
    ev = events(db, lid)
    assert [(e.kind, e.reason) for e in ev] == [("removed", "ria_archive")]
    assert ev[0].check_event_id == checks(db, lid)[-1].id
    assert ev[0].evidence["ria"]["banner"] is True
    assert checks(db, lid)[-1].signature == "ria_archive"


def test_live_ria_page_is_alive(db):
    lid = add(db, ria_url(34000002), source="domria", external_id="34000002")
    _run(FakeNet({"domria:34000002": (200, ria_page(34000002))}), [lid])
    row = get(db, lid)
    assert row.is_active is True and row.last_alive_at is not None
    assert checks(db, lid)[-1].signature == "alive"


def test_single_404_does_not_delist(db):
    lid = add(db, olx_url("10abcD"), source="olx", external_id="10abcD")
    stats = _run(FakeNet({"olx:10abcD": 404}), [lid])
    row = get(db, lid)
    assert row.is_active is True and row.delisted_at is None
    assert row.last_checked is None, "404 — не зрозуміла перевірка"
    c = checks(db, lid)[-1]
    assert (c.code, c.alive, c.signature) == (404, None, "not_found")
    assert stats["delisted"] == 0 and events(db) == []


def test_removal_propagates_to_every_row_of_the_site_key(db):
    """OLX і дві копії LUN — один ключ, один запит, три зняття з подіями."""
    own = add(db, olx_url("10Prop"), source="olx", external_id="10Prop")
    lun1 = add(db, olx_url("10Prop", "kopiya"), source="lun", external_id="l1")
    lun2 = add(db, olx_url("10Prop", "insha-nazva"), source="lun", external_id="l2")
    net = FakeNet({"olx:10Prop": 410})
    stats = _run(net, [own])
    assert [get(db, lid).is_active for lid in (own, lun1, lun2)] == [False, False, False]
    assert len(net.calls) == 1, "один запит на ключ"
    for lid in (own, lun1, lun2):
        assert [e.kind for e in events(db, lid)] == ["removed"]
        assert len(checks(db, lid)) == 1
    assert stats["delisted"] == 3


def test_alive_verdict_returns_removed_rows_of_the_key_with_event(db):
    gone = datetime(2026, 9, 30, 12, 0, 0, 123456)
    lun = add(db, olx_url("10Back", "kopiya"), source="lun", external_id="lb",
              is_active=False, delisted_at=gone, check_failures=2,
              last_checked=datetime(2026, 9, 30, 12, 0, 0, 123456))
    own = add(db, olx_url("10Back"), source="olx", external_id="10Back")
    manual = add(db, olx_url("10Back", "ruchna"), source="lun", external_id="lm",
                 is_active=False, delisted_at=gone, manual_active=False)
    stats = _run(FakeNet({"olx:10Back": 200}), [own])
    row = get(db, lun)
    assert row.is_active is True and row.delisted_at is None
    assert row.last_alive_at is not None
    ev = events(db, lun)
    assert [e.kind for e in ev] == ["returned"]
    # Попередні значення — точно, до мікросекунди (повернення можна відкотити й
    # зіставити з перевіркою зняття), разом зі станом рядка до повернення.
    assert datetime.fromisoformat(ev[0].evidence["prev_delisted_at"]) == gone
    assert ev[0].evidence["prev_is_active"] is False
    assert ev[0].evidence["prev_check_failures"] == 2
    assert datetime.fromisoformat(ev[0].evidence["prev_last_checked"]) == gone
    assert ev[0].evidence["prev_removal_reason"] == "legacy", "зняте старим кодом — без події"
    kept = get(db, manual)
    assert kept.manual_active is False and kept.is_active is False, "ручне рішення не чіпаємо"
    assert stats["restored"] == 1


def test_unknown_answer_counts_a_failure_and_never_dates_the_check(db):
    lid = add(db, rieltor_url(13000001), source="lun", external_id="r1")
    _run(FakeNet({"rieltor:13000001": 403}), [lid])
    row = get(db, lid)
    assert row.is_active is True and row.check_failures == 1
    assert row.last_checked is None and row.last_attempt is not None
    assert checks(db, lid)[-1].signature == "blocked"


def _snapshot(ids, taken_at, complete=True):
    from realty import snapshot

    snap = snapshot.Snapshot(source="domria", taken_at=taken_at, ids=set(ids))
    snap.complete = complete               # поле E8; на старому коді — просто атрибут
    snapshot.save(snap)


def _at_time(monkeypatch, moment):
    try:
        from realty.liveness import queue
    except ImportError:                    # код до E8: тест падає нижче, на поведінці
        return
    monkeypatch.setattr(queue, "_now", lambda: moment)


def test_repeated_404_removes_only_after_count_interval_and_existence_check(db, monkeypatch):
    """404 трьома прогонами з інтервалом ≥24 год І оголошення немає ні в переліку
    DOM.RIA, ні за карткою API — лише тоді «знято» (repeat_404, рішення власника 1)."""
    lid = add(db, ria_url(34000003), source="domria", external_id="34000003")
    card = json.dumps({"realty_id": 34000003, "is_delete": 1, "deleted_at": "2026-10-09 10:00"})
    net = FakeNet({"domria:34000003": 404,
                   "https://dom.ria.com/realty/data/34000003?lang_id=4": (200, card)})
    for hours in (0, 2, 24):                         # другий — раніше за інтервал: не рахується
        _at_time(monkeypatch, at(hours))
        _snapshot({"1"} | {str(i) for i in range(30)}, at(hours))
        _run(net, [lid])
        assert get(db, lid).is_active is True, f"знято після {hours} год — зарано"
    _at_time(monkeypatch, at(48))
    _snapshot({str(i) for i in range(30)}, at(47))
    stats = _run(net, [lid])
    row = get(db, lid)
    assert row.is_active is False, stats
    ev = events(db, lid)
    assert [(e.kind, e.reason) for e in ev] == [("removed", "repeat_404")]
    assert len(ev[0].evidence["streak"]) == 3
    assert {x["state"] for x in ev[0].evidence["existence"]} == {"absent"}


def test_repeated_404_is_kept_while_existence_cannot_be_checked(db, monkeypatch):
    """Перелік застарів, картка API не відповіла — «не знайдено», не знято."""
    lid = add(db, ria_url(34000004), source="domria", external_id="34000004")
    net = FakeNet({"domria:34000004": 404,
                   "https://dom.ria.com/realty/data/34000004?lang_id=4": 0})
    for hours in (0, 24, 48, 72):
        _at_time(monkeypatch, at(hours))
        _snapshot({str(i) for i in range(30)}, at(hours) - timedelta(hours=30))
        _run(net, [lid])
    assert get(db, lid).is_active is True
    assert events(db, lid) == []


def test_repeated_404_with_ad_still_listed_repairs_the_link_instead(db, monkeypatch):
    """Оголошення є в переліку пошуку, а картка дає нову адресу, яка жива →
    probe_url + подія url_repaired, «знято» не ставимо; original_url — як від джерела."""
    old = ria_url(34000005, "realty-prodaja-kvartira-ivano-frankovsk-stara")
    lid = add(db, old, source="domria", external_id="34000005")
    new = "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-nova-34000005.html"
    card = json.dumps({"realty_id": 34000005,
                       "beautiful_url": "realty-prodaja-kvartira-ivano-frankovsk-nova-34000005.html"})
    net = FakeNet({old: 404, new: (200, ria_page(34000005)),
                   "https://dom.ria.com/realty/data/34000005?lang_id=4": (200, card)})
    for hours in (0, 24, 48):
        _at_time(monkeypatch, at(hours))
        _snapshot({"34000005"} | {str(i) for i in range(30)}, at(hours))
        _run(net, [lid])
    row = get(db, lid)
    assert row.is_active is True and row.probe_url == new
    assert row.original_url == old, "адресу джерела не переписуємо (інтеграція, конфлікт 21)"
    ev = events(db, lid)
    assert [(e.kind, e.reason) for e in ev] == [("url_repaired", "ria_api")]
    assert ev[0].evidence["new_url"].endswith("-34000005.html")
    # Наступна перевірка питає вже полагоджену адресу.
    _at_time(monkeypatch, at(72))
    net.calls.clear()
    _run(net, [lid])
    assert net.calls[0][0] == new


def test_repair_survives_the_next_collection_of_the_old_url(db, monkeypatch):
    """Збір, що знову приносить стару адресу, ремонт не скасовує (конфлікт 21)."""
    from realty import pipeline

    old = ria_url(34000006, "realty-prodaja-kvartira-ivano-frankovsk-stara")
    lid = add(db, old, source="domria", external_id="34000006",
              probe_url="https://dom.ria.com/uk/realty-prodaja-kvartira-nova-34000006.html")
    with db() as s:
        pipeline.Pipeline._upsert(s, {"source": "domria", "external_id": "34000006",
                                      "original_url": old, "price": 1.0, "currency": "USD",
                                      "probe_url": None, "absent_since": datetime(2020, 1, 1)})
        s.commit()
    row = get(db, lid)
    assert row.probe_url.endswith("nova-34000006.html")
    assert row.absent_since is None, "поля перевірки збір не задає"


def test_liveness_never_writes_last_seen(db, monkeypatch):
    """D43: last_seen ставить лише pipeline._upsert — ні зняття, ні повернення, ні ремонт."""
    seen = datetime(2026, 9, 1, 8, 0)
    a = add(db, olx_url("10Seen"), source="olx", external_id="a", last_seen=seen)
    b = add(db, olx_url("10Gone"), source="olx", external_id="b", last_seen=seen,
            is_active=False, delisted_at=datetime(2026, 9, 20))
    c = add(db, ria_url(34000007), source="domria", external_id="c", last_seen=seen)
    _run(FakeNet({"olx:10Seen": 410, "olx:10Gone": 200,
                  "domria:34000007": (200, ria_page(34000007, archived=True))}), [a, b, c])
    for lid in (a, b, c):
        assert get(db, lid).last_seen == seen


# --- Рецензія E8 (D52): шляхи до хибного «знято» ----------------------------------------------


def test_olx_404_streak_outside_the_capped_feed_never_removes(db, monkeypatch):
    """OLX: живе оголошення поза 25 сторінками стрічки (не бачили 10 днів), 404 —
    загальна сторінка помилки — чотири рази з інтервалом 24 год → лишається актуальним.
    На коді до виправлення: «немає в стрічці» = «немає» → знято як repeat_404."""
    lid = add(db, olx_url("22359x"), source="olx", external_id="22359x",
              last_seen=at(0) - timedelta(days=10))
    net = FakeNet({"olx:22359x": 404})
    for hours in (0, 24, 48, 72):
        _at_time(monkeypatch, at(hours))
        _run(net, [lid])
    row = get(db, lid)
    assert row.is_active is True and row.delisted_at is None
    assert events(db, lid) == []
    assert [c.signature for c in checks(db, lid)] == ["not_found"] * 4
    assert len(net.calls) == 4, "стрічка — не запит; olx_id_url вимкнено"


def test_rieltor_404_streak_is_not_removed_because_lun_dropped_its_copy(db, monkeypatch):
    """rieltor: копії LUN немає в повному переліку LUN, rieltor тричі 404 → актуальне.
    Агрегатор, що випустив копію, — не доказ, що rieltor зняв (у rieltor є явний 410)."""
    lid = add(db, rieltor_url(13000777), source="lun", external_id="lun777",
              last_seen=at(0) - timedelta(days=10))
    net = FakeNet({"rieltor:13000777": 404})
    from realty import snapshot

    for hours in (0, 24, 48):
        _at_time(monkeypatch, at(hours))
        snap = snapshot.Snapshot(source="lun", taken_at=at(hours),
                                 ids={str(i) for i in range(30)})
        snap.complete = True
        snapshot.save(snap)
        _run(net, [lid])
    assert get(db, lid).is_active is True
    assert events(db, lid) == []


@pytest.mark.parametrize("card", [404, 410, "other_id"])
def test_ria_api_card_without_proof_is_not_absence(db, monkeypatch, card):
    """DOM.RIA: сторінка 404 тричі, немає в свіжому переліку, а картка API — 404/410
    (такого підпису Етап 0 не бачив: 100 зі 100 — 200) чи картка ІНШОГО оголошення з
    is_delete → «не відповіла», не знято. На коді до виправлення картка 404 знімала."""
    rid = 34000999
    lid = add(db, ria_url(rid), source="domria", external_id=str(rid))
    api = f"https://dom.ria.com/realty/data/{rid}?lang_id=4"
    answer = (200, json.dumps({"realty_id": 1, "is_delete": 1})) if card == "other_id" else card
    net = FakeNet({f"domria:{rid}": 404, api: answer})
    for hours in (0, 24, 48):
        _at_time(monkeypatch, at(hours))
        _snapshot({str(i) for i in range(30)}, at(hours))
        _run(net, [lid])
    assert get(db, lid).is_active is True
    assert events(db, lid) == []
    assert any(u == api for u, _m, _d in net.calls), "картку питали"


def test_older_removal_never_overrides_a_newer_alive(db):
    """Мережева фаза циклу (до 25 хв) «зняла» о t1, а процес перевірки при відкритті
    вже застосував «живе» о t2 > t1 → «знято» не застосовується (лише журнал)."""
    from realty.liveness import apply as la, engine, policy as pol, queue
    from realty.liveness.signatures import verdict_from_code

    lid = add(db, olx_url("10Race"), source="olx", external_id="10Race")
    cfg = pol.load()
    with db() as s:
        item = queue.items_for_rows(s, cfg, [lid], "sweep")[0]
    t1, t2 = at(0), at(0.3)
    la.apply_outcomes([engine.Outcome(item, verdict_from_code(200), t2)], cfg=cfg,
                      scope=verify.session_scope)
    rep = la.apply_outcomes([engine.Outcome(item, verdict_from_code(410), t1)], cfg=cfg,
                            scope=verify.session_scope)
    row = get(db, lid)
    assert row.is_active is True and row.delisted_at is None
    assert row.last_checked == t2 and row.last_attempt == t2, "дати не йдуть назад"
    assert events(db, lid) == []
    assert [c.signature for c in checks(db, lid)] == ["alive", "status_410"], "журнал — так"
    assert rep.stale == 1
    # І навпаки: старе «живе» не повертає рядок, знятий пізніше.
    la.apply_outcomes([engine.Outcome(item, verdict_from_code(410), at(1))], cfg=cfg,
                      scope=verify.session_scope)
    la.apply_outcomes([engine.Outcome(item, verdict_from_code(200), at(0.5))], cfg=cfg,
                      scope=verify.session_scope)
    row = get(db, lid)
    assert row.is_active is False and row.delisted_at == at(1)
    assert [e.kind for e in events(db, lid)] == ["removed"]


def test_removal_event_keeps_the_previous_state_and_return_names_the_removal(db):
    """Подія removed несе попередні is_active, last_checked, check_failures; наступне
    повернення — яке саме зняття воно скасувало (id, причина, час)."""
    lid = add(db, olx_url("10Prev"), source="olx", external_id="10Prev", check_failures=1,
              last_checked=datetime(2026, 10, 1, 1, 2, 3, 456789))
    _run(FakeNet({"olx:10Prev": 410}), [lid])
    removed = events(db, lid)[0]
    assert removed.evidence["prev_is_active"] is True
    assert removed.evidence["prev_check_failures"] == 1
    assert datetime.fromisoformat(removed.evidence["prev_last_checked"]) == \
        datetime(2026, 10, 1, 1, 2, 3, 456789)
    _run(FakeNet({"olx:10Prev": 200}), [lid])
    back = events(db, lid)[1]
    assert back.kind == "returned"
    assert back.evidence["prev_removal_event_id"] == removed.id
    assert back.evidence["prev_removal_reason"] == "status_410"
    assert datetime.fromisoformat(back.evidence["prev_removal_at"]) == removed.at


def test_collection_never_returns_or_removes_a_listing(db):
    """Запис джерела з is_active / delisted_at / датами перевірки стану не міняє: знімає й
    повертає лише перевірка (з подією). На коді до виправлення збір повертав знятий
    рядок без жодної події."""
    from realty import pipeline

    gone = datetime(2026, 9, 30, 12, 0)
    lid = add(db, olx_url("10Pipe"), source="olx", external_id="10Pipe", is_active=False,
              delisted_at=gone, check_failures=3)
    with db() as s:
        pipeline.Pipeline._upsert(s, {"source": "olx", "external_id": "10Pipe",
                                      "original_url": olx_url("10Pipe"), "price": 1.0,
                                      "currency": "USD", "is_active": True, "delisted_at": None,
                                      "check_failures": 0, "last_checked": datetime(2030, 1, 1),
                                      "last_alive_at": datetime(2030, 1, 1)})
        s.commit()
    row = get(db, lid)
    assert row.is_active is False and row.delisted_at == gone and row.check_failures == 3
    assert row.last_checked is None and row.last_alive_at is None
    assert events(db, lid) == []


# --- Рецензія E8 (D52): ручний запуск не йде паралельно з циклом ------------------------------


def test_manual_verify_refuses_during_a_cycle_or_with_collector_off(tmp_path, monkeypatch):
    """`cli.py verify` / `liveness run` вручну під час циклу подвоїв би темп запитів до
    кожного сайту (D50), а з COLLECTOR_OFF — бив би сайти з машини, де збір вимкнено.
    Відмова (код 2), якщо не --force; запуск кроком циклу (змінна диригента) — як був."""
    import argparse
    import signal

    from realty import netguard, runner, verify as vmod

    # cli.py на імпорті вмикає заборону мережі «як дочірній процес» (журнал у файл
    # батька) — у процесі самих тестів це зробив conftest, другий раз не треба.
    monkeypatch.setattr(netguard, "install_from_env", lambda: False)
    import cli

    calls = []

    def fake(**kw):
        calls.append(kw)
        return {"requests": 0, "checked": 0, "alive": 0, "delisted": 0, "restored": 0,
                "unknown": 0, "by_host": {}, "blocked_sources": [], "by_source": {}}

    monkeypatch.setattr(vmod, "verify_batch", fake)
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)
    monkeypatch.delenv("REALTY_CYCLE_STEP", raising=False)
    flag = tmp_path / "COLLECTOR_OFF"
    flag.write_text("базу перенесено", encoding="utf-8")
    monkeypatch.setattr(runner, "DISABLED_FLAG", flag)
    monkeypatch.setattr(runner, "lock_busy", lambda *a, **k: None)
    args = argparse.Namespace(limit=None, sources=None, force=False)
    assert cli.cmd_verify(args) == 2 and calls == []
    flag.unlink()
    monkeypatch.setattr(runner, "lock_busy", lambda *a, **k: {"pid": 4242})
    assert cli.cmd_verify(args) == 2 and calls == []
    args.force = True
    assert cli.cmd_verify(args) == 0 and calls[-1]["kind"] == "manual"
    args.force = False
    monkeypatch.setenv("REALTY_CYCLE_STEP", "перевірка актуальності")
    assert cli.cmd_verify(args) == 0 and calls[-1]["kind"] is None


def test_cycle_steps_carry_the_conductor_variable():
    import sys as _sys

    from realty import runner

    code = ("import os, sys; sys.exit(0 if os.environ.get('REALTY_CYCLE_STEP') == "
            "'перевірка актуальності' else 3)")
    step = runner.Step("перевірка актуальності", [_sys.executable, "-c", code], 30)
    result, _pid = runner.run_step(step, 30)
    assert result.status == "ok", result
