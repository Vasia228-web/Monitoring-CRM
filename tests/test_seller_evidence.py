"""Докази типу продавця у звичайному зборі — білий список config/seller.toml (E11, D60).

Блок 3: LUN (payload стрічки), flombu (attributes JSON:API), OLX (сторінка оголошення:
чип «Приватна особа»/«Бізнес», «Тип угоди», профіль), rieltor.ua (картка: роль,
агенція). Нових запитів немає — це ті самі сторінки, що збір уже читає. Лише булеві й
коди; телефонів, імен, аватарів, адрес профілів і агенцій у базі немає ніколи;
наявні ключі не переписуються. На коді до E11 seller_evidence зі збору не було.
Фікстури — синтетичні: імена «Тест Тестович», номери — нулі (067 000 00 01).
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from realty import configfiles, pipeline  # noqa: E402
from realty.models import Base, Listing  # noqa: E402
from realty.pipeline import Pipeline  # noqa: E402
from realty.quality import rules  # noqa: E402
from realty.quality.staging import QualityGate  # noqa: E402

PHONE = "067 000 00 01"
NAME = "Тест Тестович"


def lun_obj(i: int = 1, *, url: str | None = None, **over) -> dict:
    """Об'єкт payload LUN тієї самої будови, що й Етап 0 (probes/_lun_sample.json), з
    контактами — їх у базі бути не повинно."""
    d = {"id": 4720690000 + i, "price": 60000, "currency": "usd", "roomCount": 2,
         "areaTotal": 55,
         "urlRaw": url or f"https://rieltor.ua/ivano-frankovsk/flats-sale/view/{13100000 + i}/",
         "geo": "вулиця Галицька, Івано-Франківськ", "location": [24.71, 48.92],
         "header": "2-кімнатна", "text": "Квартира в центрі",
         "isOwner": False, "isExclusive": None, "agency": None,
         "withoutCommission": False, "commissionRate": 3, "commissionType": "%",
         "phones": [PHONE], "hiddenPhones": ["067 *** ** 01"],
         "phonesInfo": [{"phone": PHONE, "hasTelegram": True, "hasViber": True,
                         "isAgency": True, "tgUsername": "test_seller"}],
         "rieltorContact": {"contactType": "rieltor", "viberPhone": PHONE, "phones": [PHONE],
                            "isVerified": True, "diia": True, "bankId": False, "name": NAME,
                            "avatar": "https://img.lun.ua/avatar/test.jpg",
                            "agency": {"name": "Агенція Тест", "url": "https://test.agency/"},
                            "activeOffers": 14, "lastLoginDate": "2026-10-01",
                            "startedWorkAt": "2020-01-01"},
         "site": {"siteId": 1, "displayName": "rieltor.ua"}}
    d.update(over)
    return d


def flombu_item(i: int = 1, **attrs) -> dict:
    a = {"type2HumanVal": "Квартира", "title": "2-к квартира", "price": 60000,
         "priceCurrency": "USD", "addressToStreet": "вул. Тестова, Івано-Франківськ",
         "addressLocalityHumanVal": "Івано-Франківськ", "estateSizeHumanVal": "55 м²",
         "tileEstateAccentAttrs": ["2 кімнати"], "ownerType": "agent",
         "ownerTypeHumanVal": "від посередника", "agentCommission": True,
         "agentCommissionType": "negotiable", "agentCommissionValue": None,
         "ownerPhoneId": "a1b2c3d4e5"}
    a.update(attrs)
    return {"id": 116000 + i, "attributes": a}


def olx_detail_html(*, chip: str = "Приватна особа", deal: str | None = "Переуступка, Від забудовника",
                    zhk: str | None = "ЖК Тестовий", profile: str = "/uk/list/user/2hNbPy/",
                    no_commission: bool = True) -> str:
    params = [f'<p><span>{chip}</span></p>', "<p>Вид об'єкта: Вторинний ринок</p>"]
    if no_commission:
        params.append("<p>Без комісії</p>")
    if deal:
        params.append(f"<p>Тип угоди: {deal}</p>")
    if zhk:
        params.append(f"<p>Назва ЖК: {zhk}</p>")
    params.append("<p>Кількість кімнат: 2 кімнати</p><p>Загальна площа: 55 м²</p>")
    return ('<html><body><h4 data-testid="offer_title">2-кімнатна</h4>'
            f'<div data-testid="ad-parameters-container">{"".join(params)}</div>'
            f'<div data-testid="ad_description">Опис. Дзвоніть {PHONE}</div>'
            f'<div data-testid="seller_card"><h4 data-testid="trader-title">{NAME}</h4>'
            f'<a data-testid="user-profile-link" href="{profile}">{NAME}</a></div>'
            "</body></html>")


def rieltor_html(n: int = 13100001, *, role: str = "Рієлтор",
                 agency: str | None = "https://tstagencia.rieltor.ua/") -> str:
    ag = (f'<a class="offer-view-rieltor-agency-link" href="{agency}">Прогрес</a>'
          if agency else "")
    return (f"<html><head><title>Оголошення №{n} — продаж квартири</title></head><body>"
            f'<div class="offer-view-section-text">Опис. Тел. {PHONE}</div>'
            f'<div class="offer-view-rieltor-name">{NAME}</div>'
            f'<div class="offer-view-rieltor-position">{role}</div>{ag}'
            '<div class="ldb__complex"><div class="ldb__complex-name">ЖК Паркова Алея</div></div>'
            "</body></html>")


@pytest.fixture
def iso(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'w.db'}", future=True)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False, future=True)

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

    monkeypatch.setattr(pipeline, "session_scope", scope)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    yield Session
    eng.dispose()


def _gate() -> QualityGate:
    wide = rules.Band(0, 1e9, 0, 1e9)
    return QualityGate(thresholds=rules.Thresholds(price_usd=wide, price_per_sqm=wide,
                                                   area_total=wide, sample_size=5000))


def _row_text(Session) -> str:
    """Усе, що лягло в рядок, — одним текстом (для пошуку імен і номерів)."""
    with Session() as s:
        rows = s.scalars(select(Listing)).all()
        return json.dumps([{c.name: getattr(r, c.name) for c in Listing.__table__.columns}
                           for r in rows], ensure_ascii=False, default=str)


# --- LUN і flombu: стрічка --------------------------------------------------------------------


def test_lun_feed_item_gives_whitelisted_codes_without_contacts():
    from realty.sources.lun import LunSource

    rec = LunSource()._parse(lun_obj())
    ev = rec["seller_evidence"]
    assert ev["lun_is_owner"] is False and ev["lun_contact_type"] == "rieltor"
    assert ev["lun_contact_has_agency"] is True and ev["lun_has_agency"] is False
    assert ev["lun_active_offers"] == 14 and ev["lun_phone_is_agency"] is True
    assert ev["lun_commission_rate"] == 3 and ev["lun_commission_type"] == "%"
    assert ev["lun_checked_at"]
    text = json.dumps(ev, ensure_ascii=False)
    for bad in ("067", NAME, "avatar", "test_seller", "Агенція", "2026-10-01"):
        assert bad not in text


def test_flombu_owner_type_but_never_the_phone_id():
    from realty.sources.flombu import FlombuSource

    rec = FlombuSource()._parse(flombu_item(), {})
    ev = rec["seller_evidence"]
    assert ev == {"flombu_owner_type": "agent", "flombu_agent_commission": True,
                  "flombu_commission_type": "negotiable",
                  "flombu_checked_at": ev["flombu_checked_at"]}
    # identity.seller = «flombu:<ownerPhoneId>» — давнє поле identity (відкрите питання 2
    # Блоку 3, не цей крок); у доказах продавця й профілі його немає.
    assert "a1b2c3d4e5" not in json.dumps(ev) and not rec.get("seller_profile")


def test_a_payload_with_phones_and_names_never_reaches_the_db(iso):
    """Рішення власника 5 і правило «телефони не збирати»: payload LUN із номерами,
    іменем, аватаром і Telegram ріелтора, сторінка rieltor з ім'ям агента й номером в
    описі, сторінка OLX з ім'ям продавця — у базі ні номера, ні імені."""
    from realty.sources.lun import LunSource
    from realty.sources.olx import OlxSource

    lun = LunSource()
    rec = lun.finalize(lun._parse(lun_obj(1)) | {"source": "lun"})
    rec = lun.enrich(rec, rieltor_html(13100001))
    olx = OlxSource()
    card = BeautifulSoup(
        '<div data-cy="l-card"><a href="/d/uk/obyavlenie/kvartyra-test-ID10BkYC.html">'
        '<h6 data-testid="ad-card-title">2-кімнатна квартира</h6></a>'
        '<p data-testid="ad-price">60 000 $</p>'
        '<p data-testid="location-date">Івано-Франківськ, Центр - Сьогодні о 10:00</p>'
        '<span>55 м²</span></div>', "lxml").select_one('[data-cy="l-card"]')
    orec = olx.enrich(olx.finalize(olx._parse(card) | {"source": "olx"}), olx_detail_html())
    Pipeline(sources=["lun", "olx"], use_llm=False, gate=_gate())._write([rec, orec])
    stored = _row_text(iso)
    assert "rieltor_role" in stored and "olx_chip" in stored     # докази є
    for bad in ("067", "000 00 01", NAME, "avatar", "test_seller", "2hNbPy", "tstagencia"):
        assert bad not in stored, bad
    with iso() as s:
        lrow = s.scalar(select(Listing).where(Listing.source == "lun"))
        orow = s.scalar(select(Listing).where(Listing.source == "olx"))
        assert lrow.seller_evidence["rieltor_role"] == "Рієлтор"
        assert lrow.seller_evidence["rieltor_agency"].startswith("rieltor:ag:")
        assert lrow.seller_evidence_at is not None
        assert orow.seller_evidence["olx_chip"] == "private"
        assert orow.seller_profile.startswith("olx:u:") and len(orow.seller_profile) == 6 + 16


