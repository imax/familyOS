from datetime import UTC, datetime

import pytest

from family_ea.ical import ics_filename, todo_ics
from tests.test_context import _c


def test_dated_todo_is_all_day() -> None:
    t = _c(1, text="Стоматолог, Оля; 2 год", due="2026-09-11")
    data = todo_ics(t, now=datetime(2026, 9, 10, 5, 0, tzinfo=UTC)).decode()
    lines = data.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and data.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20260911" in lines and "DTEND;VALUE=DATE:20260912" in lines
    assert "DTSTAMP:20260910T050000Z" in lines
    assert "UID:todo-1@family-ea" in lines
    assert "SUMMARY:Стоматолог\\, Оля\\; 2 год" in lines
    assert ics_filename(t.text) == "stomatoloh_olia_2_hod.ics"


def test_folding_and_no_deadline() -> None:
    lines = todo_ics(_c(5, text="я" * 100, due="2026-09-08")).decode().split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines)
    i = next(n for n, line in enumerate(lines) if line.startswith("SUMMARY:"))
    unfolded = lines[i]
    for line in lines[i + 1 :]:
        if not line.startswith(" "):
            break
        unfolded += line[1:]
    assert unfolded == "SUMMARY:" + "я" * 100
    with pytest.raises(ValueError):
        todo_ics(_c(6))
