"""Deterministic context for the LLM, the event agenda, commitment buckets, the digest,
the web timeline.

Everything here is plain code: what is "today", what is "overdue", which journal
entries to show. The LLM only sees the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from .db import Commitment, Database, Entry, Event, Member, Message, Reminder
from .family import Family

JOURNAL_WINDOW_DAYS = 2  # fresher entries are in every LLM context; older ones only via search
RECENT_MESSAGES = 20
FTS_LIMIT = 10
PAST_EVENT_DAYS = 7  # ended events stay in the LLM context this long ("коли був стоматолог?")
DEFAULT_EVENT_DURATION = timedelta(hours=1)
WEEKDAYS_UK = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")
MONTHS_UK = (
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
)  # fmt: skip


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


def fmt_event_time(e: Event, tz: ZoneInfo) -> str:
    """'15:30–17:00' or '15:30'; '' for an all-day event."""
    if not e.starts_at:
        return ""
    start, end = event_span(e, tz)
    return f"{start:%H:%M}" + (f"–{end:%H:%M}" if e.until else "")


def fmt_event_when(e: Event, tz: ZoneInfo) -> str:
    """'11.09 15:30–17:00', '11.09 15:30', '12.09–19.09' or '12.09'."""
    start, end = event_span(e, tz)
    if e.starts_at:
        return f"{start:%d.%m} {fmt_event_time(e, tz)}"
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
        when = fmt_event_time(e, tz) + " "
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


# --- timeline -----------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One line of the timeline, ready to render: a planned event, an open commitment or a
    pending reminder. Everything is formatted here; the template only lays it out."""

    kind: str  # 'event' | 'commitment' | 'reminder'
    id: int
    text: str
    time: str = ""  # '16:00' (a start); '' for an all-day event or an untimed commitment
    note: str = ""  # 'до 17:00' / 'до 19.09': an end still ahead; the due when overdue
    who: str = ""  # display name; 'усім' for a reminder to everyone; '' when nobody in particular
    ics_url: str | None = None  # «📅»: an event or a dated commitment


@dataclass
class Day:
    when: date
    title: str
    rows: list[Row] = field(default_factory=list)


@dataclass
class Timeline:
    """The web home, Things-style: everything dated in one stream by day, the rest apart.

    Overdue commitments on top; then every day that has something, today always, even
    empty; then the commitments without a date.
    """

    overdue: list[Row] = field(default_factory=list)
    days: list[Day] = field(default_factory=list)
    undated: list[Row] = field(default_factory=list)


def day_title(d: date, today: date) -> str:
    """'Сьогодні, четвер 10.09', 'Завтра, п'ятниця 11.09', 'Середа 07.10'."""
    name = WEEKDAYS_UK[d.weekday()]
    if d == today:
        return f"Сьогодні, {name} {d:%d.%m}"
    if d == today + timedelta(days=1):
        return f"Завтра, {name} {d:%d.%m}"
    return f"{name.capitalize()} {d:%d.%m}"


