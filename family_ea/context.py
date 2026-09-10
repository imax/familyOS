"""Deterministic context for the LLM, the event agenda, commitment buckets, the digest.

Everything here is plain code: what is "today", what is "overdue", which memories
to show. The LLM only sees the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Commitment, Database, Event, Member, Memory, Message, Reminder
from .family import Family

MEMORY_WINDOW_DAYS = 60
RECENT_MESSAGES = 20
FTS_LIMIT = 10
PAST_EVENT_DAYS = 7  # ended events stay in the LLM context this long ("коли був стоматолог?")
DEFAULT_EVENT_DURATION = timedelta(hours=1)
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


# --- events -------------------------------------------------------------------


def event_span(e: Event, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local start and exclusive end. Timed: `until` or one hour. All-day: midnight to midnight."""
    if e.starts_at:
        start = parse_iso(e.starts_at).astimezone(tz)
        end = parse_iso(e.until).astimezone(tz) if e.until else start + DEFAULT_EVENT_DURATION
        return start, end
    first = date.fromisoformat(e.date_from or e.date_to or "")
    last = date.fromisoformat(e.date_to or e.date_from or "")
    midnight = datetime.min.time()
    return (
        datetime.combine(first, midnight, tzinfo=tz),
        datetime.combine(last + timedelta(days=1), midnight, tzinfo=tz),
    )


def fmt_event_when(e: Event, tz: ZoneInfo) -> str:
    """'11.09 15:30–17:00', '11.09 15:30', '12.09–19.09' or '12.09'."""
    start, end = event_span(e, tz)
    if e.starts_at:
        return f"{start:%d.%m %H:%M}" + (f"–{end:%H:%M}" if e.until else "")
    last = end - timedelta(days=1)
    return f"{start:%d.%m}" if last.date() == start.date() else f"{start:%d.%m}–{last:%d.%m}"


def event_line(
    e: Event, family: Family, tz: ZoneInfo, *, with_id: bool = True, with_date: bool = True
) -> str:
    """'[подія #3] 11.09 15:30 Стоматолог (Анна)'; without date: '15:30 Стоматолог (Анна)'."""
    start, end = event_span(e, tz)
    meta = [family.display_name(e.who)] if e.who else []
    if with_date:
        when = fmt_event_when(e, tz) + " "
    elif e.starts_at:
        when = f"{start:%H:%M}" + (f"–{end:%H:%M}" if e.until else "") + " "
    else:
        when = ""
        last = end - timedelta(days=1)
        if last.date() != start.date():
            meta.append(f"до {last:%d.%m}")
    head = f"[подія #{e.id}] " if with_id else ""
    tail = f" ({', '.join(meta)})" if meta else ""
    return f"{head}{when}{e.text}{tail}"


@dataclass
class Agenda:
    today: list[Event] = field(default_factory=list)
    tomorrow: list[Event] = field(default_factory=list)
    later: list[Event] = field(default_factory=list)
    recent: list[Event] = field(default_factory=list)  # ended within PAST_EVENT_DAYS

    @property
    def upcoming(self) -> list[Event]:
        return self.today + self.tomorrow + self.later


def build_agenda(items: list[Event], now: datetime, past_days: int = PAST_EVENT_DAYS) -> Agenda:
    """Planned events by day relative to `now` (aware, family tz). A multi-day event lands in
    the first list it touches; ended events older than `past_days` are left out."""
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    horizon = now - timedelta(days=past_days)
    a = Agenda()
    for e in sorted((e for e in items if e.is_planned), key=lambda e: event_span(e, tz)[0]):
        start, end = event_span(e, tz)
        first, last = start.date(), (end - timedelta(seconds=1)).date()
        if first <= today <= last:
            a.today.append(e)
        elif first <= tomorrow <= last:
            a.tomorrow.append(e)
        elif first > tomorrow:
            a.later.append(e)
        elif end >= horizon:
            a.recent.append(e)
    return a


# --- commitment buckets -------------------------------------------------------


@dataclass
class Buckets:
    today: list[Commitment] = field(default_factory=list)
    overdue: list[Commitment] = field(default_factory=list)
    open: list[Commitment] = field(default_factory=list)  # no dates
    later: list[Commitment] = field(default_factory=list)  # dated, in the future


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


# --- reminders ----------------------------------------------------------------


def reminder_line(r: Reminder, family: Family, tz: ZoneInfo) -> str:
    """'[нагадування #2] 11.09 15:00 Зустріч з пані Марією о 16:00 (Анна)'; '(усім)' for all."""
    to = family.display_name(r.who) if r.who else "усім"
    return f"[нагадування #{r.id}] {fmt_dt(r.at, tz)} {r.text} ({to})"


# --- digest -------------------------------------------------------------------


def _digest_lines(
    a: Agenda,
    b: Buckets,
    family: Family,
    tz: ZoneInfo,
    *,
    with_ids: bool,
    include_open: bool,
    max_open: int,
) -> list[str]:
    def ev(e: Event) -> str:
        return f"- {event_line(e, family, tz, with_id=with_ids, with_date=False)}"

    def cm(c: Commitment) -> str:
        return f"- {commitment_line(c, family, tz, with_id=with_ids)}"

    lines: list[str] = []
    if a.today:
        lines += ["Сьогодні:", *map(ev, a.today)]
    if a.tomorrow:
        lines += ["Завтра:", *map(ev, a.tomorrow)]
    if b.today:
        lines += ["Справи на сьогодні:", *map(cm, b.today)]
    if b.overdue:
        lines += ["Прострочено:", *map(cm, b.overdue)]
    if include_open and b.open:
        lines += ["Без дати:", *map(cm, b.open[:max_open])]
        if len(b.open) > max_open:
            lines.append(f"- і ще {len(b.open) - max_open}")
    return lines


def render_digest(a: Agenda, b: Buckets, family: Family, tz: ZoneInfo, max_open: int = 5) -> str:
    """Today / tomorrow / due / overdue / open, with ids, for the LLM context."""
    lines = _digest_lines(a, b, family, tz, with_ids=True, include_open=True, max_open=max_open)
    return "\n".join(lines) if lines else "нічого"


def digest_text(
    a: Agenda,
    b: Buckets,
    family: Family,
    tz: ZoneInfo,
    *,
    include_open: bool,
    max_open: int = 5,
) -> str | None:
    """The morning push: today's and tomorrow's events, today's and overdue commitments,
    plus undated ones when `include_open`. None when there is nothing to say: an empty
    morning stays silent. Deterministic on purpose: presentation, not understanding."""
    lines = _digest_lines(
        a, b, family, tz, with_ids=False, include_open=include_open, max_open=max_open
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
    """Assemble everything the LLM needs for one message."""
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    since = (now - timedelta(days=MEMORY_WINDOW_DAYS)).astimezone(ZoneInfo("UTC"))
    since_iso = since.isoformat(timespec="seconds").replace("+00:00", "Z")

    agenda = build_agenda(db.planned_events(), now)
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
            f"Події (минулі за {PAST_EVENT_DAYS} днів і всі майбутні)",
            [f"- {event_line(e, family, tz)}" for e in agenda.recent + agenda.upcoming],
        ),
        section(
            "Нагадування (заплановані, ще не надіслані)",
            [f"- {reminder_line(r, family, tz)}" for r in db.pending_reminders()],
        ),
        section(
            "Відкриті commitments (справи, усі)",
            [f"- {commitment_line(c, family, tz)}" for c in open_items],
        ),
        section("Сьогодні / прострочено", [render_digest(agenda, buckets, family, tz)]),
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
