"""Докази Блоків 3/4 з тієї самої відповіді перевірки — лише туди, де порожньо (E8, D52).

Інтеграція, конфлікт 3: один запит на ключ — сторінку DOM.RIA перевірка вже
завантажила й розібрала, тож район, ЖК, «пропозиція від» і профіль продавця беремо
з неї ж; білий список у config/liveness.toml [capture], лише скаляри. Імена й
телефони зі стану сторінки не беруться ніколи. Наявні значення не переписуються
(конфлікт 11) — ні гачком перевірки, ні записом збору (pipeline.FILL_ONLY_JSON).
На коді до E8 цих полів не було.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_kit import FakeNet, add, clean_fuse, db, get, ria_page, ria_url  # noqa: E402,F401

from realty import pipeline, verify  # noqa: E402

pytestmark = pytest.mark.usefixtures("clean_fuse")


def _page(rid):
    realty = {"district_id": 15700, "district_name_uk": "Центр", "user_newbuild_id": 6829,
              "characteristics_values": {"1437": 1506, "209": 2}, "user_id": 900123}
    html = ria_page(rid, extra_realty=realty)
    data_extra = (',"newbuildName":{"name":"ЖК Тестовий","url":""},'
                  '"agencyOwner":{"agency":{"agency_id":800123,"agency_type":4,'
                  '"name":"Агенція Тест"},"owner":{"name":"Іван Тестовий",'
                  '"phones":["0670000001"]}}')
    return html.replace('}}}};</script>', '}' + data_extra + '}}};</script>', 1)


def test_ria_page_fills_whitelisted_evidence_only_where_empty(db):
    rid = 34600001
    lid = add(db, ria_url(rid), source="domria", external_id=str(rid),
              place_raw={"ria_district": "з джерела"})
    page = _page(rid)
    assert json.loads(page.split("__INITIAL_STATE__=")[1].split(";</script>")[0])
    verify.verify_batch(ids=[lid], http=FakeNet({f"domria:{rid}": (200, page)}))
    row = get(db, lid)
    assert row.place_raw == {"ria_district": "з джерела", "ria_district_id": 15700,
                             "ria_newbuild_name": "ЖК Тестовий"}
    assert row.seller_evidence == {"ria_offer": 1506, "ria_agency_type": 4,
                                   "ria_has_agency": True}
    assert row.seller_profile == "ria:900123"
    assert row.seller_evidence_at is not None
    stored = json.dumps([row.place_raw, row.seller_evidence], ensure_ascii=False)
    assert "Іван" not in stored and "067" not in stored and "Агенція" not in stored


def test_zero_ids_are_not_evidence(db):
    """district_id = 0 у стані DOM.RIA — «немає»; записаний, він заступив би справжнє."""
    rid = 34600002
    lid = add(db, ria_url(rid), source="domria", external_id=str(rid))
    page = ria_page(rid, extra_realty={"district_id": 0, "district_name_uk": "Центр"})
    verify.verify_batch(ids=[lid], http=FakeNet({f"domria:{rid}": (200, page)}))
    assert get(db, lid).place_raw == {"ria_district": "Центр"}


def test_collection_never_overwrites_existing_evidence():
    """_upsert: нові ключі доказів — так, наявні — ніколи; профіль — лише якщо NULL."""
    class Row:
        seller_evidence = {"ria_offer": 1434}
        place_raw = None
        seller_profile = "ria:1"
        seller_evidence_at = None

    row = Row()
    pipeline._fill_only(row, {"seller_evidence": {"ria_offer": 1506, "ria_agency_type": 4},
                              "place_raw": {"ria_district": "Центр"},
                              "seller_profile": "ria:2"})
    assert row.seller_evidence == {"ria_offer": 1434, "ria_agency_type": 4}
    assert row.place_raw == {"ria_district": "Центр"}
    assert row.seller_profile == "ria:1"
