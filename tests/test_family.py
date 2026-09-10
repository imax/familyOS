import pytest

from family_ea.db import Database
from family_ea.family import Family, slugify


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Олег", "oleh"),
        ("Анна", "anna"),
        ("пані Марія", "pani_mariia"),
        ("Юхим", "yukhym"),
        ("Їжак Й", "yizhak_y"),
        ("Ілля", "illia"),
        ("Tom", "tom"),
        ("Tom O'Brien-Smith", "tom_obrien_smith"),
        ("123", "m_123"),
        ("", "member"),
        ("  ", "member"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_family_add_lookup_update(db: Database) -> None:
    fam = Family(db, admin_telegram_id=1)
    assert fam.members == [] and fam.describe() == ""
    assert fam.is_admin(1) and not fam.is_admin(2)

    oleh = fam.add("Олег", 1)
    anna = fam.add(" Анна ", 2)
    assert (oleh.id, anna.id, anna.name) == ("oleh", "anna", "Анна")
    assert fam.by_telegram_id(2) == anna and fam.by_telegram_id(999) is None
    assert fam.get("oleh") == oleh and fam.get("ghost") is None
    assert fam.display_name("oleh") == "Олег"
    assert fam.display_name("ghost") == "ghost"
    assert fam.display_name(None) == "—"
    assert fam.describe() == "- oleh: Олег\n- anna: Анна"

    # same name twice: ids stay unique; a telegram id cannot be shared
    assert fam.add("Анна").id == "anna2"
    with pytest.raises(ValueError):
        fam.add("Хтось", 2)
    with pytest.raises(ValueError):
        fam.add("Хтось", member_id="Bad Id")
    with pytest.raises(ValueError):
        fam.add("   ")
    assert [m.id for m in fam.members] == ["oleh", "anna", "anna2"]

    assert fam.update("anna2", name="Аня", telegram_id=3) is True
    assert fam.by_telegram_id(3).name == "Аня"
    assert fam.update("ghost", name="x", telegram_id=None) is False
    with pytest.raises(ValueError):
        fam.update("anna2", name="Аня", telegram_id=1)
    with pytest.raises(ValueError):
        fam.update("anna2", name="", telegram_id=3)
    assert fam.by_telegram_id(3).name == "Аня"  # failed updates change nothing


def test_family_without_admin(db: Database) -> None:
    fam = Family(db)
    assert fam.is_admin(1) is False
    assert fam.add("Олег").telegram_id is None
