from zoneinfo import ZoneInfo

import pytest

from family_ea.db import Database
from family_ea.family import Family, Member

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def family() -> Family:
    return Family([Member("oleh", "Олег", telegram_id=1), Member("anna", "Анна", telegram_id=2)])


@pytest.fixture
def oleh(family: Family) -> Member:
    p = family.get("oleh")
    assert p
    return p