def build_timeline(
    events: list[Event],
    commitments: list[Commitment],
    reminders: list[Reminder],
    now: datetime,
    family: Family,
) -> Timeline:
    """Place planned events, open commitments and pending reminders on days.

    A thing still running (a multi-day event, an open window) sits on today with «до …»;
    a reminder whose time passed but is still pending (about to be sent) sits on today too.
    Within a day: all-day events, then timed things by time, then untimed commitments.
    """
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    by_day: dict[date, list[tuple[tuple, Row]]] = {today: []}
    overdue: list[tuple[str, Row]] = []
    undated: list[Row] = []

    def place(day: date, key: tuple, row: Row) -> None:
        by_day.setdefault(day, []).append((key, row))

    def until_note(last: date, day: date) -> str:
        return f"до {last:%d.%m}" if last > day else ""

    for e in events:
        if not e.is_planned:
            continue
        start, end = event_span(e, tz)
        first, last = start.date(), (end - timedelta(seconds=1)).date()
        if last < today:
            continue
        day = max(first, today)
        if not e.starts_at:
            note = until_note(last, day)
        elif not e.until:
            note = ""
        else:
            note = f"до {end:%H:%M}" if last == day else f"до {end:%d.%m %H:%M}"
        row = Row(
            "event",
            e.id,
            e.text,
            time=f"{start:%H:%M}" if e.starts_at else "",
            note=note,
            who=family.display_name(e.who) if e.who else "",
            ics_url=f"/events/{e.id}.ics",
        )
        place(day, (1, start.timestamp(), 0, e.id) if e.starts_at else (0, 0.0, 0, e.id), row)

    for c in commitments:
        if not c.is_open:
            continue
        who = family.display_name(c.owner) if c.owner else ""
        ics = f"/commitments/{c.id}.ics" if c.has_due else None
        if c.due_at:
            due = parse_iso(c.due_at).astimezone(tz)
            if due < now:
                late = Row("commitment", c.id, c.text, note=fmt_due(c, tz), who=who, ics_url=ics)
                overdue.append((c.due_at, late))
                continue
            row = Row("commitment", c.id, c.text, time=f"{due:%H:%M}", who=who, ics_url=ics)
            place(due.date(), (1, due.timestamp(), 2, c.id), row)
        elif c.due_from or c.due_to:
            first = date.fromisoformat(c.due_from or c.due_to or "")
            last = date.fromisoformat(c.due_to or c.due_from or "")
            if last < today:
                late = Row("commitment", c.id, c.text, note=fmt_due(c, tz), who=who, ics_url=ics)
                overdue.append((c.due_to or c.due_from or "", late))
                continue
            day = max(first, today)
            row = Row("commitment", c.id, c.text, note=until_note(last, day), who=who, ics_url=ics)
            place(day, (2, 0.0, 2, c.id), row)
        else:
            undated.append(Row("commitment", c.id, c.text, who=who))

    for r in reminders:
        if not r.is_pending:
            continue
        at = parse_iso(r.at).astimezone(tz)
        row = Row(
            "reminder",
            r.id,
            r.text,
            time=f"{at:%H:%M}",
            who=family.display_name(r.who) if r.who else "усім",
        )
        place(max(at.date(), today), (1, at.timestamp(), 1, r.id), row)

    t = Timeline(undated=undated)
    t.overdue = [row for _, row in sorted(overdue, key=lambda pair: pair[0])]
    for day in sorted(by_day):
        rows = [row for _, row in sorted(by_day[day], key=lambda pair: pair[0])]
        t.days.append(Day(day, day_title(day, today), rows))
    return t


# --- journal ------------------------------------------------------------------


def month_title(iso_month: str) -> str:
    """'2026-09' -> 'Вересень 2026'."""
    year, month = iso_month.split("-")
    return f"{MONTHS_UK[int(month) - 1].capitalize()} {year}"


def group_by_month(entries: list[Entry]) -> list[tuple[str, list[Entry]]]:
    """Entries in the given order, grouped by the month of their day: [(title, entries)]."""
    return [(month_title(m), list(g)) for m, g in groupby(entries, key=lambda e: e.date[:7])]


# --- search -------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)

# Function words: they carry no meaning for search and would match half the database.
# (Kept as text: ruff's SIM905 turns a literal `"...".split()` into a hundred-line list.)
_STOPWORDS_TEXT = """
він вона воно вони мене тебе себе мені тобі собі нам вам нас вас його них ним нею йому
мій моя моє мої твій твоя твоє твої наш наша наше наші ваш ваша ваше ваші свій своя своє
свої цей цього цієї цьому той того тієї тому але або щоб коли куди звідки хто кого кому
чого чому від для про при під над без між через після перед біля коло крім ще вже теж
також тільки лише дуже там тут так ось був була було були буде бути треба можна потім
зараз сьогодні завтра вчора якщо який яка яке які весь вся все всі усе усі ага дякую будь
ласка два дві три один одна одне одну
"""
STOPWORDS_UK = frozenset(_STOPWORDS_TEXT.split())
# Inflection endings, longest first; one is cut when enough of the word remains.
ENDINGS_UK = (
    "ами", "ями", "ові", "еві", "єві", "ого", "ому", "ему", "єму", "ими", "іми", "їми", "ьми",
    "ьої", "ьою", "ах", "ях", "ам", "ям", "ою", "ею", "єю", "ів", "їв", "ей", "ий", "ій", "им",
    "ім", "їм", "их", "іх", "ом", "ем", "єм", "ої", "а", "я", "у", "ю", "и", "і", "ї", "е",
    "є", "о", "ь",
)  # fmt: skip


