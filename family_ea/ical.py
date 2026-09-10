"""One commitment as an iCalendar file, so a dated item lands in a phone calendar in a tap."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from .context import parse_iso
from .db import Commitment
from .family import slugify

DEFAULT_DURATION = timedelta(hours=1)
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


def ics_filename(c: Commitment) -> str:
    return f"{slugify(c.text)[:40]}.ics"


def commitment_ics(c: Commitment, now: datetime | None = None) -> bytes:
    """A VCALENDAR with one VEVENT: timed (due_at, one hour) or all-day (due_from..due_to)."""
    if not c.has_due:
        raise ValueError(f"commitment #{c.id} has no dates")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    if c.due_at:
        start = parse_iso(c.due_at).astimezone(UTC)
        end = start + DEFAULT_DURATION
        when = [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"]
    else:
        first = date.fromisoformat(c.due_from or c.due_to or "")
        last = date.fromisoformat(c.due_to or c.due_from or "")
        after = last + timedelta(days=1)  # DTEND of an all-day event is exclusive
        when = [f"DTSTART;VALUE=DATE:{first:%Y%m%d}", f"DTEND;VALUE=DATE:{after:%Y%m%d}"]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Family EA//EN",
        "BEGIN:VEVENT",
        f"UID:commitment-{c.id}@family-ea",
        f"DTSTAMP:{stamp}",
        *when,
        f"SUMMARY:{_escape(c.text)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    folded = [piece for line in lines for piece in _fold(line)]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")
