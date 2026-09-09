"""SQLite storage: messages, memories, commitments.

One connection, one process, one writer. Original messages are never mutated;
memories are soft-deleted; commitments are closed, never removed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
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

CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
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

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(text, content='memories', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


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
class Memory:
    id: int
    text: str
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


def _message(row: sqlite3.Row) -> Message:
    d = dict(row)
    d["is_voice"] = bool(d["is_voice"])
    return Message(**d)


def _memory(row: sqlite3.Row) -> Memory:
    return Memory(**dict(row))


def _commitment(row: sqlite3.Row) -> Commitment:
    return Commitment(**dict(row))


COMMITMENT_UPDATABLE = ("text", "owner", "due_at", "due_from", "due_to")


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # SQLite's LIKE and lower() are ASCII-only; Ukrainian text needs Python's casefold.
        self.conn.create_function("ufold", 1, str.casefold, deterministic=True)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

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

    # --- memories -----------------------------------------------------------

    def create_memory(self, text: str, created_by: str, source_message_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories (text, created_by, created_at, source_message_id)"
            " VALUES (?, ?, ?, ?)",
            (text, created_by, utc_now_iso(), source_message_id),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def delete_memory(self, memory_id: int) -> bool:
        """Soft delete. Returns False if the memory does not exist or is already deleted."""
        cur = self.conn.execute(
            "UPDATE memories SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (utc_now_iso(), memory_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_memory(self, memory_id: int) -> Memory | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _memory(row) if row else None

    def memories_since(self, since_iso: str) -> list[Memory]:
        """Active memories created at or after `since_iso`, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE deleted_at IS NULL AND created_at >= ? ORDER BY id",
            (since_iso,),
        ).fetchall()
        return [_memory(r) for r in rows]

    def list_memories(self, limit: int = 200) -> list[Memory]:
        """Active memories, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_memory(r) for r in rows]

    def search_memories(
        self, match: str, limit: int = 10, exclude_ids: set[int] | None = None
    ) -> list[Memory]:
        """FTS5 search over active memories; `match` is an FTS5 query string."""
        if not match:
            return []
        try:
            rows = self.conn.execute(
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid"
                " WHERE memories_fts MATCH ? AND m.deleted_at IS NULL"
                " ORDER BY f.rank LIMIT ?",
                (match, limit + len(exclude_ids or ())),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # malformed query; search is best-effort
        out = [_memory(r) for r in rows]
        if exclude_ids:
            out = [m for m in out if m.id not in exclude_ids]
        return out[:limit]

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

    def search_commitments(self, q: str, limit: int = 50) -> list[Commitment]:
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE ufold(text) LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q.casefold()}%", limit),
        ).fetchall()
        return [_commitment(r) for r in rows]