def stem(word: str) -> str | None:
    """A search prefix for a Ukrainian word: casefolded, ending cut, at most 5 chars.

    A cheap stand-in for stemming, tuned for names and nouns: «діти» / «дітям» / «дітьми» →
    «діт», «Коля» / «Колі» / «Колею» → «кол», «газовик» / «газовика» → «газов». Fleeting
    vowels («котел» / «котла») are not handled. None for function words, digits and stubs.
    """
    w = word.casefold()
    if len(w) < 3 or w.isdigit() or w in STOPWORDS_UK:
        return None
    keep = 2 if len(w) == 3 else 3  # «Оля» → «ол»: three-letter names inflect too
    for ending in ENDINGS_UK:
        if w.endswith(ending) and len(w) - len(ending) >= keep:
            w = w[: -len(ending)]
            break
    return w[:5]


def stems(text: str, max_terms: int = 12) -> list[str]:
    """Distinct stems of the words in `text`, in order of appearance."""
    out: list[str] = []
    for word in _WORD.findall(text):
        s = stem(word)
        if s and s not in out:
            out.append(s)
        if len(out) >= max_terms:
            break
    return out


def fts_query(text: str, max_terms: int = 12) -> str:
    """The stems as a forgiving FTS5 query: prefixes, OR-ed. '' when there is nothing."""
    return " OR ".join(f'"{s}"*' for s in stems(text, max_terms))


def word_pattern(text: str, max_terms: int = 12) -> str | None:
    """The stems as a regex for `ufold(text) REGEXP ?`: a word starting with any of them."""
    terms = stems(text, max_terms)
    return r"\b(?:" + "|".join(re.escape(s) for s in terms) + ")" if terms else None


# --- context ------------------------------------------------------------------


def _entry_line(e: Entry, family: Family) -> str:
    return f"[#{e.id}] {fmt_date(e.date)}, {family.display_name(e.created_by)}: {e.text}"


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
    since = (now - timedelta(days=JOURNAL_WINDOW_DAYS)).astimezone(ZoneInfo("UTC"))
    since_iso = since.isoformat(timespec="seconds").replace("+00:00", "Z")

    agenda = build_agenda(db.planned_events(), now)
    open_items = db.open_commitments()
    buckets = bucket_commitments(open_items, now)
    # The journal can be long; the LLM sees only what was just written and what the message
    # is about. The rest is on the web.
    recent_entries = db.entries_since(since_iso)
    older_hits = db.search_entries(
        fts_query(text), limit=FTS_LIMIT, exclude_ids={e.id for e in recent_entries}
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
        section("Сім'я (пишуть боту; решта людей — у фактах і нотатках)", [family.describe()]),
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
            f"Нотатки (journal) за останні {JOURNAL_WINDOW_DAYS} дні",
            [f"- {_entry_line(e, family)}" for e in recent_entries],
        ),
        section(
            "Старіші нотатки, схожі на повідомлення",
            [f"- {_entry_line(e, family)}" for e in older_hits],
        ),
        section(
            "Останні повідомлення",
            [_message_line(m, family, tz) for m in recent_messages],
        ),
        section("Нове повідомлення", [f"від {author.id} ({author.name}):\n{text}"]),
    ]
    return "\n\n".join(parts)
