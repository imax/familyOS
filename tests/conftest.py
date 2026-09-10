from zoneinfo import ZoneInfo

import pytest

from family_ea.db import Database, Member
from family_ea.family import Family

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def family(db: Database) -> Family:
    fam = Family(db, admin_telegram_id=1)
    fam.add("Олег", 1, member_id="oleh")
    fam.add("Анна", 2, member_id="anna")
    return fam


@pytest.fixture
def oleh(family: Family) -> Member:
    p = family.get("oleh")
    assert p
    return p
