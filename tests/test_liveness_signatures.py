"""Класифікатор Блоку 1 на відповідях Етапу 0: живе не знімається ніколи (E8, D52).

Фікстури — tests/fixtures/liveness: знеособлені відповіді живої вибірки Етапу 0
(07.10.2026, D45) з вердиктом, перевіреним тоді очима й браузером: DOM.RIA — код і
сторінка (стан __INITIAL_STATE__ з білим списком полів, банер m-sold), OLX, rieltor,
flombu, lun.ua — код і переадресації HEAD, Благо — переадресація на каталог.

Що на коді до E8 було інакше (і чому ці тести там падають): HEAD 200 на DOM.RIA
вважався «живе» — 18 з 18 знятих зі сторінкою «Оголошення видалено…» лишались
активними; один 404 знімав (DOM.RIA 8046 і OLX 22359 зняли так, а вони живі);
порожня чи капчева сторінка 200 ставила last_alive_at; переадресація на каталог чи
головну давала «живе».
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import manifest, probe_of, ria_page  # noqa: E402

from realty import configfiles  # noqa: E402
from realty.fetcher import ProbeResult  # noqa: E402
from realty.liveness import policy as pol  # noqa: E402
from realty.liveness.signatures import (  # noqa: E402
    ALIVE, NOT_FOUND, REMOVED, UNKNOWN, classify, ria_deleted_at_utc,
)

CFG = configfiles.load("liveness")
CASES = manifest()


def _classify(case):
    host = pol.host_for(CFG, case["key"], case["url"])
    return host, classify(CFG.hosts[host], CFG.ria_page, case["key"], probe_of(case))


def test_fixtures_cover_every_host_and_verdict_of_stage0():
    """Кількості — як у вибірці Етапу 0 (verdicts_*.json, D45)."""
    got = Counter((c["host"], c["truth"]) for c in CASES)
    assert got[("dom.ria.com", "alive")] == 108
    assert got[("dom.ria.com", "removed")] == 43        # 18 банер + 25 код 410
    assert got[("olx.ua", "alive")] == 191 and got[("olx.ua", "removed")] == 30
    assert got[("rieltor.ua", "alive")] == 52 and got[("rieltor.ua", "removed")] == 10
    assert got[("flombu.com", "alive")] == 25 and got[("lun.ua", "alive")] == 8
    assert got[("blagodeveloper.com", "unknown")] >= 100


def test_known_alive_pages_are_never_removed():
    """Кожна жива відповідь Етапу 0 — «живе» (або «не визначено», якщо сайт не
    відповів), і жодна — «знято». Головний захист від хибного зняття."""
    alive_ok = 0
    for case in CASES:
        if case["truth"] == "removed" or case["host"] == "blagodeveloper.com":
            continue
        host, v = _classify(case)
        assert v.kind != REMOVED, (case["url"], v.signature, v.evidence)
        if case["truth"] == "alive" and 200 <= case["code"] < 400:
            assert v.kind == ALIVE, (case["url"], v.signature, v.evidence)
            alive_ok += 1
    assert alive_ok >= 380


def test_explicit_removal_signals_are_recognised():
    """410 і сторінка DOM.RIA «архів + банер» — «знято»; 404 — лише «не знайдено»."""
    kinds = Counter()
    for case in CASES:
        if case["truth"] != "removed":
            continue
        host, v = _classify(case)
        kinds[(host, case["code"], v.kind, v.signature)] += 1
        if case["code"] == 410 and 410 in CFG.hosts[host].removed_statuses:
            assert (v.kind, v.signature) == (REMOVED, "status_410"), case["url"]
        elif case["code"] == 200:
            assert (v.kind, v.signature) == (REMOVED, "ria_archive"), (case["url"], v.evidence)
        elif case["code"] == 404:
            # flombu і lun.ua: один 404 — ще не зняття (правило повторного 404, D46).
            assert v.kind == NOT_FOUND, case["url"]
    assert kinds[("dom.ria.com", 200, REMOVED, "ria_archive")] == 18
    assert kinds[("dom.ria.com", 410, REMOVED, "status_410")] == 25
    assert kinds[("olx.ua", 410, REMOVED, "status_410")] == 30


def test_ria_archive_date_is_utc_from_the_page_state():
    """deleted_at_ts — секунди UTC; запасне deleted_at — час Києва (Етап 0: 34969888)."""
    case = next(c for c in CASES if c["key"] == "domria:34969888")
    host, v = _classify(case)
    assert v.signature == "ria_archive"
    assert v.source_removed_at.isoformat() == "2026-10-05T23:00:05"
    realty = {"deleted_at": "2026-10-06 02:00:05"}
    assert ria_deleted_at_utc(realty, CFG.ria_page).isoformat() == "2026-10-05T23:00:05"


def _ria(code=200, body=None, chain=(), key="domria:34000001", **kw):
    url = "https://dom.ria.com/uk/realty-prodaja-kvartira-ivano-frankovsk-34000001.html"
    res = ProbeResult(code=code, method="GET", url=url, final_url=kw.pop("final", url),
                      chain=chain, body=body, error=kw.pop("error", None))
    return classify(CFG.hosts["dom.ria.com"], CFG.ria_page, key, res)


def test_ria_state_and_banner_must_agree():
    """Стан «archive» без банера, банер без стану, чужий id, сторінка без стану —
    «не визначено», жодного «знято». CSS-правило .bg.m-sold — не банер."""
    assert _ria(body=ria_page(34000001, archived=True)).kind == REMOVED
    no_banner = _ria(body=ria_page(34000001, archived=True, banner=False))
    assert (no_banner.kind, no_banner.signature) == (UNKNOWN, "conflict")
    banner_only = _ria(body=ria_page(34000001, archived=False, banner=True))
    assert (banner_only.kind, banner_only.signature) == (UNKNOWN, "conflict")
    stateless = _ria(body=ria_page(34000001, archived=True, state=False))
    assert stateless.kind == UNKNOWN
    other = _ria(body=ria_page(34999999, archived=True))
    assert (other.kind, other.signature) == (UNKNOWN, "id_mismatch")
    plain = _ria(body="<html><body><div>капча</div></body></html>")
    assert (plain.kind, plain.signature) == (UNKNOWN, "unrecognized")
    redirected = _ria(body=ria_page(34000001, archived=True),
                      chain=((301, "https://dom.ria.com/realty-prodaja-kvartira-34000001.html"),))
    assert redirected.kind == UNKNOWN, "знято — лише 200 без переадресації"


def test_complaint_phrase_on_a_live_page_is_not_a_signal():
    """«Оголошення неактуальне чи інформація неточна?» — кнопка скарги на ЖИВИХ сторінках."""
    page = ria_page(34000001)
    assert "неактуальне" in page
    assert _ria(body=page).kind == ALIVE


@pytest.mark.parametrize("host,url,key", [
    ("dom.ria.com", "https://dom.ria.com/uk/realty-prodaja-kvartira-a-34000001.html",
     "domria:34000001"),
    ("olx.ua", "https://www.olx.ua/d/uk/obyavlenie/kvartyra-ID10abcD.html", "olx:10abcD"),
    ("rieltor.ua", "https://rieltor.ua/ivano-frankovsk/flats-sale/view/13000001/",
     "rieltor:13000001"),
    ("lun.ua", "https://lun.ua/uk/realty/4700000001", "lun:4700000001"),
    ("flombu.com", "https://www.flombu.com/uk/estate_deal_sales/116000", "flombu:116000"),
])
def test_single_404_is_not_removal_on_any_host(host, url, key):
    spec = CFG.hosts[host]
    direct = classify(spec, CFG.ria_page, key, ProbeResult(404, spec.method, url, url))
    assert (direct.kind, direct.signature) == (NOT_FOUND, "not_found")
    assert direct.alive_flag is None
    # 301 → 404 на тому самому ключі (flombu → www, lun.ua → http → https) — теж лише «не знайдено».
    via = classify(spec, CFG.ria_page, key,
                   ProbeResult(404, spec.method, url, url, chain=((301, url),)))
    assert via.kind == NOT_FOUND


@pytest.mark.parametrize("code,error,signature", [
    (403, None, "blocked"), (429, None, "blocked"), (401, None, "blocked"),
    (0, "ConnectError", "net_error"), (0, "timeout", "net_error"),
    (500, None, "server_error"), (503, None, "server_error"),
    (200, "too_large", "too_large"), (200, "timeout", "net_error"),
])
def test_captcha_block_empty_and_error_pages_are_unknown(code, error, signature):
    v = _ria(code=code, error=error, body=None)
    assert (v.kind, v.signature) == (UNKNOWN, signature)
    head = classify(CFG.hosts["olx.ua"], CFG.ria_page, "olx:10abcD",
                    ProbeResult(code, "HEAD", "https://www.olx.ua/x-ID10abcD.html",
                                "https://www.olx.ua/x-ID10abcD.html", error=error))
    assert head.kind == UNKNOWN
    assert _ria(body="").kind == UNKNOWN, "порожня сторінка 200 — не «живе»"


def test_redirect_to_other_ad_or_catalog_is_not_alive():
    olx = CFG.hosts["olx.ua"]
    url = "https://www.olx.ua/d/uk/obyavlenie/kvartyra-ID10abcD.html"
    other = ProbeResult(200, "HEAD", url, "https://www.olx.ua/d/uk/obyavlenie/insha-ID10zzzZ.html",
                        chain=((301, url),))
    assert classify(olx, CFG.ria_page, "olx:10abcD", other).signature == "id_mismatch"
    home = ProbeResult(200, "HEAD", url, "https://www.olx.ua/uk/", chain=((302, url),))
    assert classify(olx, CFG.ria_page, "olx:10abcD", home).kind == UNKNOWN
    renamed = ProbeResult(200, "HEAD", url,
                          "https://www.olx.ua/d/uk/obyavlenie/nova-nazva-ID10abcD.html",
                          chain=((301, url),))
    assert classify(olx, CFG.ria_page, "olx:10abcD", renamed).kind == ALIVE
    flombu = CFG.hosts["flombu.com"]
    f_url = "https://www.flombu.com/uk/estate_deal_sales/116000"
    to_home = ProbeResult(200, "HEAD", f_url, "https://www.flombu.com/uk", chain=((301, f_url),))
    assert classify(flombu, CFG.ria_page, "flombu:116000", to_home).kind == UNKNOWN


def test_blago_is_not_checkable_and_its_catalog_redirect_never_counts():
    blago = [c for c in CASES if c["host"] == "blagodeveloper.com"]
    assert blago and all(c["chain"] for c in blago), "кожна адреса планування веде на каталог"
    assert not CFG.hosts["blagodeveloper.com"].checkable
    assert not pol.is_checkable(CFG, blago[0]["url"])


def test_config_refuses_a_single_404_as_removal(tmp_path, monkeypatch):
    """404 у removed_statuses схема не приймає (рішення власника 1, D46)."""
    import shutil

    for f in (pol.configfiles.ROOT / "config").glob("*.toml"):
        shutil.copy(f, tmp_path / f.name)
    path = tmp_path / "liveness.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('removed_statuses = [410]\nnot_found_statuses = [404]\n'
                                 'existence = ["feed"',
                                 'removed_statuses = [410, 404]\nnot_found_statuses = []\n'
                                 'existence = ["feed"', 1), encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(tmp_path))
    with pytest.raises(configfiles.ConfigError, match="404"):
        configfiles.load("liveness")
