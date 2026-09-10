"""An event or a dated commitment as an iCalendar file: one tap and it is in the phone calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from .context import DEFAULT_EVENT_DURATION, parse_iso
from .db import Commitment, Event
from .family import slugify

MAX_LINE_OCTETS = 74  # RFC 5545 folds at 75 octets; keep one for the continuation space


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """Split one content line into folded lines without cutting a UTF-8 character."""
    out: list[str] = []
    current, size = "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > MAX_LINE_OCTETS:
            out.append(current)
            current, size = " " + ch, 1 + n
        else:
            current += ch
            size += n
    out.append(current)
    return out


def _vcalendar(uid: str, summary: str, when: list[str], now: datetime | None) -> bytes:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Family EA//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        *when,
        f"SUMMARY:{_escape(summary)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    folded = [piece for line in lines for piece in _fold(line)]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")


def _timed(start_iso: str, end_iso: str | None) -> list[str]:
    start = parse_iso(start_iso).astimezone(UTC)
    end = parse_iso(end_iso).astimezone(UTC) if end_iso else start + DEFAULT_EVENT_DURATION
    return [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"]


def _all_day(first_iso: str | None, last_iso: str | None) -> list[str]:
    first = date.fromisoformat(first_iso or last_iso or "")
    last = date.fromisoformat(last_iso or first_iso or "")
    after = last + timedelta(days=1)  # DTEND of an all-day event is exclusive
    return [f"DTSTART;VALUE=DATE:{first:%Y%m%d}", f"DTEND;VALUE=DATE:{after:%Y%m%d}"]


def ics_filename(text: str) -> str:
    return f"{slugify(text)[:40]}.ics"


def event_ics(e: Event, now: datetime | None = None) -> bytes:
    """Timed: starts_at until `until` (or one hour). All-day: date_from..date_to inclusive."""
    when = _timed(e.starts_at, e.until) if e.starts_at else _all_day(e.date_from, e.date_to)
    return _vcalendar(f"event-{e.id}@family-ea", e.text, when, now)


def commitment_ics(c: Commitment, now: datetime | None = None) -> bytes:
    """A dated commitment: due_at as a one-hour slot, or the due window as all-day."""
    if not c.has_due:
        raise ValueError(f"commitment #{c.id} has no dates")
    when = _timed(c.due_at, None) if c.due_at else _all_day(c.due_from, c.due_to)
    return _vcalendar(f"commitment-{c.id}@family-ea", c.text, when, now)