def test_collection_adds_only_new_evidence_keys(iso):
    """Наявні ключі доказів не переписуються (FILL_ONLY_JSON): другий прохід стрічки з
    іншим contactType не змінює першого, але дописує ключ, якого ще не було."""
    from realty.sources.lun import LunSource

    lun = LunSource()
    first = lun.finalize(lun._parse(lun_obj(5)) | {"source": "lun"})
    first["seller_evidence"].pop("lun_active_offers")
    Pipeline(sources=["lun"], use_llm=False, gate=_gate())._write([first])
    obj = lun_obj(5)
    obj["rieltorContact"] = {**obj["rieltorContact"], "contactType": "owner"}
    second = lun.finalize(lun._parse(obj) | {"source": "lun"})
    Pipeline(sources=["lun"], use_llm=False, gate=_gate())._write([second])
    with iso() as s:
        ev = s.scalar(select(Listing)).seller_evidence
    assert ev["lun_contact_type"] == "rieltor" and ev["lun_active_offers"] == 14


# --- OLX і rieltor: сторінки ------------------------------------------------------------------


def test_olx_detail_chip_deal_flag_and_opaque_profile():
    from realty.sources import olx

    got = olx.parse_detail(olx_detail_html())
    ev = got["seller_evidence"]
    assert ev["olx_chip"] == "private" and ev["olx_no_commission"] is True
    assert ev["olx_deal"] == ["assignment", "from_developer"] and ev["olx_detail_at"]
    assert got["seller_profile"].startswith("olx:u:") and "2hNbPy" not in got["seller_profile"]
    assert got["place_raw"]["olx_zhk"] == "ЖК Тестовий"
    assert NAME not in json.dumps(got, ensure_ascii=False, default=str)
    # Той самий продавець — той самий непрозорий id; магазин — свій префікс.
    again = olx.parse_detail(olx_detail_html(chip="Бізнес", profile="/uk/list/user/2hNbPy/"))
    assert again["seller_profile"] == got["seller_profile"]
    assert again["seller_evidence"]["olx_chip"] == "business"
    shop = olx.parse_detail(olx_detail_html(chip="Бізнес", profile="https://kapital1.olx.ua/"))
    assert shop["seller_profile"].startswith("olx:shop:")


