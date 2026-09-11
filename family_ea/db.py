"""SQLite storage: members, messages, journal, events, commitments, reminders, facts.

One connection, one process, one writer. Original messages are never mutated;
journal entries are soft-deleted; commitments are closed, events are cancelled and reminders
are sent, missed or cancelled, never removed (a past event simply passes); facts (the
human-maintained standing context) keep every version; members (who talks to the bot) are
edited by the admin on the web.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL,            -- author: family member id or 'bot'
  chat_with TEXT NOT NULL,          -- family member whose Telegram chat this is
  tg_message_id INTEGER,
  created_at TEXT NOT NULL,         -- ISO UTC
  raw_text TEXT NOT NULL,           -- for voice: the transcript
  is_voice INTEGER NOT NULL DEFAULT 0,
  llm_result TEXT                   -- JSON: what the LLM returned and what was applied
);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,               -- the entry in full; can be long
  date TEXT NOT NULL,               -- ISO date: the day the entry is about
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS commitments (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  owner TEXT,                       -- family member id; NULL = both / unclear
  status TEXT NOT NULL,             -- 'open' | 'done' | 'dropped'
  due_at TEXT,                      -- ISO UTC datetime when there is a specific time
  due_from TEXT,                    -- ISO date, soft window start
  due_to TEXT,                      -- ISO date, soft window end
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  who TEXT,                         -- family member id; NULL = the whole family
  status TEXT NOT NULL,             -- 'planned' | 'cancelled'; a past event just passes
  starts_at TEXT,                   -- ISO UTC datetime: a timed event
  until TEXT,                       -- ISO UTC datetime: its end; NULL = one hour
  date_from TEXT,                   -- ISO date: an all-day event (one or more days)
  date_to TEXT,                     -- ISO date, inclusive; NULL = date_from
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,               -- what to send, self-contained
  who TEXT,                         -- family member id; NULL = everyone
  at TEXT NOT NULL,                 -- ISO UTC datetime: when to send
  status TEXT NOT NULL,             -- 'pending' | 'sent' | 'missed' | 'cancelled'
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,           -- every save is a new row; the latest one is current
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL          -- 'web' or a family member id
);

CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,              -- latin slug (oleh); what the LLM uses as owner
  name TEXT NOT NULL,
  telegram_id INTEGER UNIQUE,       -- the allowlist; NULL until known
  created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts
  USING fts5(text, content='journal', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS journal_ai AFTER INSERT ON journal BEGIN
  INSERT INTO journal_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS journal_ad AFTER DELETE ON journal BEGIN
  INSERT INTO journal_fts(journal_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS journal_au AFTER UPDATE OF text ON journal BEGIN
  INSERT INTO journal_fts(journal_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO journal_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _regexp(pattern: str, text: str) -> bool:
    """SQLite `text REGEXP pattern` via Python's re: Unicode-aware, unlike LIKE."""
    return _compiled(pattern).search(text) is not None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Message:
    id: int
    user_id: str
    chat_with: str
    tg_message_id: int | None
    created_at: str
    raw_text: str
    is_voice: bool
    llm_result: str | None


@dataclass(frozen=True)
class Entry:
    """A journal entry: what happened, in full, on a given day."""

    id: int
    text: str
    date: str  # ISO date: the day the entry is about
    created_by: str
    created_at: str
    source_message_id: int
    deleted_at: str | None


@dataclass(frozen=True)
class Commitment:
    id: int
    text: str
    owner: str | None
    status: str
    due_at: str | None
    due_from: str | None
    due_to: str | None
    created_by: str
    created_at: str
    source_message_id: int
    closed_at: str | None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def has_due(self) -> bool:
        """Dated in any way: a specific time or a soft window. Only these go to a calendar."""
        return bool(self.due_at or self.due_from or self.due_to)


@dataclass(frozen=True)
class Event:
    id: int
    text: str
    who: str | None
    status: str
    starts_at: str | None
    until: str | None
    date_from: str | None
    date_to: str | None
    created_by: str
    created_at: str
    source_message_id: int
    cancelled_at: str | None

    @property
    def is_planned(self) -> bool:
        return self.status == "planned"

    @property
    def all_day(self) -> bool:
        return self.starts_at is None


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    who: str | None
    at: str
    status: str
    created_by: str
    created_at: str
    source_message_id: int
    sent_at: str | None

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"


