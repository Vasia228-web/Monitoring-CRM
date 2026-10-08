"""Спільне для тестів доказів типу продавця (Блок 3, E11, D60): синтетичні сторінки й
payload тієї самої будови, що й Етап 0 (probes/_lun_sample.json, _olx_detail.html,
_rieltor_detail.html), з іменами й номерами, яких у базі бути не повинно. Імена —
«Тест Тестович», номери — нулі (067 000 00 01)."""
from __future__ import annotations

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
                 agency: str | None = "https://tstagencia.rieltor.ua/",
                 zhk: str = "ЖК Паркова Алея") -> str:
    ag = (f'<a class="offer-view-rieltor-agency-link" href="{agency}">Прогрес</a>'
          if agency else "")
    return (f"<html><head><title>Оголошення №{n} — продаж квартири</title></head><body>"
            f'<div class="offer-view-section-text">Опис. Тел. {PHONE}</div>'
            f'<div class="offer-view-rieltor-name">{NAME}</div>'
            f'<div class="offer-view-rieltor-position">{role}</div>{ag}'
            f'<div class="ldb__complex"><div class="ldb__complex-name">{zhk}</div></div>'
            "</body></html>")


def olx_tab_html(tab_label: str | None, tokens, *, promoted=(), next_page: bool = False) -> str:
    """Сторінка вкладки пошуку OLX: активна кнопка (data-is-active) і картки."""
    labels = ["Всі оголошення", "Бізнес", "Приватні"]
    buttons = "".join(
        f'<button data-is-active="{"true" if lab == (tab_label or "Всі оголошення") else "false"}">'
        f"<span>{lab}</span></button>" for lab in labels)
    cards = "".join(
        f'<div data-cy="l-card" data-testid="l-card"><a href="/d/uk/obyavlenie/kv-ID{tok}.html'
        f'?search_reason=search%7C{"promoted" if tok in promoted else "organic"}">'
        f'<h6>кв {tok}</h6></a></div>' for tok in [*promoted, *tokens])
    nxt = ('<a data-testid="pagination-forward" href="?page=2">Вперед</a>' if next_page else "")
    return (f'<html><body><div data-testid="top-listing-filters"><header>{buttons}</header>'
            f"</div>{cards}{nxt}</body></html>")
