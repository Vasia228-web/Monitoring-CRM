"""Спільне для тестів Блоку 1 (E8, D52): тимчасова база, фальшива мережа, фікстури Етапу 0.

Фальшива мережа відповідає за ключем «сайт:id» (або точною адресою, якщо вона
задана) — тим самим, за яким перевірка працює. Має й `check` (новий шлях), і
`probe` (код HEAD) — так тест, запущений на коді до E8, падає саме на поведінці, а
не на відсутньому методі фетчера. Мережі немає (conftest її забороняє).
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import links  # noqa: E402
from realty.models import Base, Listing  # noqa: E402

try:                                    # код до E8 цього класу не мав — там лише probe()
    from realty.fetcher import ProbeResult  # noqa: E402
except ImportError:                     # pragma: no cover — лише прогін на старому коді
    ProbeResult = None

FIXTURES = ROOT / "tests" / "fixtures" / "liveness"


def manifest() -> list[dict]:
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["cases"]


def body_of(case: dict) -> str | None:
    if not case.get("body"):
        return None
    return (FIXTURES / case["body"]).read_text(encoding="utf-8")


def probe_of(case: dict):
    """Відповідь Етапу 0 як ProbeResult (адреси — нейтральні, ключ той самий)."""
    return ProbeResult(code=int(case["code"]), method=case["method"], url=case["url"],
                       final_url=case["final_url"],
                       chain=tuple((int(c), u) for c, u in case["chain"]),
                       body=body_of(case))


def ria_page(realty_id: int | str, *, archived: bool = False, banner: bool | None = None,
             state: bool = True, extra_realty: dict | None = None,
             deleted_ts: int | None = None) -> str:
    """Мінімальна сторінка DOM.RIA тієї самої будови, що й фікстури Етапу 0."""
    if banner is None:
        banner = archived
    realty = {"realty_id": int(realty_id), "status": "archive" if archived else "active",
              "isActive": not archived, "isArchive": archived, "isSold": False}
    if archived and deleted_ts:
        realty["deleted_at_ts"] = deleted_ts
    realty.update(extra_realty or {})
    css = ("<style>.bg.m-sold{width:100%;background:#ffa369} "
           ".bg.m-sold.isCoulumn{padding:25px 15px}</style>") if archived else ""
    ban = ('<div class="middle bg mobileW100 flex f-center m-sold f-space"><div class="message">'
           "<span>  Оголошення видалено та не бере участі у пошуку  </span></div></div>"
           if banner else "")
    st = ("<script>window.__INITIAL_STATE__=" + json.dumps(
        {"serverStatus": 200, "listing": {"data": {"realty": realty}}}, ensure_ascii=False)
        + ";</script>") if state else ""
    return (f'<!doctype html><html><head><meta charset="utf-8">{css}</head><body>'
            f"<h1>ID {realty_id}</h1>{ban}"
            '<div class="page-review"><div class="bold">Оголошення неактуальне чи інформація '
            "неточна?</div></div>" + st + "</body></html>")


def ria_url(realty_id, slug: str = "realty-prodaja-kvartira-ivano-frankovsk") -> str:
    return f"https://dom.ria.com/uk/{slug}-{realty_id}.html"


def olx_url(token: str, slug: str = "kvartyra") -> str:
    return f"https://www.olx.ua/d/uk/obyavlenie/{slug}-ID{token}.html"


def rieltor_url(n) -> str:
    return f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{n}/"


class FakeNet:
    """Відповіді за точною адресою або за ключем «сайт:id»; рахує запити."""

    def __init__(self, responses: dict | None = None, default=200) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[tuple[str, str, float | None]] = []

    def _answer(self, url: str, method: str):
        got = self.responses.get(url)
        if got is None:
            got = self.responses.get(links.site_key(url) or url, self.default)
        if callable(got):
            got = got(url, method)
        if isinstance(got, list):
            got = got.pop(0) if len(got) > 1 else got[0]
        return got

    def check(self, url, method="HEAD", delay=None, max_bytes=0) -> ProbeResult:
        self.calls.append((url, method, delay))
        got = self._answer(url, method)
        if ProbeResult is not None and isinstance(got, ProbeResult):
            return ProbeResult(code=got.code, method=method, url=url,
                               final_url=got.final_url or url, chain=got.chain,
                               body=got.body if method == "GET" else None, error=got.error)
        if isinstance(got, tuple):                       # (код, тіло)
            code, body = got
            if ProbeResult is None:
                return code
            return ProbeResult(code=code, method=method, url=url, final_url=url,
                               body=body if method == "GET" else None)
        if ProbeResult is None:
            return int(got)
        return ProbeResult(code=int(got), method=method, url=url, final_url=url)

    def probe(self, url, delay=None) -> int:            # шлях коду до E8 (лише код HEAD)
        got = self.check(url, "HEAD", delay)
        return got if isinstance(got, int) else got.code

    def close(self) -> None:
        pass


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Тимчасова база; перевірка (verify, snapshot) пише саме в неї."""
    import realty.verify as verify

    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(verify, "session_scope", scope)
    try:
        import realty.snapshot as snapshot
        monkeypatch.setattr(snapshot, "session_scope", scope)
        monkeypatch.setattr(snapshot, "DIR", tmp_path / "snapshots")
    except Exception:                                   # noqa: BLE001 — модуля могло не бути
        pass
    yield Session
    engine.dispose()


@pytest.fixture(autouse=False)
def clean_fuse():
    """Запобіжник живе в ops.db копії тестів — кожен тест починає й закінчує чистим."""
    def wipe():
        for table in ("liveness_fuse", "liveness_fuse_log"):
            try:
                from realty import ops
                ops.init_ops(force=True)
                with ops.engine.begin() as conn:
                    conn.execute(text(f"DELETE FROM {table}"))
            except Exception:                           # noqa: BLE001 — до E8 таблиці немає
                pass
    wipe()
    yield
    wipe()


def add(Session, url: str, *, source: str, external_id: str | None = None, **over) -> int:
    rec = dict(source=source, external_id=external_id or url, original_url=url,
               price_usd=50_000.0, quality_status="ok", is_active=True,
               last_seen=datetime(2026, 10, 1))
    rec.update(over)
    with Session() as s:
        row = Listing(**rec)
        s.add(row)
        s.commit()
        return row.id


def get(Session, lid: int) -> Listing:
    with Session() as s:
        return s.get(Listing, lid)


def events(Session, lid: int | None = None) -> list:
    from realty.models import ListingEvent

    with Session() as s:
        q = s.query(ListingEvent)
        if lid is not None:
            q = q.filter(ListingEvent.listing_id == lid)
        return q.order_by(ListingEvent.id).all()


def checks(Session, lid: int) -> list:
    from realty.models import CheckEvent

    with Session() as s:
        return s.query(CheckEvent).filter(CheckEvent.listing_id == lid).order_by(
            CheckEvent.id).all()


T0 = datetime(2026, 10, 8, 3, 0)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)
