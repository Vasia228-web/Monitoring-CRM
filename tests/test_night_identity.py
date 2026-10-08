"""Дозбір identity у нічній смузі (рецензія E9, D53): один потік на сайт, без чужих хостів.

До рецензії дозбір у смузі йшов СВОЇМ фетчером і своїм процесом збору:
  * прохід стрічки LUN (`cli.py scrape --sources lun --mode full`) ходив і на сторінки
    деталей — це rieltor.ua, olx.ua, dom.ria.com, — з паузою 1,5 с, поки смуги цих хостів
    питали їх у своєму темпі (rieltor ≥ 3,0 с): два потоки на сайт;
  * картки DOM.RIA — власний обмежувач: перший запит одразу після останньої перевірки
    смуги, 401/403/429 смуга не бачила (і позначала картку «пробували»);
  * картки DOM.RIA писались у транзакції, відкритій на весь пакет із 25 запитів.
Тут — справжні `Lane`, `identity_job`, `identity_backfill`, `Pipeline` і джерело LUN;
мережу підмінено на рівні `Fetcher._fetch` (журнал запитів), час — віртуальний.
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import FakeNet, add, clean_fuse, db, ria_page, ria_url  # noqa: E402,F401
from night_kit import TimedNet, clean_night, local_epoch, utc_of  # noqa: E402,F401

from realty.liveness import policy, queue  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse", "clean_night")
T0 = local_epoch(2026, 10, 9, 1, 10)
SEEN = datetime(2026, 10, 1)


class RelClock:
    """Віртуальний час смуги й модуль `time` для обмежувачів і дедлайнів водночас.

    monotonic — від нуля, time — epoch: обмежувач фетчера доспить «залишок паузи» в
    циклі, а на величинах ~1,8e9 крок float більший за такий залишок (FakeClock для
    фетчера зациклився б)."""

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.t = 0.0

    def time(self) -> float:
        return self.t0 + self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        # +1 нс: обмежувач фетчера доспить і залишок 1e-16, а такий крок float губить.
        self.t += max(0.0, s) + 1e-9

    perf_counter = monotonic


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code
        self.encoding = "utf-8"

    def raise_for_status(self):
        import httpx

        req = httpx.Request("GET", "https://example.invalid/")
        raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=req,
                                    response=httpx.Response(self.status_code, request=req))


class _NoCache:
    def __init__(self, *a, **kw):
        pass

    def get(self, key):
        return None

    def set(self, key, value):
        pass


@pytest.fixture
def wire(db, tmp_path, monkeypatch):
    """Справжні фетчери з журналом запитів (хост, віртуальний час) замість мережі."""
    import realty.fetcher as fetcher
    import realty.identity_backfill as bf
    import realty.night.lane as lane_mod

    clock = RelClock(T0)
    for mod in (fetcher, bf, lane_mod):
        monkeypatch.setattr(mod, "time", clock)
    monkeypatch.setattr(fetcher, "DiskCache", _NoCache)
    monkeypatch.setattr(bf, "SessionLocal", db)
    monkeypatch.setattr(bf, "init_db", lambda: None)
    monkeypatch.setattr(bf, "STATE_PATH", tmp_path / "identity_backfill.json")
    monkeypatch.setattr(bf.ops, "beat", lambda *a, **kw: None)
    log: list[tuple[str, float, str]] = []
    pages: dict = {}

    def fake_fetch(self, method, url, params=None, headers=None, body=True):
        log.append((urlsplit(url).netloc, clock.monotonic(), url))
        answer = pages.get("answer")
        code, text = answer(url) if answer else (200, "")
        return _Resp(code), text

    monkeypatch.setattr(fetcher.Fetcher, "_fetch", fake_fetch)
    return clock, log, pages


def _lane(spec_host, items, clock, *, identity, net=None, out=None):
    from realty.night import codec
    from realty.night.lane import Lane, identity_job

    cfg = policy.load()
    spec = {"host": spec_host, "family": cfg.hosts[spec_host].family,
            "pace": policy.pace(cfg, spec_host, "night"), "stop_at": T0 + 3600,
            "held_path": None, "identity": identity, "max_consecutive_blocks": 5,
            "block_share": 0.10, "block_min_requests": 20,
            "items": [codec.item_to_json(i) for i in items]}
    net = net or TimedNet(FakeNet(default=200))
    return Lane(spec, out or io.StringIO(), fetcher=net.for_lane(spec_host, clock), cfg=cfg,
                clock=clock, now_fn=lambda: utc_of(clock.time()), identity_fn=identity_job,
                snapshots={}), net


def _gaps(times):
    times = sorted(times)
    return [b - a for a, b in zip(times, times[1:])]


# --- LUN: лише стрічка, лише lun.ua ------------------------------------------------------


def _lun_page(objs) -> str:
    payload = "".join(json.dumps(o, ensure_ascii=False) for o in objs) or "[]"
    return f'<script>self.__next_f.push([1,{json.dumps(payload, ensure_ascii=False)}])</script>'


def _lun_obj(i, url):
    # Опису немає — стан «невідомо»: старий прохід ішов по нього на сторінку деталей.
    return {"id": 9000 + i, "price": 50000 + i, "currency": "usd", "urlRaw": url,
            "geo": "вулиця Галицька, Івано-Франківськ", "location": [24.71, 48.92],
            "roomCount": 1, "areaTotal": 40, "header": f"1-кімнатна {i}"}


def _inline_scrape(monkeypatch, db):
    """`cli.py scrape …` кроку дозбору — у процесі тесту (той самий cmd_scrape і Pipeline)."""
    import cli
    import realty.identity_backfill as bf
    import realty.pipeline as pipeline
    from realty.config import SOURCES
    from realty.runner import StepResult

    from night_kit import scope_of

    for name in list(SOURCES):                         # cmd_scrape --min-delay міняє SOURCES
        monkeypatch.setitem(SOURCES, name, SOURCES[name])
    monkeypatch.setattr(pipeline, "session_scope", scope_of(db))
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    steps = []

    def run_step(step, budget):
        argv = step.argv[step.argv.index("scrape") + 1:]
        steps.append(argv)
        ns = argparse.Namespace(pages=None, sources=None, no_llm=False, trigger="cli",
                                mode="fresh", no_detail=False, min_delay=None)
        it = iter(argv)
        for a in it:
            key = a.lstrip("-").replace("-", "_")
            if key in ("no_llm", "no_detail"):
                setattr(ns, key, True)
            else:
                setattr(ns, key, next(it))
        ns.min_delay = float(ns.min_delay) if ns.min_delay else None
        cli.cmd_scrape(ns)
        return StepResult(step.name, "ok", 0.0, 0), None

    monkeypatch.setattr(bf, "run_step", run_step)
    monkeypatch.setattr(bf, "FULL_PASS_MIN", 1)
    return steps


def test_lun_identity_pass_in_the_lane_asks_only_lun_ua_at_the_night_pace(wire, db, monkeypatch):
    """Справжній Lane.finish → identity_job('lun') → прохід стрічки: кожен запит — до
    lun.ua, пауза старт-до-старту ≥ нічного темпу lun.ua. Записи стрічки ведуть на
    rieltor.ua, olx.ua і dom.ria.com, і стан у них «невідомо» — старий прохід ішов на їхні
    сторінки деталей своїм темпом 1,5 с, поки смуги цих хостів питали їх у своєму.

    З E11 (D60) прохід — спільний нічний прохід стрічки (night/feed.py), а не звичайний
    збір: новий запис стрічки (9001) НЕ додається, наявний рядок (9000) отримує identity
    і докази лише туди, де порожньо (до E11 тест перевіряв `scrape --no-detail` і
    вставку 9001 — поведінку свідомо змінено, D60)."""
    import realty.db as rdb

    from night_kit import scope_of

    clock, log, pages = wire
    monkeypatch.setattr(rdb, "session_scope", scope_of(db))
    for i in range(60):
        add(db, f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{7700000 + i}/",
            source="lun", external_id=f"old{i}")              # активні LUN без identity
    known = add(db, "https://rieltor.ua/ivano-frankovsk/flats-sale/view/11868893/",
                source="lun", external_id="9000")
    objs = [_lun_obj(0, "https://rieltor.ua/ivano-frankovsk/flats-sale/view/11868893/"),
            _lun_obj(1, "https://www.olx.ua/d/uk/obyavlenie/kvartyra-IDabc12.html"),
            _lun_obj(2, "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-"
                        "34375047.html")]

    def answer(url):
        if "lun.ua" in url:
            return 200, _lun_page(objs if "page=" not in url else [])
        return 200, "<html><body>сторінка деталей</body></html>"

    pages["answer"] = answer
    steps = _inline_scrape(monkeypatch, db)
    lane, _net = _lane("lun.ua", [], clock, identity="lun")
    summary = lane.run()
    assert not steps, "звичайного збору (`cli.py scrape`) уночі більше немає"
    assert {host for host, _t, _u in log} == {"lun.ua"}, log
    pace = policy.pace(policy.load(), "lun.ua", "night")
    assert len(log) >= 2 and min(_gaps([t for _h, t, _u in log])) >= pace - 1e-6
    assert summary["stopped"] is None
    from realty.models import Listing

    with db() as s:
        assert s.scalar(select(func.count()).select_from(Listing)
                        .where(Listing.external_id == "9001")) == 0     # не збір
        row = s.get(Listing, known)
        assert row.identity and row.seller_evidence["lun_checked_at"]
        assert row.place_raw["lun_geo_checked_at"]
        assert row.last_seen == SEEN and row.price_usd == 50_000.0        # не чіпали


def test_full_pass_reports_what_the_feed_pass_wrote_and_keeps_flombu_state(db, tmp_path,
                                                                            monkeypatch):
    """Прохід стрічки — звичайний збір: скільки рядків і подій ціни він дописав, іде у
    звіт (нічна звірка); стан cooldown — лише свого джерела: паралельний прохід flombu в
    іншій смузі, що записав свій стан посеред проходу LUN, не затирається."""
    import realty.identity_backfill as bf
    from realty.models import Listing, PriceEvent
    from realty.runner import StepResult

    monkeypatch.setattr(bf, "SessionLocal", db)
    monkeypatch.setattr(bf, "STATE_PATH", tmp_path / "state.json")
    for i in range(60):
        add(db, f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{7800000 + i}/",
            source="lun", external_id=f"l{i}")
    argv = []

    def fake_step(step, budget):
        argv.append(step.argv)
        bf._save_source_state("flombu", {"at": "2026-10-09T00:00:00+00:00", "gain": 1})
        with db() as s:
            for i in range(2):
                s.add(Listing(source="lun", external_id=f"new{i}", original_url=f"https://n/{i}",
                              is_active=True, first_seen=SEEN, last_seen=SEEN))
            s.flush()
            lid = s.scalar(select(Listing.id).where(Listing.external_id == "new0"))
            s.add(PriceEvent(listing_id=lid, source="lun", price=1.0, currency="USD",
                             price_usd=1.0, observed_at=SEEN))
            s.commit()
        return StepResult(step.name, "ok", 1.0, 0), None

    monkeypatch.setattr(bf, "run_step", fake_step)

    class Gate:
        pace = 1.6
        waited = 0

        def stopped(self):
            return False

        def wait(self):
            self.waited += 1

    gate = Gate()
    rep = {"done": {}, "left": {}, "errors": 0}
    bf._full_pass("lun", 1e18, rep, gate=gate)
    assert rep["written"]["lun"] == {"listings": 2, "price_events": 1}
    assert gate.waited == 1                                     # пауза смуги перед проходом
    tail = argv[0][argv[0].index("scrape"):]
    assert "--no-detail" in tail and tail[tail.index("--min-delay") + 1] == "1.6"
    state = json.loads((tmp_path / "state.json").read_text())
    assert set(state) == {"lun", "flombu"}


def test_scrape_no_detail_and_min_delay(monkeypatch):
    """`scrape --no-detail` — Pipeline без сторінок деталей; `--min-delay` — пауза
    джерела не менша за нічний темп хоста (flombu: повний збір 1,0 с, ніч — 1,2 с)."""
    import cli
    import realty.pipeline as pipeline
    from realty.config import SOURCES

    for name in list(SOURCES):
        monkeypatch.setitem(SOURCES, name, SOURCES[name])
    Pipeline = pipeline.Pipeline
    seen = {}

    class FakePipeline:
        def __init__(self, **kw):
            seen.update(kw)
            seen["flombu"] = SOURCES["flombu"].paced("full").delay

        def run(self):
            from realty.pipeline import RunReport
            return RunReport()

    monkeypatch.setattr(pipeline, "Pipeline", FakePipeline)
    ns = argparse.Namespace(pages=None, sources="flombu", no_llm=True, trigger="manual",
                            mode="full", no_detail=True, min_delay=1.2)
    cli.cmd_scrape(ns)
    assert seen["fetch_details"] is False and seen["flombu"] == pytest.approx(1.2)
    # Сам Pipeline з fetch_details=False сторінок не просить ніколи.
    p = Pipeline(sources=["lun"], use_llm=False, fetch_details=False)
    p._page_text = lambda rec: pytest.fail("сторінку деталей запитано")
    from realty.sources.lun import LunSource

    rec = {"source": "lun", "original_url": "https://rieltor.ua/x/1/", "price": 1, "rooms": 1,
           "area_total": 40.0, "condition": None, "market_type": None}
    assert p.complete(dict(rec), LunSource(mode="full")) == rec


# --- DOM.RIA: той самий потік, ті самі блокування, коротка транзакція --------------------------


def _ria_items(n, base=34_100_000):
    out = []
    for i in range(n):
        url = ria_url(base + i)
        row = queue.Row(id=10_000 + i, site_key=None, source="domria", external_id=str(base + i),
                        original_url=url, probe_url=None, is_active=True, manual_active=None,
                        delisted_at=None, last_seen=SEEN, last_attempt=None, last_checked=None,
                        check_failures=0, absent_since=None, viewed_at=None, property_id=None)
        out.append(queue.WorkItem(key=f"domria:{base + i}", host="dom.ria.com", url=url,
                                  tier="onetime_blind", rows=(row,)))
    return out


def test_merged_requests_of_checks_and_identity_keep_the_host_pace(wire, db):
    """Перевірки смуги й картки дозбору — один потік на dom.ria.com: у спільному журналі
    обох пауза старт-до-старту ≥ нічного темпу (раніше перша картка йшла одразу після
    останньої перевірки — свій обмежувач нічого про смугу не знав)."""
    clock, log, pages = wire
    for i in range(4):
        add(db, ria_url(34_200_000 + i), source="domria", external_id=str(34_200_000 + i))
    pages["answer"] = lambda url: (200, json.dumps({"flat_entity_id": 7, "user_id": 1}))
    net = TimedNet(FakeNet(default=200))
    lane, net = _lane("dom.ria.com", _ria_items(5), clock, identity="domria", net=net)
    summary = lane.run()
    cards = [t for h, t, u in log if "/realty/data/" in u]
    checks = [t - T0 for _h, t, _u, _m in net.log]       # журнал смуги — epoch, картки — від 0
    assert len(cards) == 4 and len(checks) == 5
    pace = policy.pace(policy.load(), "dom.ria.com", "night")
    assert min(_gaps(cards + checks)) >= pace - 1e-6
    assert summary["identity_requests"] == 4 and summary["stopped"] is None


def test_identity_blocks_stop_the_lane_like_its_checks(wire, db):
    """401/403/429 дозбору — у ті самі «5 поспіль» смуги: після п'ятої відмови дозбір
    стоїть, смуга — «blocks» (диригент: тривога, друге вікно без хоста, двічі поспіль —
    рішення власника). Відмовлені картки НЕ позначаються «пробували» — їх спитаємо знову."""
    clock, log, pages = wire
    ids = [add(db, ria_url(34_300_000 + i), source="domria", external_id=str(34_300_000 + i))
           for i in range(12)]
    pages["answer"] = lambda url: (403, "")
    out = io.StringIO()
    lane, _net = _lane("dom.ria.com", [], clock, identity="domria", out=out)
    summary = lane.run()
    assert summary["stopped"] == "blocks"
    assert summary["identity_requests"] == 5 == len(log)
    from realty.models import Listing

    with db() as s:
        assert all(s.get(Listing, lid).identity is None for lid in ids)


def test_domria_cards_are_fetched_with_no_write_transaction_open(db, monkeypatch):
    """Картки — спершу всі запити пакета, потім один короткий запис: поки йдуть запити,
    інший процес (нічний пакет застосування) пише без очікування. Раніше перший UPDATE
    пакета тримав замок запису SQLite, поки йшли решта 24 картки (~30–60 с мережі)."""
    import realty.identity_backfill as bf

    monkeypatch.setattr(bf, "SessionLocal", db)
    for i in range(3):
        add(db, ria_url(34_400_000 + i), source="domria", external_id=str(34_400_000 + i))
    path = db.kw["bind"].url.database
    writes = []

    class FakeFetcher:
        def __init__(self, *a, **kw):
            pass

        def get_json(self, url, params=None):
            con = sqlite3.connect(path, timeout=0.2)        # інший процес пише посеред пакета
            try:
                con.execute("UPDATE listings SET title = 'x' WHERE id = -1")
                con.commit()
                writes.append("ok")
            except sqlite3.OperationalError as e:
                writes.append(str(e))
            finally:
                con.close()
            return {"flat_entity_id": 7, "user_id": 1}

        def close(self):
            pass

    monkeypatch.setattr(bf, "Fetcher", FakeFetcher)
    rep = {"done": {}, "left": {}, "errors": 0}
    bf._domria(1e18, rep)
    assert writes == ["ok", "ok", "ok"]
    assert rep["done"]["domria"] == 3
