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


def test_domain_without_token_is_a_config_error():
    assert "error" in tunnel.plan({**AUTH, "PUBLIC_DOMAIN": "x.org"})


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
