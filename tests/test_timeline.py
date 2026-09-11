"""The web home: everything dated in one stream by day, undated commitments apart."""

import html
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from family_ea.context import build_timeline, day_title
from family_ea.db import Database, Reminder
from family_ea.family import Family
from family_ea.web import build_web
from tests.conftest import KYIV
from tests.test_context import _c
from tests.test_events import _e
from tests.test_web import _auth, _settings, freeze_web_clock

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=KYIV)  # Thursday afternoon


def _r(id: int, **kw) -> Reminder:
    base = dict(
        text=f"r{id}",
        who=None,
        at="2026-09-11T05:00:00Z",
        status="pending",
        created_by="oleh",
        created_at="2026-09-01T00:00:00Z",
        source_message_id=1,
        sent_at=None,
    )
    base.update(kw)
    return Reminder(id=id, **base)


def test_day_title() -> None:
    today = date(2026, 9, 10)
    assert day_title(today, today) == "Сьогодні, четвер 10.09"
    assert day_title(date(2026, 9, 11), today) == "Завтра, п'ятниця 11.09"
    assert day_title(date(2026, 10, 7), today) == "Середа 07.10"


def test_build_timeline(family: Family) -> None:
    events = [
        _e(
            1,
            text="Стоматолог",
            who="anna",
            starts_at="2026-09-11T12:30:00Z",
            until="2026-09-11T13:30:00Z",
        ),
        _e(2, text="Ранкова", starts_at="2026-09-10T07:00:00Z"),  # today 10:00, over: still today
        _e(3, text="Табір", date_from="2026-09-08", date_to="2026-09-19"),  # running: today, «до»
        _e(4, text="Гості", date_from="2026-09-12"),  # Saturday, all-day
        _e(5, text="Стрижка", starts_at="2026-10-07T12:30:00Z"),  # far ahead
        _e(6, text="Минуле", starts_at="2026-09-05T10:00:00Z"),  # over: not on the page
        _e(7, text="Скасоване", starts_at="2026-09-11T10:00:00Z", status="cancelled"),
    ]
    commitments = [
        _c(1, text="Квіти", owner="anna", due_at="2026-09-11T06:00:00Z"),  # tomorrow 09:00
        _c(2, text="Проспали", due_at="2026-09-10T04:00:00Z"),  # today 07:00, passed: overdue
        _c(3, text="Вікно", due_from="2026-09-08", due_to="2026-09-20"),  # open: today, «до»
        _c(4, text="Було до вчора", due_to="2026-09-09"),  # overdue, and first: oldest due
        _c(5, text="Майбутнє вікно", due_from="2026-09-12", due_to="2026-09-14"),
        _c(6, text="Без дати", owner="oleh"),
        _c(7, text="Закрите", status="done"),
    ]
    reminders = [
        _r(1, text="Квіти о 9", who="anna", at="2026-09-11T05:00:00Z"),  # tomorrow 08:00
        _r(2, text="Давно", at="2026-09-01T05:00:00Z"),  # pending but past: today
        _r(3, text="Надіслане", at="2026-09-11T05:00:00Z", status="sent"),
    ]
    t = build_timeline(events, commitments, reminders, NOW, family)

    assert [(r.id, r.note) for r in t.overdue] == [(4, "09.09"), (2, "10.09 07:00")]
    assert t.overdue[1].ics_url == "/commitments/2.ics" and t.overdue[1].time == ""

    assert [d.title for d in t.days] == [
        "Сьогодні, четвер 10.09",
        "Завтра, п'ятниця 11.09",
        "Субота 12.09",
        "Середа 07.10",
    ]
    today, tomorrow, saturday, october = t.days
    assert [(r.kind, r.id, r.time, r.note) for r in today.rows] == [
        ("event", 3, "", "до 19.09"),
        ("reminder", 2, "08:00", ""),
        ("event", 2, "10:00", ""),
        ("commitment", 3, "", "до 20.09"),
    ]
    assert [(r.kind, r.id, r.time, r.note, r.who) for r in tomorrow.rows] == [
        ("reminder", 1, "08:00", "", "Анна"),
        ("commitment", 1, "09:00", "", "Анна"),
        ("event", 1, "15:30", "до 16:30", "Анна"),
    ]
    assert today.rows[1].who == "усім"
    assert tomorrow.rows[1].ics_url == "/commitments/1.ics"
    assert tomorrow.rows[2].ics_url == "/events/1.ics"
    assert [(r.id, r.note) for r in saturday.rows] == [(4, ""), (5, "до 14.09")]
    assert [(r.kind, r.id, r.time) for r in october.rows] == [("event", 5, "15:30")]
    assert [(r.id, r.who, r.ics_url) for r in t.undated] == [(6, "Олег", None)]


def test_empty_timeline_keeps_today(family: Family) -> None:
    t = build_timeline([], [], [], NOW, family)
    assert t.overdue == [] and t.undated == []
    assert [(d.title, d.rows) for d in t.days] == [("Сьогодні, четвер 10.09", [])]


def test_web_home_is_a_timeline(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_web_clock(monkeypatch, NOW)
    mid = db.insert_message("oleh", "oleh", "...")
    db.create_event(
        "Стоматолог",
        who="anna",
        created_by="oleh",
        source_message_id=mid,
        starts_at="2026-09-11T12:30:00Z",
        until="2026-09-11T13:30:00Z",
    )
    db.create_reminder(
        "Стоматолог о 15:30",
        who=None,
        at="2026-09-11T11:30:00Z",
        created_by="oleh",
        source_message_id=mid,
    )
    db.create_commitment(
        "Купити квіти", owner="anna", created_by="oleh", source_message_id=mid, due_to="2026-09-09"
    )
    db.create_commitment(
        "Подзвонити газовику Петру", owner=None, created_by="oleh", source_message_id=mid
    )
    db.create_memory("Газовик Петро", "oleh", mid)
    client = TestClient(build_web(_settings(), family, db))

    home = html.unescape(client.get("/", headers=_auth()).text)  # «п'ятниця» is escaped
    assert home.index('<h2 class="overdue">Прострочено</h2>') < home.index("Купити квіти")
    assert "· 09.09 · Анна" in home
    assert home.index("Сьогодні, четвер 10.09") < home.index("Завтра, п'ятниця 11.09")
    assert home.index("Завтра") < home.index("14:30</span>") < home.index("15:30</span>")
    assert "Стоматолог <a" in home and "· до 16:30 · Анна" in home
    assert '<span class="mark">⏰</span>Стоматолог о 15:30' in home and "усім" in home
    assert '<li class="event">' in home and 'href="/events/1.ics"' in home
    assert home.index("<h2>Без дати</h2>") < home.index("☐</span>Подзвонити газовику Петру")
    assert "Газовик" not in home  # memories have their own page
    assert 'class="id"' not in home  # database ids are not for people

    memories = client.get("/journal", headers=_auth()).text
    assert "Газовик Петро" in memories and "Олег" in memories


def test_web_home_empty(db: Database, family: Family, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_web_clock(monkeypatch, NOW)
    client = TestClient(build_web(_settings(), family, db))
    home = client.get("/", headers=_auth()).text
    assert "Прострочено" not in home and "Завтра" not in home
    assert "Відпочиваємо :-)" in home  # today, empty
    assert home.count("нічого") == 1  # undated, empty
