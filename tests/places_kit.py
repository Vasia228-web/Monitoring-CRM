"""Спільне для тестів Блоку 4 (райони й ЖК, E10, D57): синтетична база з доказами місця
з усіх джерел, прогін кроку «райони й ЖК» і сайт на ній (власник і друг).

Назви — з робочого довідника config/places/ (тести перевіряють саме його), номери й
імена — синтетичні.
"""
from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from realty.models import Base, Condition, Listing, MarketType, Property  # noqa: F401

NOW = datetime(2026, 10, 7, 8, 0, 0)

# Докази місця за джерелами: (джерело, поля рядка).
EVIDENCE = [
    ("domria", dict(district="Центр", complex_name="ЖК Comfort Park",
                    identity={"complex": "ria:5902"})),               # за ЖК → Пасічна
    ("domria", dict(district="Пасічна")),
    ("domria", dict(district="Каскад", complex_name="ЖК SHEPIT",
                    identity={"complex": "ria:6420"})),                # id за назвою
    ("domria", dict(district="Набережна", complex_name="ЖК Manhattan Up",
                    identity={"complex": "ria:7845"})),
    ("domria", dict(district="Княгинин", complex_name="ЖК Kniahynyn-Center",
                    identity={"complex": "ria:8216"})),
    ("domria", dict(district="Княгинин", complex_name="ЖК Княгинин",
                    identity={"complex": "ria:7504"})),                # парасолька
    ("domria", dict(district="", complex_name="ЖК Невідомий Двір",
                    identity={"complex": "ria:999999"})),              # нерозпізнана
    ("lun", dict(location="Хіміків вул., 28, к.1-6, Пасiчна", district="ТЦ \"Арсен\"")),
    ("lun", dict(location="Кераміків вул., 26, Кладовище, Крихівці (Івано-Франківськ)",
                 district="Міське озеро ")),
    ("lun", dict(location="Гетьмана Мазепи вул., 164, к4, Міське озеро",
                 district="ТЦ Панорама PLAZA")),                       # орієнтир — не район
    ("lun", dict(location="Галицька вул., 5, Німецька колонія", district="Стометрівка")),
    ("lun", dict(location="Лисець")),
    ("lun", dict(location="Івано-Франківськ", district="ТЦ \"Арсен\"")),
    ("lun", dict(location="Вовчинецька вул., 1, Вовчинець",
                 place_raw={"lun_geo": [{"type": "residential_complex", "id": 5,
                                         "name": "ЖК Comfort House"}]})),
    ("olx", dict(location="Івано-Франківськ", place_raw={"olx_zhk": "Comfort Park"})),
    ("olx", dict(location="Івано-Франківськ")),
    ("olx", dict(location="Івано-Франківськ", place_raw={"olx_zhk": "Зовсім Новий ЖК"})),
    ("blago", dict(complex_name="SKYGARDEN", location="ЖК SKYGARDEN")),
    ("flombu", dict(location="м. Івано-Франківськ",
                    place_raw={"flombu_locality": "Чукалівка"})),
    ("flombu", dict(location="м. Івано-Франківськ",
                    place_raw={"flombu_locality": "Івано-Франківськ"})),
]


def listing(i: int, pid: int | None, source: str, **kw) -> Listing:
    base = dict(source=source, external_id=f"{source}-{i}",
                original_url=f"https://example.test/{source}/{i}", price=50_000 + i,
                currency="USD", price_usd=50_000.0 + 137 * i, rooms=1 + i % 4,
                area_total=40.0 + i % 30, price_per_sqm=900.0 + i, floor=1 + i % 9,
                # Кожне оголошення — свій будинок: докази «за адресою» (інші квартири того
                # самого будинку) тести задають явно.
                floors_total=10, location=f"вул. Тестова, {i}",
                market_type=[MarketType.PRIMARY, MarketType.SECONDARY, MarketType.UNKNOWN][i % 3],
                condition=[Condition.RENOVATED, Condition.NEEDS_REPAIR][i % 2],
                published_at=NOW - timedelta(days=10 + i % 50),
                first_seen=NOW - timedelta(days=30), last_seen=NOW - timedelta(hours=i),
                quality_status="ok" if i % 9 else "review", is_active=bool(i % 13),
                property_id=pid, title=f"Квартира {i}")
    base.update(kw)
    return Listing(id=i, **base)


def build(engine, n: int = 160, seed: int = 20261008) -> None:
    """Квартири з 1–3 оголошень, докази місця — з EVIDENCE; частина без квартири."""
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    rnd = random.Random(seed)
    with Session() as s:
        i = 0
        pid = 0
        rows = []
        props = []
        while i < n:
            pid += 1
            k = rnd.choice((1, 1, 2, 3))
            src_ev = [rnd.choice(EVIDENCE) for _ in range(k)]
            no_property = rnd.random() < 0.1
            if not no_property:
                props.append(Property(id=pid, fingerprint=f"p{pid}", rooms=2,
                                      area_total=50.0, district="сирий район",
                                      first_seen=NOW - timedelta(days=30), last_seen=NOW))
            for src, ev in src_ev:
                i += 1
                kw = dict(ev)
                rows.append(listing(i, None if no_property else pid, src, **kw))
        s.add_all(props)
        s.flush()
        s.add_all(rows)
        s.commit()


def raw_checksum(engine) -> tuple:
    """Контрольна сума сирих полів, які Блок 4 не має чіпати ніколи."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, district, complex_name, location, title, last_seen, description, "
            "property_id, is_active, quality_status FROM listings ORDER BY id")).all()
    return tuple(tuple(r) for r in rows)


def assign(engine, *, dry_run: bool = False, d=None, rules=None, measure_weak=False,
           mode: str = "fill", include_lost: bool = False):
    """Прогін кроку на `engine` (той самий код, що й `cli.py places assign`)."""
    from realty import configfiles
    from realty.places import assign as step
    from realty.places import directory

    d = d or directory.load()
    rules = rules or configfiles.load("places/rules")
    extra = {} if mode == "fill" and not include_lost else {"mode": mode,
                                                             "include_lost": include_lost}
    return step.run(engine=engine, write_engine=engine, dry_run=dry_run, d=d, rules=rules,
                    rules_hash="test", measure_weak=measure_weak, now=NOW, **extra)


def rules_with_secondary():
    """Правила з увімкненим «не в ЖК» (у config/places/rules.toml вимкнено до перевіреної
    вибірки власника — рецензія E10), щоб перевіряти саму логіку."""
    from dataclasses import replace

    from realty import configfiles

    rules = configfiles.load("places/rules")
    return replace(rules, secondary=replace(rules.secondary, markets=("secondary",)))


@contextmanager
def scope_for(Session):
    s = Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def make_engine(tmp_path, name: str = "places.db"):
    return create_engine(f"sqlite:///{tmp_path / name}", future=True)
