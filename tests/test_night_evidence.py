"""Дозбір доказів Блоків 3/4 у нічному диригенті цілком (E11, D60): план → бекап →
смуги (Блок 1, потім рендери OLX) → запис ночі з покриттям → щоденне зведення,
/api/status/night, `night --dry-run`.

Віртуальний час і смуги в процесі тесту (night_kit), браузера немає (FakeRenderer),
мережі немає. На коді до E11 диригент доказів не дозбирав: ні рендерів, ні покриття, ні
evidence_summary, ні /api/status/night, ні розділу дозбору в `--dry-run`.
"""
from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import (  # noqa: E402,F401
    FakeNet, add, clean_fuse, db, get, olx_url, ria_page, ria_url, rieltor_url,
)
from night_kit import (  # noqa: E402,F401
    FakeClock, TimedNet, VirtualLauncher, clean_night, local_epoch, night_env, scope_of, utc_of,
)
from seller_kit import NAME, olx_detail_html, olx_tab_html  # noqa: E402
from test_web_speed import site  # noqa: E402,F401 — фікстура сайту з входом власника

from realty import configfiles, ops  # noqa: E402
from realty.liveness import policy  # noqa: E402
from realty.models import CheckEvent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.usefixtures("clean_fuse", "clean_night")
T0 = local_epoch(2026, 10, 9, 1, 10)


class FakeRenderer:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.log: list[tuple[float, str]] = []

    def render(self, url):
        from realty.night.render import RenderResult

        self.log.append((self.clock.time(), url))
        if "private_business" in url:
            return RenderResult(200, olx_tab_html("Приватні" if "=private" in url else "Бізнес",
                                                  ["10Ev001"]), url)
        return RenderResult(200, olx_detail_html(chip="Приватна особа"), url)

    def close(self, dead=False):
        pass


def _answered(db, lid, hours_ago=2):
    with db() as s:
        s.add(CheckEvent(listing_id=lid, checked_at=utc_of(T0 - hours_ago * 3600), code=200,
                         alive=True, reason="sweep", signature="alive"))
        s.commit()


RID = 34_900_001


def _night(db, tmp_path, *, backup_age_hours=5):
    from realty.night import evidence
    from realty.night.conductor import Conductor

    lcfg, ncfg = policy.load(), configfiles.load("night")
    master = FakeClock(T0)
    page = ria_page(RID, extra_realty={"characteristics_values": {"1437": 1434},
                                       "user_id": 900777})
    timed = TimedNet(FakeNet({f"domria:{RID}": (200, page)}, default=200))
    renderers: list[FakeRenderer] = []

    def ev_factory(clock):
        r = FakeRenderer(clock)
        renderers.append(r)
        return functools.partial(evidence.run_olx_jobs, renderer_factory=lambda rcfg: r,
                                 scope=scope_of(db), now_fn=lambda: utc_of(clock.time()),
                                 mem_fn=lambda: 5000)

    launcher = VirtualLauncher(master, timed, lcfg, evidence_fn=ev_factory)
    env, calls = night_env(tmp_path, scope_of(db), master, launcher,
                           backup_age_hours=backup_age_hours)
    res = Conductor(ncfg=ncfg, nhash="n" * 16, lcfg=lcfg, lhash="l" * 16, env=env).run()
    return res, timed, renderers, calls


def _seed(db):
    """OLX-ключ із відповіддю новим підписом (не M3, не контрольний) без доказів сторінки і
    ключ rieltor без доказів картки (у M3 — GET замість HEAD)."""
    lid = add(db, olx_url("10Ev001"), source="olx", external_id="10Ev001")
    _answered(db, lid)
    rl = add(db, rieltor_url(13400001), source="lun", external_id="r1")
    # DOM.RIA: докази продавця — з того самого GET перевірки (гачок стану, E8), і вночі теж.
    add(db, ria_url(RID), source="domria", external_id=str(RID))
    return lid, rl


def test_night_collects_evidence_after_block1_and_records_coverage(db, tmp_path):
    from realty.night import report

    lid, rl = _seed(db)
    res, timed, renderers, calls = _night(db, tmp_path)
    assert res["status"] in ("ok", "partial"), res
    row = report.runs(1, run_id=res["night_run_id"])[0]
    # Бекап: останній 5 год тому — свіжий за правилом 20 год, але вікно допише докази в
    # непорожні JSON — правило одноразових робіт (3 год).
    assert calls["backup"] == 1 and row["backup"]["rule"] == "onetime_max_age_hours"
    ev = row["evidence"]
    assert ev["plan"]["olx_detail"] == 1 and ev["plan"]["body_gets"] == 1
    olx = ev["lanes"]["olx.ua"]
    assert olx["detail"]["written"] == 1 and olx["tabs"]["private"]["keys"] == 1
    assert ev["coverage"]["olx>olx"]["with"] == 1 and ev["coverage"]["olx>olx"]["total"] == 1
    lanes = row["lanes"]
    rendered = sum(len(r.log) for r in renderers)                   # рендерить лише olx.ua
    assert lanes["olx.ua"]["evidence_requests"] == rendered >= 2
    assert get(db, lid).seller_evidence["olx_chip"] == "private"
    with db() as s:
        from realty.models import Listing

        ria = s.query(Listing).filter(Listing.external_id == str(RID)).one()
        assert ria.seller_evidence["ria_offer"] == 1434 and ria.seller_profile == "ria:900777"
    methods = {u: m for _h, _t, u, m in timed.log}
    assert methods[rieltor_url(13400001)] == "GET"                  # M3 rieltor — з тілом
    text = report.render_run(row)
    assert "дозбір доказів (olx.ua)" in text and "сторінок деталей 1" in text
    # Щоденне зведення (хвиля W3 кличе саме цю функцію).
    with db() as s:
        lines = report.evidence_summary(s)
    assert isinstance(lines, list) and all(isinstance(x, str) for x in lines)
    joined = "\n".join(lines)
    assert "Докази типу продавця" in joined and "OLX 1 з 1 (100%)" in joined
    assert f"Ніч {row['night_date']}" in joined and "рендерів OLX" in joined
    assert NAME not in joined


