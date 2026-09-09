from base64 import b64encode
from datetime import datetime

from fastapi.testclient import TestClient

from family_ea.context import build_context
from family_ea.db import Database
from family_ea.people import People, Person
from family_ea.web import build_web
from tests.conftest import KYIV
from tests.test_web import _settings


def _auth() -> dict[str, str]:
    return {"Authorization": "Basic " + b64encode(b"u:p").decode()}


def test_facts_versions(db: Database) -> None:
    assert db.current_facts() is None
    assert db.facts_versions() == 0
    db.save_facts("Живемо на Печерську.", "web")
    db.save_facts("Живемо на Печерську. Дитина в 3-Б.", "oleh")
    current = db.current_facts()
    assert current and current.text.endswith("3-Б.") and current.created_by == "oleh"
    assert db.facts_versions() == 2


def test_context_shows_facts_or_placeholder(db: Database, people: People, oleh: Person) -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, people, now, oleh, "привіт")
    assert "## Факти про сім'ю (веде людина, стабільний фон)\nпоки порожньо" in ctx
    db.save_facts("Класна керівниця — пані Марія.", "web")
    ctx = build_context(db, people, now, oleh, "привіт")
    assert "## Факти про сім'ю (веде людина, стабільний фон)\nКласна керівниця — пані Марія." in ctx


def test_facts_web_roundtrip(db: Database, people: People) -> None:
    client = TestClient(build_web(_settings(), people, db))
    page = client.get("/facts", headers=_auth())
    assert page.status_code == 200 and "Поки порожньо" in page.text

    saved = client.post(
        "/facts",
        data={"text": "Машина Skoda.\r\nСтраховка до квітня.  "},
        headers=_auth(),
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/facts"
    current = db.current_facts()
    assert current and current.created_by == "web"
    assert current.text == "Машина Skoda.\nСтраховка до квітня."

    # saving the same text again does not create a version
    client.post("/facts", data={"text": "Машина Skoda.\nСтраховка до квітня."}, headers=_auth())
    assert db.facts_versions() == 1

    page = client.get("/facts", headers=_auth())
    assert "Машина Skoda." in page.text and "Версія 1" in page.text
    assert client.post("/facts", data={"text": "x"}).status_code == 401
