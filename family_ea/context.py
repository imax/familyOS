"""Deterministic context for the LLM, and the commitment buckets shared with the digest.

Everything here is plain code: what is "today", what is "overdue", which memories
to show. The LLM only sees the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Commitment, Database, Member, Memory, Message
from .family import Family

MEMORY_WINDOW_DAYS = 60
RECENT_MESSAGES = 20
FTS_LIMIT = 10
WEEKDAYS_UK = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")


# --- dates -------------------------------------------------------------------


def parse_iso(value: str) -> datetime:
    """Parse an ISO datetime; naive values are treated as UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def fmt_dt(iso_utc: str, tz: ZoneInfo) -> str:
    """'2026-09-10T12:30:00Z' -> '10.09 15:30' in the family timezone."""
    return parse_iso(iso_utc).astimezone(tz).strftime("%d.%m %H:%M")


def fmt_date(iso_date: str) -> str:
    """'2026-09-20' -> '20.09'."""
    return date.fromisoformat(iso_date).strftime("%d.%m")


def fmt_due(c: Commitment, tz: ZoneInfo) -> str:
    if c.due_at:
        return fmt_dt(c.due_at, tz)
    if c.due_from and c.due_to and c.due_from != c.due_to:
        return f"{fmt_date(c.due_from)}–{fmt_date(c.due_to)}"
    if c.due_from or c.due_to:
        return fmt_date(c.due_from or c.due_to or "")
    return ""


# --- commitment buckets -------------------------------------------------------


@dataclass
class Buckets:
    today: list[Commitment] = field(default_factory=list)
    overdue: list[Commitment] = field(default_factory=list)
    open: list[Commitment] = field(default_factory=list)  # no dates
    later: list[Commitment] = field(default_factory=list)  # dated, in the future

    @property
    def digest_empty(self) -> bool:
        """True when a morning digest would have nothing to say."""
        return not (self.today or self.overdue or self.open)


def bucket_commitments(items: list[Commitment], now: datetime) -> Buckets:
    """Split open commitments into today / overdue / open / later relative to `now`.

    `now` must be timezone-aware in the family timezone; "today" is its date.
    """
    today = now.date()
    b = Buckets()
    for c in items:
        if not c.is_open:
            continue
        if c.due_at:
            due = parse_iso(c.due_at).astimezone(now.tzinfo)
            if due < now:
                b.overdue.append(c)
            elif due.date() == today:
                b.today.append(c)
            else:
                b.later.append(c)
        elif c.due_from or c.due_to:
            start = date.fromisoformat(c.due_from or c.due_to or "")
            end = date.fromisoformat(c.due_to or c.due_from or "")
            if end < today:
                b.overdue.append(c)
            elif start <= today:
                b.today.append(c)
            else:
                b.later.append(c)
        else:
            b.open.append(c)

    def sort_key(c: Commitment) -> str:
        return c.due_at or c.due_to or c.due_from or c.created_at

    b.today.sort(key=sort_key)
    b.overdue.sort(key=sort_key)
    b.later.sort(key=sort_key)
    return b


def commitment_line(c: Commitment, family: Family, tz: ZoneInfo, with_id: bool = True) -> str:
    parts = [f"[#{c.id}] " if with_id else "", c.text]
    meta = []
    if c.owner:
        meta.append(family.display_name(c.owner))
    due = fmt_due(c, tz)
    if due:
        meta.append(due)
    if meta:
        parts.append(f" ({', '.join(meta)})")
    return "".join(parts)


def _digest_lines(
    b: Buckets,
    family: Family,
    tz: ZoneInfo,
    *,
    with_ids: bool,
    include_open: bool,
    max_open: int,
) -> list[str]:
    def line(c: Commitment) -> str:
        return f"- {commitment_line(c, family, tz, with_id=with_ids)}"

    lines: list[str] = []
    if b.today:
        lines.append("Сьогодні:")
        lines += [line(c) for c in b.today]
    if b.overdue:
        lines.append("Прострочено:")
        lines += [line(c) for c in b.overdue]
    if include_open and b.open:
        lines.append("Без дати:")
        lines += [line(c) for c in b.open[:max_open]]
        if len(b.open) > max_open:
            lines.append(f"- і ще {len(b.open) - max_open}")
    return lines


