from datetime import UTC, datetime

import pytest

from family_ea.ical import commitment_ics, ics_filename
from tests.test_context import _c


def test_timed_event() -> None:
    c = _c(1, text="Стоматолог, Оля; 2 год", due_at="2026-09-11T07:00:00Z")
    data = commitment_ics(c, now=datetime(2026, 9, 10, 5, 0, tzinfo=UTC)).decode()
    lines = data.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and data.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20260911T070000Z" in lines and "DTEND:20260911T080000Z" in lines
    assert "DTSTAMP:20260910T050000Z" in lines
    assert "UID:commitment-1@family-ea" in lines
    assert "SUMMARY:Стоматолог\\, Оля\\; 2 год" in lines
    assert ics_filename(c) == "stomatoloh_olia_2_hod.ics"


def test_all_day_window() -> None:
    lines = commitment_ics(_c(2, due_from="2026-09-08", due_to="2026-09-20")).decode().split("\r\n")
    assert "DTSTART;VALUE=DATE:20260908" in lines and "DTEND;VALUE=DATE:20260921" in lines
    single = commitment_ics(_c(3, due_from="2026-09-08")).decode()
    assert "DTSTART;VALUE=DATE:20260908" in single and "DTEND;VALUE=DATE:20260909" in single
    until = commitment_ics(_c(4, due_to="2026-09-12")).decode()
    assert "DTSTART;VALUE=DATE:20260912" in until and "DTEND;VALUE=DATE:20260913" in until


def test_folding_and_no_dates() -> None:
    lines = commitment_ics(_c(5, text="я" * 100, due_from="2026-09-08")).decode().split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines)
    i = next(n for n, line in enumerate(lines) if line.startswith("SUMMARY:"))
    unfolded = lines[i]
    for line in lines[i + 1 :]:
        if not line.startswith(" "):
            break
        unfolded += line[1:]
    assert unfolded == "SUMMARY:" + "я" * 100
    with pytest.raises(ValueError):
        commitment_ics(_c(6))
