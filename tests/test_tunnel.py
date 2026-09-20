"""Тунель: домен — параметр .env, без пароля в інтернет не відкриваємось."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from realty import tunnel  # noqa: E402

AUTH = {"AUTH_USER": "u", "AUTH_PASSWORD": "p"}


def test_quick_tunnel_without_domain():
    p = tunnel.plan({**AUTH, "PORT": "8000"})
    assert p["mode"] == "quick" and p["url"] is None
    assert p["args"][-1] == "http://127.0.0.1:8000"


def test_domain_is_just_a_value_in_env():
    p = tunnel.plan({**AUTH, "PUBLIC_DOMAIN": "https://realty.example.org/",
                     "CLOUDFLARE_TUNNEL_TOKEN": "tok"})
    assert p["mode"] == "named" and p["url"] == "https://realty.example.org"
    assert "tok" not in " ".join(p["args"])          # токен не видно в `ps`
    assert p["env"] == {"TUNNEL_TOKEN": "tok"}


def test_refuses_to_open_site_without_password():
    assert "error" in tunnel.plan({"PUBLIC_DOMAIN": ""})
    assert "error" in tunnel.plan({"AUTH_USER": "u"})


def test_domain_without_any_tunnel_credentials_is_a_config_error():
    assert "error" in tunnel.plan({**AUTH, "PUBLIC_DOMAIN": "x.org"})


def test_named_tunnel_by_local_credentials():
    """Після `cloudflared tunnel login` облікові дані лежать на машині —
    тоді тунель запускається за іменем, без токена."""
    p = tunnel.plan({**AUTH, "PUBLIC_DOMAIN": "mojkvartiry.link",
                     "CLOUDFLARE_TUNNEL_NAME": "realty", "PORT": "8000"})
    assert p["mode"] == "named" and p["url"] == "https://mojkvartiry.link"
    assert p["args"] == ["tunnel", "--no-autoupdate", "run", "--url",
                         "http://127.0.0.1:8000", "realty"]


def test_quick_url_is_recognised_in_cloudflared_output():
    line = "2026-09-19T20:00:00Z INF |  https://brave-otter-lamp-quiet.trycloudflare.com  |"
    assert tunnel.QUICK_URL.search(line).group(0) == \
        "https://brave-otter-lamp-quiet.trycloudflare.com"


def test_no_hardcoded_domain_anywhere():
    """Домен — лише значення в .env: у коді, юнітах і скриптах його немає."""
    root = Path(__file__).resolve().parent.parent
    files = [*(root / "realty").rglob("*.py"), *(root / "deploy").rglob("*.*")]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "PUBLIC_DOMAIN=" not in text.replace("PUBLIC_DOMAIN=…", "") or f.suffix == ".md"
        if f.name != "tunnel.py":
            assert "trycloudflare.com" not in text, f


def test_every_response_forbids_indexing(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    from realty.web.app import app
    c = TestClient(app)
    for r in (c.get("/"), c.get("/healthz"), c.get("/", auth=("u", "p"))):
        assert r.headers.get("x-robots-tag") == "noindex, nofollow"
    assert c.get("/").status_code == 401


def test_cloudflare_service_address_is_not_the_site_address():
    """Регресія: у тексті помилки cloudflared є https://api.trycloudflare.com —
    перша версія виразу записала її як адресу сайту й надіслала в Telegram."""
    err = ('failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": '
           'context deadline exceeded')
    assert tunnel.QUICK_URL.search(err) is None
    ok = "INF |  https://brave-otter-lamp-quiet.trycloudflare.com  |"
    assert tunnel.QUICK_URL.search(ok).group(0).endswith("quiet.trycloudflare.com")


def test_address_is_announced_only_after_it_answers(monkeypatch):
    calls = []
    monkeypatch.setattr(tunnel.httpx, "get",
                        lambda url, **kw: calls.append(url) or _Resp(502))
    assert tunnel.responds("https://x.trycloudflare.com", attempts=1) is False
    assert calls == ["https://x.trycloudflare.com/healthz"]
    monkeypatch.setattr(tunnel.httpx, "get", lambda url, **kw: _Resp(200))
    assert tunnel.responds("https://x.trycloudflare.com", attempts=1) is True


class _Resp:
    def __init__(self, code):
        self.status_code = code
