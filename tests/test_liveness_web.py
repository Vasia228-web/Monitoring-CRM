"""Сайт Блоку 1 (E8, D52): позначка Благо для обох ролей, панель «Зняті оголошення»,
зняття запобіжника лише власником, схема /api/status без змін.

На коді до E8: позначки не було (Благо ніколи не знімається, а сайт мовчав), панелі й
кінцевих точок /api/status/liveness і /api/status/liveness-fuse не було.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_web_speed import NOW, SITE, _l, site  # noqa: E402,F401 — фікстура сайту

from realty import configfiles  # noqa: E402
from realty.models import Property  # noqa: E402

# Текст — config/liveness.toml [ui] (перевіряє останній тест); тут — літералом, щоб
# на коді до E8 тест падав на відсутній позначці, а не на схемі конфігу.
LABEL = "актуальність не підтверджена"
BLAGO = "https://blagodeveloper.com/plannings/{}/"
FRIEND_U, FRIEND_PW = "druh", "druh-pass-2"


@pytest.fixture
def blago_site(site, monkeypatch):
    c, Session, engine = site
    with Session() as s:
        for pid in (61, 62):
            s.add(Property(id=pid, fingerprint=f"p{pid}", rooms=2, area_total=50.0,
                           price_usd_min=50_000.0, price_per_sqm=1000.0,
                           first_seen=NOW, last_seen=NOW))
        s.flush()
        # 61 — лише Благо; 62 — Благо й DOM.RIA (перевіряється) — без позначки в списку.
        s.add_all([_l(201, 61, source="blago", original_url=BLAGO.format(29446)),
                   _l(202, 62, source="blago", original_url=BLAGO.format(29447)),
                   _l(203, 62, original_url="https://dom.ria.com/uk/realty-prodaja-"
                                            "kvartira-ivano-frankovsk-34375047.html")])
        s.commit()
    from realty import webcache
    webcache.bump("lists", "тест")
    webcache.GENERATIONS.poll()
    monkeypatch.setenv("FRIEND_USER", FRIEND_U)
    monkeypatch.setenv("FRIEND_PASSWORD", FRIEND_PW)
    from fastapi.testclient import TestClient

    from realty.web.app import app
    friend = TestClient(app, base_url=SITE, client=("127.0.0.1", 50001), follow_redirects=False)
    r = friend.post("/login", data={"username": FRIEND_U, "password": FRIEND_PW, "next": "/"},
                    headers={"CF-Connecting-IP": "203.0.113.92", "Accept": "text/html"})
    assert r.status_code == 303
    return c, friend, Session


def _row_html(html: str, listing_id: int) -> str:
    start = html.index(f'data-id="{listing_id}"')
    row_start = html.rfind("<tr", 0, start)
    return html[row_start:html.index("</tr>", start)]


def test_unconfirmed_badge_for_blago_for_both_roles(blago_site):
    owner, friend, _ = blago_site
    for c in (owner, friend):
        html = c.get("/").text                     # рядок — квартира (представник)
        assert LABEL in _row_html(html, 201), "квартира лише з Благо — позначка в рядку"
        assert LABEL not in _row_html(html, 203), "є оголошення, що перевіряється, — без позначки"
        every = c.get("/?all_ads=1").text          # рядок — оголошення: за його хостом
        assert LABEL in _row_html(every, 202) and LABEL not in _row_html(every, 203)
        page = c.get("/property/61?verify=0").text
        assert LABEL in page
        both = c.get("/property/62?verify=0").text
        assert both.count(LABEL) == 1, "позначка — лише біля оголошення Благо"


def test_pages_without_blago_are_byte_identical_outside_marked_blocks(blago_site):
    """Позначка — у блоці <!--unconfirmed-->; без неї шаблон не додає жодного байта."""
    owner, _, _ = blago_site
    html = owner.get("/?source=domria").text
    assert "<!--unconfirmed-->" not in html
    page = owner.get("/property/2?verify=0").text
    assert "<!--unconfirmed-->" not in page


def test_status_liveness_panel_and_fuse_release_are_owner_only(blago_site):
    from realty.liveness import fuse

    owner, friend, _ = blago_site
    html = owner.get("/status").text
    assert "Зняті оголошення" in html and "/api/status/liveness" in html
    assert "<!--liveness-panel-->" in html
    d = owner.get("/api/status/liveness").json()
    assert {"report", "fuse", "latest_run", "fuse_mode"} <= set(d)
    assert "liveness" not in owner.get("/api/status").json(), "схема /api/status — без змін"
    fuse.trip([fuse.Trip("domria", "share", 30, 7, 0.2333, ["https://dom.ria.com/uk/x-1.html"])],
              run_id=None, mode="literal")
    assert fuse.held_sources() == {"domria"}
    held = owner.get("/api/status/liveness").json()["fuse"]
    assert held[0]["state"] == "held" and held[0]["checked"] == 30
    assert friend.get("/api/status/liveness").status_code == 403
    r = friend.post("/api/status/liveness-fuse", json={"source": "domria", "action": "clear"},
                    headers={"Origin": SITE})
    assert r.status_code == 403
    assert fuse.held_sources() == {"domria"}
    no_origin = owner.post("/api/status/liveness-fuse", json={"source": "domria",
                                                             "action": "clear"})
    assert no_origin.status_code == 403, "same-origin — як для будь-якого POST"
    r = owner.post("/api/status/liveness-fuse", json={"source": "domria", "action": "clear"},
                   headers={"Origin": SITE})
    assert r.status_code == 200 and r.json()["released"] is True
    assert fuse.held_sources() == set()


def test_label_comes_from_the_one_config_key():
    """Один ключ для списку, картки й /find (інтеграція, конфлікт 17)."""
    from realty.liveness import ui

    cfg = configfiles.load("liveness").ui
    assert cfg.unconfirmed_label == LABEL
    assert ui.marker_for_url(BLAGO.format(1)) == {"label": cfg.unconfirmed_label,
                                                  "hint": cfg.unconfirmed_hint}
    assert ui.marker_for_url("https://www.olx.ua/d/uk/obyavlenie/x-ID10abcD.html") is None