@dataclass(frozen=True)
class Facts:
    id: int
    text: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    telegram_id: int | None = None


def _message(row: sqlite3.Row) -> Message:
    d = dict(row)
    d["is_voice"] = bool(d["is_voice"])
    return Message(**d)


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(**dict(row))


def _commitment(row: sqlite3.Row) -> Commitment:
    return Commitment(**dict(row))


def _event(row: sqlite3.Row) -> Event:
    return Event(**dict(row))


def _reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(**dict(row))


ENTRY_UPDATABLE = ("text", "date")
COMMITMENT_UPDATABLE = ("text", "owner", "due_at", "due_from", "due_to")
EVENT_UPDATABLE = ("text", "who", "starts_at", "until", "date_from", "date_to")
REMINDER_UPDATABLE = ("text", "who", "at")


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # SQLite's LIKE and lower() are ASCII-only; Ukrainian text needs Python's casefold.
        self.conn.create_function("ufold", 1, str.casefold, deterministic=True)
        self.conn.create_function("regexp", 2, _regexp, deterministic=True)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """What CREATE TABLE IF NOT EXISTS cannot express. Idempotent, runs at every start."""
        tables = {
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "memories" in tables:
            # 2026-09-11: memories became the journal. An old row's day is the (UTC) day it was
            # written; the journal triggers index the copied rows; the old table goes.
            self.conn.executescript(
                "INSERT INTO journal (text, date, created_by, created_at, source_message_id,"
                " deleted_at) SELECT text, substr(created_at, 1, 10), created_by, created_at,"
                " source_message_id, deleted_at FROM memories ORDER BY id;"
                " DROP TABLE IF EXISTS memories_fts; DROP TABLE memories;"
            )

    def close(self) -> None:
        self.conn.close()

    def backup_to(self, path: Path | str) -> None:
        """A consistent copy of the whole database via SQLite's online backup.

        Copying the file with sftp misses whatever still sits in the -wal file; this does not.
        """
        dst = sqlite3.connect(str(path))
        try:
            self.conn.backup(dst)
        finally:
            dst.close()

    # --- messages -----------------------------------------------------------

    def insert_message(
        self,
        user_id: str,
        chat_with: str,
        raw_text: str,
        *,
        is_voice: bool = False,
        tg_message_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages"
            " (user_id, chat_with, tg_message_id, created_at, raw_text, is_voice)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, chat_with, tg_message_id, utc_now_iso(), raw_text, int(is_voice)),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def set_llm_result(self, message_id: int, llm_result: str) -> None:
        self.conn.execute(
            "UPDATE messages SET llm_result = ? WHERE id = ?", (llm_result, message_id)
        )
        self.conn.commit()

    def set_tg_message_id(self, message_id: int, tg_message_id: int) -> None:
        self.conn.execute(
            "UPDATE messages SET tg_message_id = ? WHERE id = ?", (tg_message_id, message_id)
        )
        self.conn.commit()

    def get_message(self, message_id: int) -> Message | None:
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def recent_messages(self, limit: int = 20) -> list[Message]:
        """Last N messages across all chats, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_message(r) for r in reversed(rows)]

    def list_messages(self, limit: int = 200) -> list[Message]:
        """Newest first, for the web debug view."""
        rows = self.conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_message(r) for r in rows]

    def last_user_message(self, chat_with: str) -> Message | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE chat_with = ? AND user_id != 'bot'"
            " ORDER BY id DESC LIMIT 1",
            (chat_with,),
        ).fetchone()
        return _message(row) if row else None

    # --- journal ------------------------------------------------------------

    def create_entry(self, text: str, date: str, created_by: str, source_message_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO journal (text, date, created_by, created_at, source_message_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (text, date, created_by, utc_now_iso(), source_message_id),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_entry(self, entry_id: int, **fields: str | None) -> bool:
        """Update text/date of an active entry. Returns False if unknown or deleted."""
        fields = {k: v for k, v in fields.items() if k in ENTRY_UPDATABLE}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE journal SET {assignments} WHERE id = ? AND deleted_at IS NULL",
            (*fields.values(), entry_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def delete_entry(self, entry_id: int) -> bool:
        """Soft delete. Returns False if the entry does not exist or is already deleted."""
        cur = self.conn.execute(
            "UPDATE journal SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (utc_now_iso(), entry_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_entry(self, entry_id: int) -> Entry | None:
        row = self.conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()
        return _entry(row) if row else None

    def entries_since(self, since_iso: str) -> list[Entry]:
        """Active entries written at or after `since_iso`, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM journal WHERE deleted_at IS NULL AND created_at >= ? ORDER BY id",
            (since_iso,),
        ).fetchall()
        return [_entry(r) for r in rows]

    def list_entries(self, limit: int = 200) -> list[Entry]:
        """Active entries, newest day first, newest first within a day."""
        rows = self.conn.execute(
            "SELECT * FROM journal WHERE deleted_at IS NULL ORDER BY date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_entry(r) for r in rows]

    def search_entries(
        self, match: str, limit: int = 10, exclude_ids: set[int] | None = None
    ) -> list[Entry]:
        """FTS5 search over active entries; `match` is an FTS5 query string."""
        if not match:
            return []
        try:
            rows = self.conn.execute(
                "SELECT j.* FROM journal_fts f JOIN journal j ON j.id = f.rowid"
                " WHERE journal_fts MATCH ? AND j.deleted_at IS NULL"
                " ORDER BY f.rank LIMIT ?",
                (match, limit + len(exclude_ids or ())),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # malformed query; search is best-effort
        out = [_entry(r) for r in rows]
        if exclude_ids:
            out = [e for e in out if e.id not in exclude_ids]
        return out[:limit]

    # --- facts --------------------------------------------------------------

    def current_facts(self) -> Facts | None:
        row = self.conn.execute("SELECT * FROM facts ORDER BY id DESC LIMIT 1").fetchone()
        return Facts(**dict(row)) if row else None

    def save_facts(self, text: str, created_by: str) -> int:
        """Store a new version. Returns its id."""
        cur = self.conn.execute(
            "INSERT INTO facts (text, created_at, created_by) VALUES (?, ?, ?)",
            (text, utc_now_iso(), created_by),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def facts_versions(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    # --- members ------------------------------------------------------------

    def list_members(self) -> list[Member]:
        rows = self.conn.execute(
            "SELECT id, name, telegram_id FROM members ORDER BY rowid"
        ).fetchall()
        return [Member(**dict(r)) for r in rows]

    def get_member(self, member_id: str) -> Member | None:
        row = self.conn.execute(
            "SELECT id, name, telegram_id FROM members WHERE id = ?", (member_id,)
        ).fetchone()
        return Member(**dict(row)) if row else None

    def member_by_telegram_id(self, telegram_id: int) -> Member | None:
        row = self.conn.execute(
            "SELECT id, name, telegram_id FROM members WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return Member(**dict(row)) if row else None

    def add_member(self, member_id: str, name: str, telegram_id: int | None) -> Member:
        """Raises ValueError when the id or the telegram_id is already taken."""
        try:
            self.conn.execute(
                "INSERT INTO members (id, name, telegram_id, created_at) VALUES (?, ?, ?, ?)",
                (member_id, name, telegram_id, utc_now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ValueError(f"member {member_id!r} or telegram_id {telegram_id} taken") from exc
        self.conn.commit()
        return Member(member_id, name, telegram_id)

    def update_member(self, member_id: str, *, name: str, telegram_id: int | None) -> bool:
        """Returns False if there is no such member. Raises ValueError on a taken telegram_id."""
        try:
            cur = self.conn.execute(
                "UPDATE members SET name = ?, telegram_id = ? WHERE id = ?",
                (name, telegram_id, member_id),
            )
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ValueError(f"telegram_id {telegram_id} taken") from exc
        self.conn.commit()
        return cur.rowcount == 1

    # --- events -------------------------------------------------------------

    def create_event(
        self,
        text: str,
        *,
        who: str | None,
        created_by: str,
        source_message_id: int,
        starts_at: str | None = None,
        until: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (text, who, status, starts_at, until, date_from, date_to,"
            " created_by, created_at, source_message_id)"
            " VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?)",
            (
                text,
                who,
                starts_at,
                until,
                date_from,
                date_to,
                created_by,
                utc_now_iso(),
                source_message_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_event(self, event_id: int, **fields: str | None) -> bool:
        """Update text/who/when of a planned event. Returns False if not planned."""
        fields = {k: v for k, v in fields.items() if k in EVENT_UPDATABLE}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE events SET {assignments} WHERE id = ? AND status = 'planned'",
            (*fields.values(), event_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def cancel_event(self, event_id: int) -> bool:
        """Returns False if the event does not exist or is already cancelled."""
        cur = self.conn.execute(
            "UPDATE events SET status = 'cancelled', cancelled_at = ?"
            " WHERE id = ? AND status = 'planned'",
            (utc_now_iso(), event_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_event(self, event_id: int) -> Event | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _event(row) if row else None

    def planned_events(self) -> list[Event]:
        """All planned events, past ones included; callers slice by time (see context)."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE status = 'planned' ORDER BY id"
        ).fetchall()
        return [_event(r) for r in rows]

    def search_events(self, pattern: str, limit: int = 50) -> list[Event]:
        """`pattern` is a casefolded regex, see `context.word_pattern`."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE ufold(text) REGEXP ? ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [_event(r) for r in rows]

    # --- commitments --------------------------------------------------------

    def create_commitment(
        self,
        text: str,
        *,
        owner: str | None,
        created_by: str,
        source_message_id: int,
        due_at: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO commitments (text, owner, status, due_at, due_from, due_to,"
            " created_by, created_at, source_message_id)"
            " VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?)",
            (text, owner, due_at, due_from, due_to, created_by, utc_now_iso(), source_message_id),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_commitment(self, commitment_id: int, **fields: str | None) -> bool:
        """Update text/owner/due_* of an open commitment. Returns False if not open."""
        fields = {k: v for k, v in fields.items() if k in COMMITMENT_UPDATABLE}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE commitments SET {assignments} WHERE id = ? AND status = 'open'",
            (*fields.values(), commitment_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def close_commitment(self, commitment_id: int, status: str) -> bool:
        """Close an open commitment as 'done' or 'dropped'. Returns False if not open."""
        if status not in ("done", "dropped"):
            raise ValueError(f"bad status: {status}")
        cur = self.conn.execute(
            "UPDATE commitments SET status = ?, closed_at = ? WHERE id = ? AND status = 'open'",
            (status, utc_now_iso(), commitment_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_commitment(self, commitment_id: int) -> Commitment | None:
        row = self.conn.execute(
            "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
        ).fetchone()
        return _commitment(row) if row else None

    def open_commitments(self) -> list[Commitment]:
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE status = 'open' ORDER BY id"
        ).fetchall()
        return [_commitment(r) for r in rows]

    def search_commitments(self, pattern: str, limit: int = 50) -> list[Commitment]:
        """`pattern` is a casefolded regex, see `context.word_pattern`."""
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE ufold(text) REGEXP ? ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [_commitment(r) for r in rows]

    # --- reminders ------------------------------------------------------------

    def create_reminder(
        self,
        text: str,
        *,
        who: str | None,
        at: str,
        created_by: str,
        source_message_id: int,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO reminders (text, who, at, status, created_by, created_at,"
            " source_message_id) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (text, who, at, created_by, utc_now_iso(), source_message_id),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_reminder(self, reminder_id: int, **fields: str | None) -> bool:
        """Update text/who/at of a pending reminder. Returns False if not pending."""
        fields = {k: v for k, v in fields.items() if k in REMINDER_UPDATABLE}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE reminders SET {assignments} WHERE id = ? AND status = 'pending'",
            (*fields.values(), reminder_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def cancel_reminder(self, reminder_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (reminder_id,),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def finish_reminder(self, reminder_id: int, status: str) -> bool:
        """'sent', or 'missed' when it came due while the bot was down. False if not pending."""
        if status not in ("sent", "missed"):
            raise ValueError(f"bad status: {status}")
        cur = self.conn.execute(
            "UPDATE reminders SET status = ?, sent_at = ? WHERE id = ? AND status = 'pending'",
            (status, utc_now_iso() if status == "sent" else None, reminder_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        row = self.conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _reminder(row) if row else None

    def pending_reminders(self) -> list[Reminder]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' ORDER BY at, id"
        ).fetchall()
        return [_reminder(r) for r in rows]

    def due_reminders(self, now_iso: str) -> list[Reminder]:
        """Pending reminders whose time has come: `at` <= now, both ISO UTC."""
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND at <= ? ORDER BY at, id",
            (now_iso,),
        ).fetchall()
        return [_reminder(r) for r in rows]
