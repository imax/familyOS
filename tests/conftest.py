from zoneinfo import ZoneInfo

import pytest

from family_ea.db import Database
from family_ea.people import People, Person

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def people() -> People:
    return People([Person("oleh", "Олег", telegram_id=1), Person("anna", "Анна", telegram_id=2)])


@pytest.fixture
def oleh(people: People) -> Person:
    p = people.get("oleh")
    assert p
    return p
