import pytest

from family_ea.family import Family


def test_from_env_parses_two_members() -> None:
    family = Family.from_env(" oleh:123456:Олег, anna:234567:Анна ")
    assert [p.id for p in family.members] == ["oleh", "anna"]
    assert family.by_telegram_id(234567).name == "Анна"
    assert family.by_telegram_id(999) is None
    assert family.telegram_ids == [123456, 234567]
    assert family.display_name("oleh") == "Олег"
    assert family.display_name("ghost") == "ghost"
    assert family.display_name(None) == "—"
    assert family.describe() == "- oleh: Олег\n- anna: Анна"


@pytest.mark.parametrize(
    "spec",
    ["", "oleh:123456", "Oleh:123456:Олег", "oleh:abc:Олег", "oleh::Олег"],
)
def test_from_env_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        Family.from_env(spec)
