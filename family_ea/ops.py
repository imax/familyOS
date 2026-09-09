"""Apply LLM operations to the database. Memories first, then commitments.

Invalid ops (unknown ids, closed items, bad dates) are ignored and logged, never fatal.
Web done/drop must go through `close_commitment` too, so there is one code path.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .db import Database
from .family import Family
from .llm import CommitmentOp, LlmResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Applied:
    kind: str  # 'memory' | 'commitment'
    op: str
    id: int | None
    ok: bool
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_due_at(value: str | None, tz: ZoneInfo) -> str | None:
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


def normalize_owner(owner: str | None, family: Family) -> str | None:
    if owner is None:
        return None
    return owner if any(p.id == owner for p in family.members) else None


def _commitment_fields(c: CommitmentOp, family: Family, tz: ZoneInfo) -> tuple[dict, list[str]]:
    """Validated fields present on the op, plus notes about anything dropped."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if c.text is not None and c.text.strip():
        fields["text"] = c.text.strip()
    if c.owner is not None:
        owner = normalize_owner(c.owner, family)
        if owner is None:
            notes.append(f"unknown owner {c.owner!r} -> null")
        fields["owner"] = owner
    if c.due_at is not None:
        due_at = normalize_due_at(c.due_at, tz)
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
    return fields, notes


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

    for m in result.memories:
        if m.op == "create":
            text = (m.text or "").strip()
            if not text:
                applied.append(Applied("memory", "create", None, False, "empty text"))
                continue
            mid = db.create_memory(text, author_id, message_id)
            applied.append(Applied("memory", "create", mid, True))
        elif m.op == "delete":
            ok = m.id is not None and db.delete_memory(m.id)
            applied.append(
                Applied("memory", "delete", m.id, ok, "" if ok else "not found or already deleted")
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

    for a in applied:
        if not a.ok or a.note:
            log.info("op %s %s id=%s ok=%s %s", a.kind, a.op, a.id, a.ok, a.note)
    return applied
