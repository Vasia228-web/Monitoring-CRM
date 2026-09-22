"""«Дублі» під увімкненою перевіркою зовнішніх ключів (з 21.09.2026).

Перебудова квартир видаляє всі й створює заново; оголошення на них посилаються.
З негайною перевіркою ключів `DELETE FROM properties` падав — крок «дублі»
відкочувався щоциклу, і нові оголошення лишались незведеними.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from realty.dedup import rebuild
from realty.models import Base, Condition, Listing, MarketType, Property


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fk.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _on(conn, _r):
        conn.execute("PRAGMA foreign_keys = ON")      # як у робочій базі

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _listing(i, source, **kw):
    now = datetime(2026, 9, 21)
    base = dict(source=source, external_id=str(i), original_url=f"https://{source}/{i}",
                price=65000, currency="USD", price_usd=65000.0, rooms=2, area_total=55.0,
                floor=5, floors_total=9, location="вул. Незалежності, 146",
                market_type=MarketType.SECONDARY, condition=Condition.RENOVATED,
                first_seen=now, last_seen=now, quality_status="ok")
    base.update(kw)
    return Listing(**base)


def _seed(Session):
    with Session() as s:
        s.add_all([
            _listing(1, "domria"),
            _listing(2, "lun"),                                   # та сама квартира
            _listing(3, "olx", rooms=1, area_total=38.0, floor=2,
                     location="вул. Стуса, 30", price=41000, price_usd=41000.0),
        ])
        s.commit()
        rebuild(s)
        s.commit()


def test_old_way_fails_under_foreign_keys(tmp_path):
    """Регресія: без відкладеної перевірки видалення квартир відхиляється."""
    Session = _session(tmp_path)
    _seed(Session)
    with Session() as s:
        with pytest.raises(IntegrityError):
            s.query(Property).delete()
            s.flush()


def test_rebuild_works_under_foreign_keys_and_keeps_processing_marks(tmp_path):
    Session = _session(tmp_path)
    _seed(Session)
    with Session() as s:
        s.get(Listing, 3).in_progress = True               # ріелтор узяв квартиру
        s.add(_listing(4, "flombu"))                         # нове оголошення тієї ж квартири
        s.commit()

    with Session() as s:
        rebuild(s)                                           # те, що падало
        s.commit()

    with Session() as s:
        assert s.scalar(select(func.count()).select_from(Listing)
                        .where(Listing.property_id.is_(None))) == 0      # усі зведені
        groups = {l.id: l.property_id for l in s.scalars(select(Listing))}
        assert groups[1] == groups[2] == groups[4]           # три сайти → одна квартира
        assert groups[3] != groups[1]
        assert s.get(Listing, 3).in_progress is True         # позначка обробки на місці
        assert s.execute(text("PRAGMA foreign_key_check")).fetchall() == []
