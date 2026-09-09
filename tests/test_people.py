import pytest

from family_ea.people import People


def test_from_env_parses_two_people() -> None:
    people = People.from_env(" oleh:123456:Олег, anna:234567:Анна ")
    assert [p.id for p in people.family] == ["oleh", "anna"]
    assert people.by_telegram_id(234567).name == "Анна"
    assert people.by_telegram_id(999) is None
    assert people.telegram_ids == [123456, 234567]
    assert people.display_name("oleh") == "Олег"
    assert people.display_name("ghost") == "ghost"
    assert people.display_name(None) == "—"
    assert people.describe() == "- oleh: Олег\n- anna: Анна"


@pytest.mark.parametrize(
    "spec",
    ["", "oleh:123456", "Oleh:123456:Олег", "oleh:abc:Олег", "oleh::Олег"],
)
def test_from_env_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        People.from_env(spec)
