"""Apply LLM operations to the database: journal, events, commitments, reminders.

Invalid ops (unknown ids, closed items, bad dates, an event without a date, a reminder
without a time) are ignored and logged, never fatal. Closing a commitment goes through
`close_commitment`, cancelling an event through `cancel_event`, a reminder through
`cancel_reminder`; nothing else closes or cancels.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .db import Database
from .family import Family
from .llm import CommitmentOp, EventOp, JournalOp, LlmResult, ReminderOp

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Applied:
    kind: str  # 'entry' | 'event' | 'commitment' | 'reminder'
    op: str
    id: int | None
    ok: bool
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_datetime(value: str | None, tz: ZoneInfo) -> str | None:
    """LLM datetime (any offset, or naive = family tz) -> ISO UTC 'Z'. None if unparsable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def normalize_member(member_id: str | None, family: Family) -> str | None:
    if member_id is None:
        return None
    return member_id if any(p.id == member_id for p in family.members) else None


def _text(value: str | None) -> str | None:
    return value.strip() if value is not None and value.strip() else None


def _entry_fields(j: JournalOp) -> tuple[dict, list[str]]:
    """Validated fields present on the op. The text starts with a capital letter."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(j.text)) is not None:
        fields["text"] = text[:1].upper() + text[1:]
    if j.date is not None:
        value = normalize_date(j.date)
        if value is None:
            notes.append(f"bad date {j.date!r} dropped")
        else:
            fields["date"] = value
    return fields, notes


def _commitment_fields(c: CommitmentOp, family: Family, tz: ZoneInfo) -> tuple[dict, list[str]]:
    """Validated fields present on the op, plus notes about anything dropped."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(c.text)) is not None:
        fields["text"] = text
    if c.owner is not None:
        owner = normalize_member(c.owner, family)
        if owner is None:
            notes.append(f"unknown owner {c.owner!r} -> null")
        fields["owner"] = owner
    if c.due_at is not None:
        due_at = normalize_datetime(c.due_at, tz)
        if due_at is None:
            notes.append(f"bad due_at {c.due_at!r} dropped")
        else:
            fields["due_at"] = due_at
    for name in ("due_from", "due_to"):
        raw = getattr(c, name)
        if raw is not None:
            value = normalize_date(raw)
            if value is None:
                notes.append(f"bad {name} {raw!r} dropped")
            else:
                fields[name] = value
    # A specific time and a soft window are two forms of the same thing: the new one wins,
    # the old one goes, otherwise "moved to next week" keeps showing at the old time.
    if "due_at" in fields:
        fields["due_from"] = fields["due_to"] = None
    elif "due_from" in fields or "due_to" in fields:
        fields["due_at"] = None
    return fields, notes


