"""Черги перевірки: розподіл по сайтах, порядок обходу, застосування кодів."""
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from realty import verify
from realty.models import Base, Listing

URLS = {
    "dom.ria.com": "https://dom.ria.com/uk/realty-prodaja-kvartira-{n}.html",
    "rieltor.ua": "https://rieltor.ua/ivano-frankovsk/flats-sale/view/{n}/",
    "olx.ua": "https://www.olx.ua/d/uk/obyavlenie/kvartira-ID{n}.html",
    "flombu.com": "https://flombu.com/uk/estate_deal_sales/{n}",
    "blagodeveloper.com": "https://blagodeveloper.com/plannings/{n}/",
}


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'v.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(verify, "session_scope", _scope(Session))
    with Session() as s:
        yield s


def _scope(Session):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()
    return scope


def _add(session, host, n, **over):
    rec = dict(source=over.pop("source", "test"), external_id=f"{host}-{n}",
               original_url=URLS[host].format(n=n), price_usd=50_000.0,
               quality_status="ok", is_active=True)
    rec.update(over)
    row = Listing(**rec)
    session.add(row)
    session.flush()
    return row


# --- недоторкане правило ------------------------------------------------------

def test_only_explicit_gone_codes_delist():
    assert verify.classify(404) is False and verify.classify(410) is False
    for code in (200, 301, 302, 399):
        assert verify.classify(code) is True
    for code in (0, 401, 403, 429, 500, 502, 503):
        assert verify.classify(code) is None, f"код {code} не є висновком"


def test_ambiguous_answer_never_delists_and_never_dates_the_check(session):
    """403 і обрив мережі не знімають з продажу й не вдають, що ми бачили живе.

    Друге не менш важливе за перше: якби невдала спроба ставила `last_checked`,
    аналіз виживання вважав би, що оголошення точно було живе в цей момент.
    """
    rows = [_add(session, "olx.ua", i) for i in range(3)]
    session.commit()
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "by_source": {}}
    verify._apply({rows[0].id: 403, rows[1].id: 0, rows[2].id: 500}, stats)

    session.expire_all()          # запис іде в окремій сесії — перечитуємо
    for row in session.scalars(select(Listing)):
        assert row.is_active is True
        assert row.delisted_at is None
        assert row.last_checked is None, "невдала спроба не є перевіркою"
        assert row.last_attempt is not None, "але спроба має бути зафіксована"
        assert row.check_failures == 1
    assert stats["unknown"] == 3 and stats["delisted"] == 0


def test_gone_code_delists_and_dates_both_fields(session):
    row = _add(session, "dom.ria.com", 1)
    session.commit()
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "by_source": {}}
    verify._apply({row.id: 410}, stats)
    session.expire_all()
    fresh = session.get(Listing, row.id)
    assert fresh.is_active is False
    assert fresh.delisted_at is not None
    assert fresh.last_checked is not None
    assert stats["delisted"] == 1


def test_alive_answer_resets_the_failure_counter(session):
    row = _add(session, "dom.ria.com", 2, check_failures=4)
    session.commit()
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "by_source": {}}
    verify._apply({row.id: 200}, stats)
    session.expire_all()
    assert session.get(Listing, row.id).check_failures == 0


# --- нарізка черг -------------------------------------------------------------

def test_listings_are_split_by_host_across_sources(session):
    """Посилання LUN на olx.ua потрапляє в чергу olx.ua, а не в окрему.

    Саме це не давало перевірити 2105 оголошень: вони лежали в черзі «lun»,
    ходили туди звичайним GET і отримували 403 без кінця.
    """
    _add(session, "olx.ua", 1, source="lun")
    _add(session, "olx.ua", 2, source="olx")
    _add(session, "rieltor.ua", 3, source="lun")
    session.commit()

    queues = verify.collect(session, limit_per_host=10)
    assert set(queues) == {"olx.ua", "rieltor.ua"}
    assert len(queues["olx.ua"]) == 2
    assert {c.source for c in queues["olx.ua"]} == {"lun", "olx"}