def test_olx_page_evidence_refuses_a_page_without_content():
    from realty.sources import olx

    assert olx.page_evidence("<html><body>captcha</body></html>") is None
    got = olx.page_evidence(olx_detail_html(zhk=None))
    assert got["place_raw"] == {"olx_checked_at": got["place_raw"]["olx_checked_at"]}
    assert set(got) == {"place_raw", "seller_evidence", "seller_profile"}


def test_rieltor_role_is_whitelisted_and_agency_is_opaque():
    from realty.sources import rieltor

    ev = rieltor.parse_detail(rieltor_html())["seller_evidence"]
    assert ev["rieltor_role"] == "Рієлтор" and ev["rieltor_has_agency"] is True
    assert ev["rieltor_agency"].startswith("rieltor:ag:") and "tstagencia" not in ev["rieltor_agency"]
    odd = rieltor.parse_detail(rieltor_html(role=NAME))["seller_evidence"]
    assert odd["rieltor_role"] == "other"                        # ім'я не зберігаємо
    phone_sub = rieltor.parse_detail(rieltor_html(agency="https://0670000001.rieltor.ua/"))
    ev2 = phone_sub["seller_evidence"]
    assert ev2["rieltor_has_agency"] is True and "rieltor_agency" not in ev2
    none = rieltor.parse_detail(rieltor_html(agency=None))["seller_evidence"]
    assert none["rieltor_has_agency"] is False