def _event_fields(e: EventOp, family: Family, tz: ZoneInfo) -> tuple[dict, list[str]]:
    """Validated fields present on the op. A timed event has no all-day dates, and vice versa."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(e.text)) is not None:
        fields["text"] = text
    if e.who is not None:
        who = normalize_member(e.who, family)
        if who is None:
            notes.append(f"unknown who {e.who!r} -> null")
        fields["who"] = who
    for name in ("starts_at", "until"):
        raw = getattr(e, name)
        if raw is not None:
            value = normalize_datetime(raw, tz)
            if value is None:
                notes.append(f"bad {name} {raw!r} dropped")
            else:
                fields[name] = value
    for name in ("date_from", "date_to"):
        raw = getattr(e, name)
        if raw is not None:
            value = normalize_date(raw)
            if value is None:
                notes.append(f"bad {name} {raw!r} dropped")
            else:
                fields[name] = value
    if "starts_at" in fields:
        fields["date_from"] = fields["date_to"] = None
    elif "date_from" in fields or "date_to" in fields:
        fields["starts_at"] = fields["until"] = None
        fields.setdefault("date_from", fields.get("date_to"))
        fields.setdefault("date_to", fields.get("date_from"))
        if (fields["date_from"] or "") > (fields["date_to"] or ""):
            notes.append(f"date_to {fields['date_to']} before date_from dropped")
            fields["date_to"] = fields["date_from"]
    return fields, notes


def _reminder_fields(r: ReminderOp, family: Family, tz: ZoneInfo) -> tuple[dict, list[str]]:
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(r.text)) is not None:
        fields["text"] = text
    if r.who is not None:
        who = normalize_member(r.who, family)
        if who is None:
            notes.append(f"unknown who {r.who!r} -> null")
        fields["who"] = who
    if r.at is not None:
        at = normalize_datetime(r.at, tz)
        if at is None:
            notes.append(f"bad at {r.at!r} dropped")
        else:
            fields["at"] = at
    return fields, notes


def _check_until(
    fields: dict, starts_at: str | None, existing_until: str | None, notes: list[str]
) -> None:
    """`until` must come after the start; otherwise it is dropped (one-hour default applies).

    Covers a new `until` on the op and an existing one the op's new start moves past.
    """
    until = fields.get("until", existing_until)
    if until and starts_at and until <= starts_at:
        notes.append(f"until {until} not after starts_at dropped")
        fields["until"] = None


def apply_ops(
    db: Database,
    result: LlmResult,
    *,
    author_id: str,
    message_id: int,
    family: Family,
    tz: ZoneInfo,
) -> list[Applied]:
    applied: list[Applied] = []

    for j in result.journal:
        fields, notes = _entry_fields(j)
        if j.op == "create":
            if "text" not in fields:
                applied.append(Applied("entry", "create", None, False, "empty text"))
                continue
            day = fields.get("date") or datetime.now(tz).date().isoformat()
            eid = db.create_entry(fields["text"] or "", day, author_id, message_id)
            applied.append(Applied("entry", "create", eid, True, "; ".join(notes)))
        elif j.op == "update":
            if not fields:
                applied.append(Applied("entry", "update", j.id, False, "nothing to update"))
                continue
            ok = j.id is not None and db.update_entry(j.id, **fields)
            note = "; ".join(notes) if ok else "not found or deleted"
            applied.append(Applied("entry", "update", j.id, ok, note))
        elif j.op == "delete":
            ok = j.id is not None and db.delete_entry(j.id)
            applied.append(
                Applied("entry", "delete", j.id, ok, "" if ok else "not found or already deleted")
            )

    for e in result.events:
        fields, notes = _event_fields(e, family, tz)
        if e.op == "create":
            if "text" not in fields:
                applied.append(Applied("event", "create", None, False, "empty text"))
                continue
            if not (fields.get("starts_at") or fields.get("date_from")):
                applied.append(Applied("event", "create", None, False, "event without a date"))
                continue
            _check_until(fields, fields.get("starts_at"), None, notes)
            eid = db.create_event(
                fields["text"] or "",
                who=fields.get("who"),
                created_by=author_id,
                source_message_id=message_id,
                starts_at=fields.get("starts_at"),
                until=fields.get("until"),
                date_from=fields.get("date_from"),
                date_to=fields.get("date_to"),
            )
            applied.append(Applied("event", "create", eid, True, "; ".join(notes)))
        elif e.op == "update":
            existing = db.get_event(e.id) if e.id is not None else None
            if existing is None or not existing.is_planned:
                applied.append(Applied("event", "update", e.id, False, "not found or not planned"))
                continue
            if not fields:
                applied.append(Applied("event", "update", e.id, False, "nothing to update"))
                continue
            _check_until(fields, fields.get("starts_at", existing.starts_at), existing.until, notes)
            ok = db.update_event(existing.id, **fields)
            applied.append(Applied("event", "update", e.id, ok, "; ".join(notes)))
        elif e.op == "cancel":
            ok = e.id is not None and db.cancel_event(e.id)
            applied.append(
                Applied("event", "cancel", e.id, ok, "" if ok else "not found or not planned")
            )

    for c in result.commitments:
        fields, notes = _commitment_fields(c, family, tz)
        note = "; ".join(notes)
        if c.op == "create":
            if "text" not in fields:
                applied.append(Applied("commitment", "create", None, False, "empty text"))
                continue
            cid = db.create_commitment(
                fields["text"] or "",
                owner=fields.get("owner"),
                created_by=author_id,
                source_message_id=message_id,
                due_at=fields.get("due_at"),
                due_from=fields.get("due_from"),
                due_to=fields.get("due_to"),
            )
            applied.append(Applied("commitment", "create", cid, True, note))
        elif c.op == "update":
            if c.id is None or not fields:
                applied.append(Applied("commitment", "update", c.id, False, "nothing to update"))
                continue
            ok = db.update_commitment(c.id, **fields)
            applied.append(
                Applied("commitment", "update", c.id, ok, note if ok else "not found or not open")
            )
        elif c.op == "close":
            status = c.status or "done"
            ok = c.id is not None and db.close_commitment(c.id, status)
            note = "" if ok else "not found or not open"
            applied.append(Applied("commitment", f"close:{status}", c.id, ok, note))

    for r in result.reminders:
        fields, notes = _reminder_fields(r, family, tz)
        note = "; ".join(notes)
        if r.op == "create":
            if "text" not in fields:
                applied.append(Applied("reminder", "create", None, False, "empty text"))
                continue
            if "at" not in fields:
                applied.append(Applied("reminder", "create", None, False, "no time"))
                continue
            rid = db.create_reminder(
                fields["text"] or "",
                who=fields.get("who"),
                at=fields["at"] or "",
                created_by=author_id,
                source_message_id=message_id,
            )
            applied.append(Applied("reminder", "create", rid, True, note))
        elif r.op == "update":
            if r.id is None or not fields:
                applied.append(Applied("reminder", "update", r.id, False, "nothing to update"))
                continue
            ok = db.update_reminder(r.id, **fields)
            applied.append(
                Applied("reminder", "update", r.id, ok, note if ok else "not found or not pending")
            )
        elif r.op == "cancel":
            ok = r.id is not None and db.cancel_reminder(r.id)
            applied.append(
                Applied("reminder", "cancel", r.id, ok, "" if ok else "not found or not pending")
            )

    for a in applied:
        if not a.ok or a.note:
            log.info("op %s %s id=%s ok=%s %s", a.kind, a.op, a.id, a.ok, a.note)
    return applied