def test_unverifiable_host_is_left_out_of_every_queue(session):
    _add(session, "blagodeveloper.com", 1, source="blago")
    _add(session, "dom.ria.com", 2)
    session.commit()
    queues = verify.collect(session, limit_per_host=10)
    assert set(queues) == {"dom.ria.com"}


def test_limit_applies_per_host_not_across_all(session):
    """Порція на кожен сайт своя: інакше один великий сайт з'їдає весь бюджет.

    Через це dom.ria.com кілька діб поспіль забирав усю чергу, а решта
    сайтів не перевірялась жодного разу.
    """
    for i in range(10):
        _add(session, "dom.ria.com", i)
    for i in range(10):
        _add(session, "olx.ua", i)
    session.commit()
    queues = verify.collect(session, limit_per_host=3)
    assert len(queues["dom.ria.com"]) == 3
    assert len(queues["olx.ua"]) == 3


def test_delisted_listings_are_not_rechecked(session):
    _add(session, "dom.ria.com", 1, is_active=False)
    _add(session, "dom.ria.com", 2)
    session.commit()
    assert len(verify.collect(session, limit_per_host=10)["dom.ria.com"]) == 1


# --- порядок обходу -----------------------------------------------------------

def test_never_attempted_come_first(session):
    old = _add(session, "dom.ria.com", 1, last_attempt=datetime(2026, 9, 1))
    fresh = _add(session, "dom.ria.com", 2)
    session.commit()
    order = [c.listing_id for c in verify.collect(session, 10)["dom.ria.com"]]
    assert order.index(fresh.id) < order.index(old.id)


def test_repeatedly_failing_listings_move_to_the_back(session):
    """Безнадійне посилання не має з'їдати бюджет кожного прогону.

    Порядок за спробою, а не за перевіркою: оголошення, яке стабільно віддає
    403, після спроби йде в кінець черги замість того, щоб лишатись першим
    і не пускати туди решту.
    """
    hopeless = _add(session, "dom.ria.com", 1, last_attempt=datetime(2026, 9, 1),
                    check_failures=7)
    normal = _add(session, "dom.ria.com", 2, last_attempt=datetime(2026, 9, 2))
    session.commit()
    order = [c.listing_id for c in verify.collect(session, 10)["dom.ria.com"]]
    assert order == [normal.id, hopeless.id]


def test_oldest_attempt_goes_first_among_equals(session):
    older = _add(session, "dom.ria.com", 1, last_attempt=datetime(2026, 9, 1))
    newer = _add(session, "dom.ria.com", 2, last_attempt=datetime(2026, 9, 5))
    session.commit()
    order = [c.listing_id for c in verify.collect(session, 10)["dom.ria.com"]]
    assert order == [older.id, newer.id]


def test_explicit_ids_bypass_the_queue_order(session):
    """Кандидати, знайдені різницею списків, перевіряються точково."""
    a = _add(session, "dom.ria.com", 1)
    _add(session, "dom.ria.com", 2)
    c = _add(session, "olx.ua", 3)
    session.commit()
    queues = verify.collect(session, limit_per_host=1, ids=[a.id, c.id])
    assert {q[0].listing_id for q in queues.values()} == {a.id, c.id}


# --- зупинка на блокуваннях ---------------------------------------------------

class _FakeFetcher:
    def __init__(self, codes):
        self.codes = codes
        self.calls = []
        self.lock = threading.Lock()

    def probe(self, url, delay=None):
        with self.lock:
            self.calls.append((url, delay, time.monotonic()))
        return self.codes.get(url, 200)