def render_digest(b: Buckets, family: Family, tz: ZoneInfo, max_open: int = 5) -> str:
    """The today / overdue / open block with ids, for the LLM context."""
    lines = _digest_lines(b, family, tz, with_ids=True, include_open=True, max_open=max_open)
    return "\n".join(lines) if lines else "нічого"


def digest_text(
    b: Buckets, family: Family, tz: ZoneInfo, *, include_open: bool, max_open: int = 5
) -> str | None:
    """The morning push: today and overdue, plus undated items when `include_open`.

    None when there is nothing to say (spec scenario H: an empty morning stays silent).
    Deterministic on purpose: this is presentation, not understanding.
    """
    lines = _digest_lines(
        b, family, tz, with_ids=False, include_open=include_open, max_open=max_open
    )
    if not lines:
        return None
    return "\n".join(lines) + "\n\nНічого не забули?"


# --- FTS ----------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


def fts_query(text: str, max_terms: int = 12) -> str:
    """Turn free text into a forgiving FTS5 query: 5-char prefixes of longer words, OR-ed.

    Cheap stand-in for stemming that works well enough for Ukrainian inflection
    ("газовик" / "газовика" / "газовику" share "газов").
    """
    terms: list[str] = []
    for w in _WORD.findall(text.lower()):
        if len(w) < 4 or w.isdigit():
            continue
        stem = w[:5]
        if stem not in terms:
            terms.append(stem)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{t}"*' for t in terms)


# --- context ------------------------------------------------------------------


def _memory_line(m: Memory, family: Family, tz: ZoneInfo) -> str:
    day = fmt_dt(m.created_at, tz)[:5]
    return f"[#{m.id}] {day}, {family.display_name(m.created_by)}: {m.text}"


def _message_line(msg: Message, family: Family, tz: ZoneInfo) -> str:
    when = fmt_dt(msg.created_at, tz)
    if msg.user_id == "bot":
        who = f"бот → {family.display_name(msg.chat_with)}"
    else:
        who = family.display_name(msg.user_id)
    voice = " (голосове)" if msg.is_voice else ""
    return f"[{when}] {who}{voice}: {msg.raw_text}"


def build_context(db: Database, family: Family, now: datetime, author: Member, text: str) -> str:
    """Assemble everything the LLM needs for one message. Spec section 5.2."""
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    since = (now - timedelta(days=MEMORY_WINDOW_DAYS)).astimezone(ZoneInfo("UTC"))
    since_iso = since.isoformat(timespec="seconds").replace("+00:00", "Z")

    open_items = db.open_commitments()
    buckets = bucket_commitments(open_items, now)
    recent_memories = db.memories_since(since_iso)
    older_hits = db.search_memories(
        fts_query(text), limit=FTS_LIMIT, exclude_ids={m.id for m in recent_memories}
    )
    recent_messages = db.recent_messages(RECENT_MESSAGES)
    facts = db.current_facts()
    facts_text = facts.text.strip() if facts else ""

    def section(title: str, lines: list[str], empty: str = "немає") -> str:
        body = "\n".join(lines) if lines else empty
        return f"## {title}\n{body}"

    parts = [
        section(
            "Зараз",
            [f"{now.strftime('%Y-%m-%d %H:%M')} ({tz.key}), {WEEKDAYS_UK[now.weekday()]}"],
        ),
        section("Сім'я (пишуть боту; решта людей — у memories)", [family.describe()]),
        section(
            "Факти про сім'ю (веде людина, стабільний фон)",
            [facts_text] if facts_text else [],
            empty="поки порожньо",
        ),
        section(
            "Відкриті commitments (усі)",
            [f"- {commitment_line(c, family, tz)}" for c in open_items],
        ),
        section("Сьогодні / прострочено", [render_digest(buckets, family, tz)]),
        section(
            f"Memories за останні {MEMORY_WINDOW_DAYS} днів",
            [f"- {_memory_line(m, family, tz)}" for m in recent_memories],
        ),
        section(
            "Старіші memories, схожі на повідомлення",
            [f"- {_memory_line(m, family, tz)}" for m in older_hits],
        ),
        section(
            "Останні повідомлення",
            [_message_line(m, family, tz) for m in recent_messages],
        ),
        section("Нове повідомлення", [f"від {author.id} ({author.name}):\n{text}"]),
    ]
    return "\n\n".join(parts)