def test_rieltor_page_evidence_only_for_the_card_of_the_key():
    from realty.sources import rieltor

    assert rieltor.page_evidence(rieltor_html(13100001), "13100002") is None
    assert rieltor.page_evidence("<html><title> - RIELTOR.UA</title>410 Сторінка видалена</html>",
                                 "13100001") is None
    got = rieltor.page_evidence(rieltor_html(13100001), "13100001")
    assert got["place_raw"]["rieltor_zhk"] == "ЖК Паркова Алея"
    assert got["seller_evidence"]["rieltor_role"] == "Рієлтор"


# --- Схема ---------------------------------------------------------------------------------


def test_schema_refuses_paths_to_names_phones_and_contacts(tmp_path, monkeypatch):
    src = (ROOT / "config" / "seller.toml").read_text(encoding="utf-8")
    for bad in ('lun_name = "rieltorContact.name"', 'lun_tel = "phones"',
                'lun_tg = "phonesInfo[].tgUsername"', 'lun_av = "rieltorContact.avatar?"'):
        cfg = tmp_path / bad.split()[0]
        cfg.mkdir()
        (cfg / "seller.toml").write_text(
            src.replace('fields = { lun_is_owner = "isOwner"',
                        'fields = { ' + bad + ', lun_is_owner = "isOwner"'), encoding="utf-8")
        monkeypatch.setenv(configfiles.ENV_DIR, str(cfg))
        with pytest.raises(configfiles.ConfigError, match="не зберігаємо"):
            configfiles.load("seller")
    monkeypatch.delenv(configfiles.ENV_DIR)
    flombu = src.replace('flombu_owner_type = "ownerType"', 'flombu_phone = "ownerPhoneId"')
    cfg = tmp_path / "fl"
    cfg.mkdir()
    (cfg / "seller.toml").write_text(flombu, encoding="utf-8")
    monkeypatch.setenv(configfiles.ENV_DIR, str(cfg))
    with pytest.raises(configfiles.ConfigError, match="не зберігаємо"):
        configfiles.load("seller")


def test_opaque_id_never_keeps_a_phone_like_slug():
    from realty.seller import evidence

    cfg = configfiles.load("seller").profile
    assert evidence.opaque_id("olx:u:", "0670000001", cfg) is None
    a = evidence.opaque_id("olx:u:", "2hNbPy", cfg)
    assert a == evidence.opaque_id("olx:u:", "2HNBPY ", cfg) and a.startswith("olx:u:")
    # Рядок доказу з номером — не береться.
    assert evidence.clean(f"тел {PHONE}") is None and evidence.clean("rieltor") == "rieltor"