def test_one_blocked_host_does_not_stop_the_others(session):
    """Відмови rieltor.ua не мають зупиняти перевірку dom.ria.com."""
    blocked = [_add(session, "rieltor.ua", i) for i in range(8)]
    fine = [_add(session, "dom.ria.com", i) for i in range(4)]
    session.commit()
    queues = verify.collect(session, limit_per_host=10)
    fetcher = _FakeFetcher({c.url: 403 for c in queues["rieltor.ua"]})

    results = {r.host: r for r in
               (verify._run_host(q, fetcher) for q in queues.values())}
    assert results["rieltor.ua"].stopped_early is True
    assert results["rieltor.ua"].requests == verify.MAX_CONSECUTIVE_BLOCKS
    assert results["dom.ria.com"].stopped_early is False
    assert results["dom.ria.com"].requests == len(fine)


def test_a_single_block_among_good_answers_does_not_stop_the_queue(session):
    rows = [_add(session, "dom.ria.com", i) for i in range(6)]
    session.commit()
    queue = verify.collect(session, limit_per_host=10)["dom.ria.com"]
    fetcher = _FakeFetcher({queue[2].url: 403})
    result = verify._run_host(queue, fetcher)
    assert result.stopped_early is False
    assert result.requests == len(rows)
    assert result.blocked == 1


def test_each_host_is_probed_with_its_own_delay(session):
    _add(session, "dom.ria.com", 1)
    _add(session, "olx.ua", 2)
    session.commit()
    queues = verify.collect(session, limit_per_host=10)
    fetcher = _FakeFetcher({})
    for q in queues.values():
        verify._run_host(q, fetcher)
    used = {url: delay for url, delay, _ in fetcher.calls}
    assert set(used.values()) == {verify.HOSTS["dom.ria.com"].delay,
                                  verify.HOSTS["olx.ua"].delay}


# --- журнал перевірок ---------------------------------------------------------

def test_every_check_is_logged_including_failed_ones(session):
    """Аналізу виживання потрібен фактичний графік спостережень.

    Черга навмисно нерівномірна, і без журналу ця нерівномірність нечутно
    зсувала б криву виживання. Невдалі спроби теж є фактом про графік.
    """
    from realty.models import CheckEvent

    alive = _add(session, "dom.ria.com", 1)
    gone = _add(session, "dom.ria.com", 2)
    murky = _add(session, "olx.ua", 3)
    session.commit()
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "by_source": {}}
    verify._apply({alive.id: 200, gone.id: 404, murky.id: 403}, stats)

    session.expire_all()
    events = {e.listing_id: e for e in session.scalars(select(CheckEvent))}
    assert len(events) == 3
    assert events[alive.id].alive is True and events[alive.id].code == 200
    assert events[gone.id].alive is False and events[gone.id].code == 404
    assert events[murky.id].alive is None, "невдала спроба теж потрапляє в журнал"
    assert all(e.reason == "sweep" for e in events.values())


def test_check_reason_distinguishes_sweep_from_candidate(session):
    from realty.models import CheckEvent

    row = _add(session, "dom.ria.com", 1)
    session.commit()
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "by_source": {}}
    verify._apply({row.id: 200}, stats, reason="candidate")
    session.expire_all()
    assert session.scalars(select(CheckEvent)).first().reason == "candidate"


def test_delisting_preserves_the_interval_bounds(session):
    """Точної дати зняття ми не знаємо — зберігаємо межі проміжку.

    `last_alive_at` лишається тим, чим був, а `delisted_at` ставиться зараз:
    разом вони й задають інтервал, усередині якого оголошення зникло. Якби
    зняття затирало `last_alive_at`, інтервал перетворився б на точку — і
    аналіз виживання отримав би вигадану точність.
    """
    row = _add(session, "dom.ria.com", 1)
    session.commit()
    stats = {"checked": 0, "alive": 0, "delisted": 0, "restored": 0, "unknown": 0,
             "by_source": {}}

    verify._apply({row.id: 200}, stats)          # бачили живим
    session.expire_all()
    seen_alive = session.get(Listing, row.id).last_alive_at
    assert seen_alive is not None

    verify._apply({row.id: 410}, stats)          # наступного разу вже немає
    session.expire_all()
    fresh = session.get(Listing, row.id)
    assert fresh.last_alive_at == seen_alive, "межу «востаннє живим» затерто"
    assert fresh.delisted_at >= seen_alive
    assert fresh.is_active is False


