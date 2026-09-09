from base64 import b64encode

from fastapi.testclient import TestClient

from family_ea.config import Settings
from family_ea.db import Database
from family_ea.people import People
from family_ea.web import build_web
from tests.conftest import KYIV


def _settings(**kw) -> Settings:
    base = dict(
        telegram_token=None,
        anthropic_api_key=None,
        openai_api_key=None,
        database_path=":memory:",
        family_yaml="family.yaml",
        web_user="u",
        web_password="p",
        web_url=None,
        llm_model="m",
        llm_effort="medium",
        port=8080,
        tz=KYIV,
    )
    base.update(kw)
    return Settings(**base)


def _auth(user: str = "u", password: str = "p") -> dict[str, str]:
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


def test_web_pages(db: Database, people: People) -> None:
    mid = db.insert_message("oleh", "oleh", "Газовик Петро", tg_message_id=1)
    db.set_llm_result(mid, '{"output": {"reply": "Записав."}}')
    db.insert_message("bot", "oleh", "Записав.")
    db.create_memory("Газовик Петро замінив клапан", "oleh", mid)
    db.create_commitment(
        "Стоматолог",
        owner="anna",
        created_by="anna",
        source_message_id=mid,
        due_from="2000-01-01",
    )
    client = TestClient(build_web(_settings(), people, db))

    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_auth("u", "wrong")).status_code == 401

    home = client.get("/", headers=_auth())
    assert home.status_code == 200
    assert "Стоматолог" in home.text and "Анна" in home.text and "Газовик" in home.text

    search = client.get("/", params={"q": "котл"}, headers=_auth())
    assert "нічого" in search.text  # no memory mentions "котл..."
    search = client.get("/", params={"q": "газов"}, headers=_auth())
    assert "замінив клапан" in search.text

    messages = client.get("/messages", headers=_auth())
    assert "бот → Олег" in messages.text and "Записав." in messages.text


def test_web_refuses_without_configured_auth(db: Database, people: People) -> None:
    client = TestClient(build_web(_settings(web_user=None, web_password=None), people, db))
    assert client.get("/", headers=_auth()).status_code == 503