def test_steady_window_without_evidence_keeps_the_20_hour_rule(db, tmp_path):
    """Вікно, якому нічого дописувати (докази вже є), — правило 20 год, як і до E11."""
    lid = add(db, olx_url("10Ev002"), source="olx", external_id="10Ev002",
              seller_evidence={"olx_detail_at": "2026-10-01", "olx_chip": "business"})
    _answered(db, lid)
    from realty.night import evidence

    evidence.state_put(evidence.OLX_TABS_STATE, {
        "private_at": utc_of(T0).isoformat(),
        "business": {"started_at": utc_of(T0 - 86400).isoformat(),
                     "finished_at": utc_of(T0 - 3600).isoformat()}})
    res, _t, _r, calls = _night(db, tmp_path)
    assert calls["backup"] == 0, res


def test_api_status_night_is_owner_only_and_reads_the_record(site):
    c, _Session, _engine = site
    with ops.ops_session() as s:
        s.add(ops.NightRun(status="ok", window="01:10", night_date="2026-10-09",
                           started_at=datetime(2026, 10, 8, 22, 10),
                           finished_at=datetime(2026, 10, 8, 23, 50),
                           lanes=json.dumps({"olx.ua": {"requests": 10, "evidence_requests": 612,
                                                        "stopped": None}}),
                           evidence=json.dumps({"plan": {"olx_detail": 600},
                                                "coverage": {"olx>olx": {"label": "OLX",
                                                                         "total": 10,
                                                                         "with": 4}}})))
    r = c.get("/api/status/night")
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"]["olx>olx"]["with"] == 4
    assert body["nights"][0]["lanes"]["olx.ua"]["evidence_requests"] == 612
    assert body["nights"][0]["evidence"]["plan"]["olx_detail"] == 600


def test_dry_run_shows_evidence_jobs_per_host_and_window(tmp_path):
    from realty.models import Base, Listing

    db_path, ops_path = tmp_path / "cli.db", tmp_path / "cli_ops.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        for i in range(60):
            s.add(Listing(source="lun", external_id=f"lu{i}",
                          original_url=rieltor_url(13500000 + i), price_usd=50_000.0,
                          quality_status="ok", is_active=True, last_seen=datetime(2026, 10, 1)))
        for i in range(5):
            s.add(Listing(source="olx", external_id=f"10Dr{i:03d}",
                          original_url=olx_url(f"10Dr{i:03d}"), price_usd=50_000.0,
                          quality_status="ok", is_active=True, last_seen=datetime(2026, 10, 1)))
        s.commit()
    engine.dispose()
    env = {**os.environ, "DB_URL": f"sqlite:///{db_path}", "OPS_DB_URL": f"sqlite:///{ops_path}"}
    r = subprocess.run([sys.executable, "cli.py", "night", "--dry-run", "--json"], cwd=ROOT,
                       env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    d = json.loads(r.stdout)
    olx, rlt = d["hosts"]["olx.ua"], d["hosts"]["rieltor.ua"]
    est = olx["evidence_estimate"]
    assert est["job"] == "olx_render" and est["rate"] == pytest.approx(6.8)
    assert est["total"] == est["tabs"] + 5 and est["cap_per_window"] == 812
    assert rlt["body_gets"] == 60 and rlt["requests"] == 60      # GET замість HEAD, не більше
    assert d["evidence"]["writes"] > 0 and d["backup"]["due"]
    # Вікно без Блоку 1 вміщає стелю рендерів (812 × 6,8 с ≈ 92 хв < 97 хв до stop_requests).
    assert est["full_window"] == 812 and 812 * 6.8 <= 97 * 60
    text = subprocess.run([sys.executable, "cli.py", "night", "--dry-run"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)
    assert text.returncode == 0
    assert "ДОЗБІР ДОКАЗІВ" in text.stdout and "рендери OLX" in text.stdout
    assert "GET замість HEAD для доказів: 60 з 60" in text.stdout
    assert "olx_tabs" not in text.stdout.split("порядок робіт")[1].split("\n", 2)[2][:300]