# --- пріоритет ----------------------------------------------------------------

def test_recent_price_drop_jumps_the_queue(session):
    """Падіння ціни — найсильніший сигнал близького завершення."""
    from realty.models import PriceEvent

    plain = _add(session, "dom.ria.com", 1, published_at=datetime(2026, 9, 1))
    dropped = _add(session, "dom.ria.com", 2, published_at=datetime(2026, 9, 1))
    session.flush()
    now = verify._now()
    for price in (60_000.0, 55_000.0):
        session.add(PriceEvent(listing_id=dropped.id, source="test", price_usd=price,
                               observed_at=now))
    session.commit()

    order = [c.listing_id for c in verify.collect(session, 10)["dom.ria.com"]]
    assert order.index(dropped.id) < order.index(plain.id)


def test_long_listed_objects_come_before_fresh_ones(session):
    """Щойно опубліковане майже напевно живе — перевіряти його марно."""
    now = verify._now()
    old = _add(session, "dom.ria.com", 1, last_attempt=now,
               published_at=now - timedelta(days=verify.OLD_LISTING_DAYS + 30))
    fresh = _add(session, "dom.ria.com", 2, last_attempt=now,
                 published_at=now - timedelta(days=2))
    session.commit()
    order = [c.listing_id for c in verify.collect(session, 10)["dom.ria.com"]]
    assert order == [old.id, fresh.id]


def test_hopeless_links_stay_behind_every_priority(session):
    """Безнадійне посилання не обганяє нікого, навіть зі свіжим падінням ціни."""
    from realty.models import PriceEvent

    now = verify._now()
    hopeless = _add(session, "dom.ria.com", 1, check_failures=9, last_attempt=now,
                    published_at=now - timedelta(days=300))
    session.flush()
    for price in (60_000.0, 50_000.0):
        session.add(PriceEvent(listing_id=hopeless.id, source="test", price_usd=price,
                               observed_at=now))
    normal = _add(session, "dom.ria.com", 2, last_attempt=now,
                  published_at=now - timedelta(days=1))
    session.commit()
    order = [c.listing_id for c in verify.collect(session, 10)["dom.ria.com"]]
    assert order == [normal.id, hopeless.id]


def test_snapshot_covered_sources_match_the_snapshot_module():
    """Два модулі не імпортують один одного, тож збіг тримає тест."""
    from realty import snapshot

    assert verify.SNAPSHOT_COVERED == snapshot.ENUMERABLE


def test_each_host_has_its_own_sweep_portion(session):
    """Сайт під наглядом переліку не має з'їдати стільки ж, скільки OLX."""
    for i in range(300):
        _add(session, "dom.ria.com", i, source="domria")
        _add(session, "olx.ua", i, source="olx")
    session.commit()
    queues = verify.collect(session)
    assert len(queues["dom.ria.com"]) == verify.HOSTS["dom.ria.com"].sweep_limit
    assert len(queues["olx.ua"]) == verify.HOSTS["olx.ua"].sweep_limit
    assert len(queues["olx.ua"]) > len(queues["dom.ria.com"])


def test_explicit_limit_overrides_every_host_portion(session):
    for i in range(50):
        _add(session, "olx.ua", i, source="olx")
    session.commit()
    assert len(verify.collect(session, limit_per_host=7)["olx.ua"]) == 7


def test_sources_watched_by_snapshots_yield_the_queue(session):
    """Сліпа перевірка витрачається насамперед туди, де переліку немає."""
    now = verify._now()
    watched = _add(session, "olx.ua", 1, source="lun", last_attempt=now,
                   published_at=now - timedelta(days=400))
    unwatched = _add(session, "olx.ua", 2, source="olx", last_attempt=now,
                     published_at=now - timedelta(days=1))
    session.commit()
    order = [c.listing_id for c in verify.collect(session)["olx.ua"]]
    assert order == [unwatched.id, watched.id]
