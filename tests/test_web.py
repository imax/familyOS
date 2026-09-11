from datetime import UTC, datetime, time

import pytest
from fastapi.testclient import TestClient

from family_ea.auth import LINK_TTL, SESSION_TTL, sign
from family_ea.config import Settings
from family_ea.db import Database, Member
from family_ea.family import Family
from family_ea.web import build_web
from tests.conftest import KYIV


def _settings(**kw) -> Settings:
    base = dict(
        telegram_token=None,
        anthropic_api_key=None,
        openai_api_key=None,
        database_path=":memory:",
        admin_user_id=1,
        web_secret="s",
        web_url=None,
        llm_model="m",
        llm_effort="medium",
        digest_time=time(8, 30),
        port=8080,
        tz=KYIV,
    )
    base.update(kw)
    return Settings(**base)


def _auth(member: str = "oleh") -> dict[str, str]:
    """A request header carrying a valid session cookie for `member`."""
    return {"Cookie": f"session={sign('s', 'session', member, SESSION_TTL)}"}


def freeze_web_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """The home page places things relative to now; pin it so tests do not drift."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now.astimezone(tz) if tz else now

    monkeypatch.setattr("family_ea.web.datetime", _Frozen)


def test_web_pages(db: Database, family: Family) -> None:
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
    client = TestClient(build_web(_settings(), family, db))

    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Cookie": "session=garbage"}).status_code == 401

    home = client.get("/", headers=_auth())
    assert home.status_code == 200
    assert "Стоматолог" in home.text and "Анна" in home.text
    assert "Газовик" in client.get("/journal", headers=_auth()).text
    assert ">Задачі</a>" in home.text and 'class="current">Задачі' in home.text
    assert "/memories" not in home.text  # the tab is Journal now
    inventory = client.get("/inventory", headers=_auth())
    assert inventory.status_code == 200 and 'class="current">Inventory' in inventory.text
    assert client.get("/inventory").status_code == 401

    search = client.get("/", params={"q": "котл"}, headers=_auth())
    assert "нічого" in search.text  # no memory mentions "котл..."
    search = client.get("/", params={"q": "газов"}, headers=_auth())
    assert "замінив клапан" in search.text and 'class="id"' not in search.text

    messages = client.get("/messages", headers=_auth())
    assert "бот → Олег" in messages.text and "Записав." in messages.text


def test_web_refuses_without_configured_auth(db: Database, family: Family) -> None:
    client = TestClient(build_web(_settings(web_secret=None), family, db))
    assert client.get("/", headers=_auth()).status_code == 503


def test_family_web_add_and_edit(db: Database, family: Family) -> None:
    client = TestClient(build_web(_settings(), family, db))
    page = client.get("/family", params={"name": "Оля", "telegram_id": "3"}, headers=_auth())
    assert page.status_code == 200
    assert 'value="Оля"' in page.text and "oleh" in page.text and "Анна" in page.text

    def post(data: dict[str, str]):
        return client.post("/family", data=data, headers=_auth(), follow_redirects=False)

    r = post({"name": " Оля ", "telegram_id": " 3 "})
    assert r.status_code == 303 and r.headers["location"] == "/family"
    assert family.by_telegram_id(3) == Member("olia", "Оля", 3)

    r = post({"id": "olia", "name": "Ольга", "telegram_id": ""})
    assert r.headers["location"] == "/family"
    assert family.get("olia") == Member("olia", "Ольга", None)

    assert "error=" in post({"name": "Дубль", "telegram_id": "1"}).headers["location"]
    assert "error=" in post({"name": "Хтось", "telegram_id": "abc"}).headers["location"]
    assert "error=" in post({"name": "", "telegram_id": "5"}).headers["location"]
    assert post({"id": "ghost", "name": "x"}).status_code == 404
    assert [m.id for m in family.members] == ["oleh", "anna", "olia"]

    page = client.get("/family", params={"error": "Тест"}, headers=_auth())
    assert 'class="error">Тест' in page.text
    assert client.post("/family", data={"name": "x"}).status_code == 401


def test_web_ics(db: Database, family: Family) -> None:
    mid = db.insert_message("anna", "anna", "...")
    timed = db.create_commitment(
        "Стоматолог",
        owner="anna",
        created_by="anna",
        source_message_id=mid,
        due_at="2026-09-10T12:30:00Z",
    )
    undated = db.create_commitment("Без дати", owner=None, created_by="anna", source_message_id=mid)
    client = TestClient(build_web(_settings(), family, db))

    home = client.get("/", headers=_auth())
    assert f'href="/commitments/{timed}.ics"' in home.text
    assert f'href="/commitments/{undated}.ics"' not in home.text

    ics = client.get(f"/commitments/{timed}.ics", headers=_auth())
    assert ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar")
    assert "DTSTART:20260910T123000Z" in ics.text
    assert 'filename="stomatoloh.ics"' in ics.headers["content-disposition"]
    assert client.get(f"/commitments/{undated}.ics", headers=_auth()).status_code == 404
    assert client.get("/commitments/999.ics", headers=_auth()).status_code == 404
    assert client.get(f"/commitments/{timed}.ics").status_code == 401


def test_login_from_a_bot_link(db: Database, family: Family) -> None:
    app = build_web(_settings(web_url="https://ea.example"), family, db)
    client = TestClient(app, base_url="https://testserver")  # a Secure cookie needs https
    r = client.get("/")  # not logged in: a page for a person, not JSON
    assert r.status_code == 401 and "напиши боту /web" in r.text

    token = sign("s", "link", "anna", LINK_TTL)
    r = client.get("/login", params={"t": token, "next": "/facts"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/facts"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("session=") and "HttpOnly" in cookie and "Secure" in cookie
    assert "Max-Age=31536000" in cookie and "SameSite=lax" in cookie
    assert client.get("/").status_code == 200  # the client keeps the cookie
    assert "Анна" in client.get("/family").text

    # once the browser is logged in, a stale or foreign link just opens the page
    r = client.get("/login", params={"t": "garbage"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_rejects_bad_links(db: Database, family: Family) -> None:
    client = TestClient(build_web(_settings(), family, db))
    stale = sign("s", "link", "anna", LINK_TTL, now=datetime(2000, 1, 1, tzinfo=UTC))
    bad = [
        "",
        "garbage",
        stale,
        sign("s", "session", "anna", SESSION_TTL),  # a cookie is not a link
        sign("s", "link", "ghost", LINK_TTL),  # not in the family
        sign("other", "link", "anna", LINK_TTL),
    ]
    for t in bad:
        r = client.get("/login", params={"t": t})
        assert r.status_code == 403 and "застаріло" in r.text, t
    assert not client.cookies
    assert client.get("/", headers=_auth("ghost")).status_code == 401
    link_as_cookie = {"Cookie": f"session={sign('s', 'link', 'anna', LINK_TTL)}"}
    assert client.get("/", headers=link_as_cookie).status_code == 401
    # no open redirects
    good = sign("s", "link", "anna", LINK_TTL)
    r = client.get("/login", params={"t": good, "next": "//evil.example/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
