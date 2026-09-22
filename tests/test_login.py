"""Вхід через сторінку з формою: повідомлення, ліміт спроб, кука, вихід, ролі.

Тестовий клієнт приходить «від тунелю» (127.0.0.1), як cloudflared, тож
заголовок CF-Connecting-IP — справжня адреса відвідувача — береться до уваги.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from realty import ops
from realty.web import auth, sessions
from realty.web.app import app

OWNER, OWNER_PW = "vasia", "пароль власника 1"      # кирилиця й пробіл — навмисно
FRIEND, FRIEND_PW = "druh", "druh-pass-2"
SITE = "https://mojkvartiry.test"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    monkeypatch.setattr(ops, "engine", engine)
    monkeypatch.setattr(ops, "OpsSession",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    ops.OpsBase.metadata.create_all(engine)
    monkeypatch.setattr(sessions, "SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("AUTH_USER", OWNER)
    monkeypatch.setenv("AUTH_PASSWORD", OWNER_PW)
    monkeypatch.setenv("FRIEND_USER", FRIEND)
    monkeypatch.setenv("FRIEND_PASSWORD", FRIEND_PW)
    return tmp_path


def client(peer="127.0.0.1"):
    return TestClient(app, base_url=SITE, client=(peer, 50000), follow_redirects=False)


def login(c, user, pw, ip="203.0.113.10", ua=MAC_CHROME, country="UA"):
    return c.post("/login", data={"username": user, "password": pw, "next": "/"},
                  headers={"CF-Connecting-IP": ip, "CF-IPCountry": country,
                           "User-Agent": ua, "Accept": "text/html"})


def test_page_opens_and_unauthenticated_browser_is_sent_to_it(env):
    c = client()
    r = c.get("/processing", headers={"Accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/processing"
    page = c.get("/login")
    assert page.status_code == 200 and 'name="password"' in page.text
    assert 'autocapitalize="none"' in page.text            # iPhone більше не псує логін
    assert "www-authenticate" not in {k.lower() for k in r.headers}   # без віконця


def test_wrong_password_gives_a_clear_message(env):
    r = login(client(), OWNER, "не той")
    assert r.status_code == 401
    assert "Невірний логін або пароль" in r.text
    assert auth.COOKIE not in r.cookies


def test_eleventh_failed_attempt_is_blocked(env):
    c = client()
    for i in range(1, 11):
        r = login(c, OWNER, f"хибний-{i}", ip="198.51.100.1")
        assert r.status_code == 401, i
    assert "заблоковано до" in r.text                        # 10-та: перевірена й повідомляє
    r = login(c, OWNER, OWNER_PW, ip="198.51.100.1")          # 11-та — навіть із правильним
    assert r.status_code == 429
    assert auth.COOKIE not in r.cookies


def test_limit_counts_the_real_visitor_not_the_tunnel(env):
    c = client()
    for i in range(10):
        login(c, OWNER, "хибний", ip="198.51.100.2")          # бот заблокував себе
    r = login(c, OWNER, OWNER_PW, ip="203.0.113.77")          # людина з іншої адреси
    assert r.status_code == 303 and auth.COOKIE in r.cookies


def test_forged_header_from_outside_the_tunnel_is_ignored(env):
    outsider = client(peer="192.0.2.50")                      # не localhost — не тунель
    login(outsider, OWNER, "хибний", ip="1.1.1.1")
    ips = [b.ip for b in sessions_rows()]
    assert ips == ["192.0.2.50"]                              # лічимо справжнього, не підставленого


def sessions_rows():
    from sqlalchemy import select
    with ops.ops_session() as s:
        return list(s.scalars(select(sessions.AuthBlock)))


def test_script_header_login_is_under_the_same_limit(env):
    c = client()
    h = {"CF-Connecting-IP": "198.51.100.3"}
    for _ in range(10):
        r = c.get("/api/stats", auth=(OWNER, "хибний"), headers=h)
        assert r.status_code == 401
    r = c.get("/api/stats", auth=(OWNER, OWNER_PW), headers=h)
    assert r.status_code == 429                                # і скрипт заблоковано
    assert login(c, OWNER, OWNER_PW, ip="198.51.100.3").status_code == 429   # і форму
    ok = c.get("/api/stats", auth=(OWNER, OWNER_PW), headers={"CF-Connecting-IP": "203.0.113.5"})
    assert ok.status_code == 200


def test_session_cookie_is_httponly_secure_samesite(env):
    r = login(client(), OWNER, OWNER_PW)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_logout_really_logs_out(env):
    c = client()
    login(c, OWNER, OWNER_PW)
    assert c.get("/", headers={"Accept": "text/html"}).status_code == 200
    token = c.cookies.get(auth.COOKIE)
    r = c.post("/logout", headers={"Origin": SITE})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/", headers={"Accept": "text/html"}).status_code == 303
    # Навіть збережена копія куки після виходу мертва — сесію знищено на сервері.
    other = client()
    other.cookies.set(auth.COOKIE, token, domain="mojkvartiry.test")
    assert other.get("/", headers={"Accept": "text/html"}).status_code == 303


def test_change_from_another_site_is_refused(env):
    c = client()
    login(c, OWNER, OWNER_PW)
    evil = c.post("/api/listings/1/processing", json={"in_progress": True},
                  headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    no_origin = c.post("/api/listings/1/processing", json={"in_progress": True})
    assert no_origin.status_code == 403
    own = c.post("/api/listings/1/processing", json={"in_progress": False},
                 headers={"Origin": SITE})
    assert own.status_code in (200, 404)                       # пройшов перевірку походження


def test_friend_sees_the_site_but_not_status_or_controls(env):
    c = client()
    assert login(c, FRIEND, FRIEND_PW).status_code == 303
    html = c.get("/", headers={"Accept": "text/html"})
    assert html.status_code == 200 and "Стан системи" not in html.text and "Вийти" in html.text
    assert c.get("/status", headers={"Accept": "text/html"}).status_code == 403
    assert c.get("/api/status").status_code == 403
    assert c.post("/api/status/run", json={"source": "olx"},
                  headers={"Origin": SITE}).status_code == 403            # запуск збору
    assert c.get("/api/auth/blocks").status_code == 403                    # блокування
    assert c.post("/api/auth/blocks/1.2.3.4/unblock",
                  headers={"Origin": SITE}).status_code == 403
    shared = c.post("/api/listings/1/processing", json={"in_progress": False},
                    headers={"Origin": SITE})
    assert shared.status_code in (200, 404)                                # «в обробці» — спільне


def test_block_shows_on_status_and_unblock_button_works(env):
    bot = client()
    for _ in range(10):
        login(bot, OWNER, "хибний", ip="198.51.100.7", ua=IPHONE, country="PL")
    assert login(bot, OWNER, OWNER_PW, ip="198.51.100.7").status_code == 429

    owner = client()
    assert login(owner, OWNER, OWNER_PW, ip="203.0.113.20").status_code == 303
    page = owner.get("/status", headers={"Accept": "text/html"})
    assert page.status_code == 200 and "Заблоковані входи" in page.text
    blocks = owner.get("/api/auth/blocks").json()["blocks"]
    entry = next(b for b in blocks if b["ip"] == "198.51.100.7")
    assert (entry["country"], entry["device"], entry["browser"]) == ("PL", "iPhone", "Safari")
    assert entry["failures"] == 10 and entry["blocked_at"] and entry["blocked_until"]

    r = owner.post("/api/auth/blocks/198.51.100.7/unblock", headers={"Origin": SITE})
    assert r.json()["ok"] is True
    assert not any(b["ip"] == "198.51.100.7" for b in owner.get("/api/auth/blocks").json()["blocks"])
    assert login(bot, OWNER, OWNER_PW, ip="198.51.100.7").status_code == 303   # знову пускає


def test_block_lifts_by_itself_after_15_minutes(env, monkeypatch):
    c = client()
    for _ in range(10):
        login(c, OWNER, "хибний", ip="198.51.100.8")
    assert login(c, OWNER, OWNER_PW, ip="198.51.100.8").status_code == 429
    later = ops._now() + timedelta(minutes=16)
    monkeypatch.setattr(ops, "_now", lambda: later)
    assert login(c, OWNER, OWNER_PW, ip="198.51.100.8").status_code == 303


def test_return_address_cannot_lead_to_another_site(env):
    c = client()
    r = c.post("/login", data={"username": OWNER, "password": OWNER_PW,
                               "next": "//evil.example/steal"},
               headers={"CF-Connecting-IP": "203.0.113.30"})
    assert r.headers["location"] == "/"
