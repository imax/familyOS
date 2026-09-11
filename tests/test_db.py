import sqlite3
from pathlib import Path

from family_ea.db import Database


def test_journal_fts_prefix_update_and_soft_delete(db: Database) -> None:
    mid = db.insert_message("oleh", "oleh", "hi")
    e1 = db.create_entry(
        "Газовик Петро замінив клапан у котлі, тел +380501234567", "2026-09-09", "oleh", mid
    )
    e2 = db.create_entry("Стоматолог Олі — вул. Хрещатик 1", "2026-09-08", "anna", mid)

    hits = db.search_entries('"газов"* OR "котл"*')
    assert [e.id for e in hits] == [e1]
    assert [e.id for e in db.list_entries()] == [e1, e2]  # newest day first

    assert db.update_entry(e2, date="2026-09-10", text="Стоматолог Олі: вул. Хрещатик 1")
    assert db.update_entry(e2, deleted_at="x") is False  # not an updatable field
    assert [e.id for e in db.list_entries()] == [e2, e1]
    assert db.search_entries('"хреща"*')[0].text.startswith("Стоматолог Олі:")

    assert db.delete_entry(e1) is True
    assert db.delete_entry(e1) is False  # already deleted
    assert db.update_entry(e1, text="назад") is False  # deleted entries stay as they were
    assert db.search_entries('"газов"*') == []
    assert [e.id for e in db.list_entries()] == [e2]


def test_search_entries_swallows_bad_query(db: Database) -> None:
    assert db.search_entries('"unbalanced') == []
    assert db.search_entries("") == []


def test_memories_table_becomes_the_journal(tmp_path: Path) -> None:
    """A database from before 2026-09-11 has `memories`; the first start moves them over."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, source_message_id INTEGER NOT NULL, deleted_at TEXT);
        CREATE VIRTUAL TABLE memories_fts USING fts5(text, content='memories', content_rowid='id');
        INSERT INTO memories VALUES (1, 'Газовик Петро', 'oleh', '2026-09-10T09:00:00Z', 1, NULL);
        INSERT INTO memories VALUES
          (2, 'зайве', 'oleh', '2026-09-10T10:00:00Z', 1, '2026-09-10T11:00:00Z');
        """
    )
    conn.close()

    db = Database(path)
    entries = db.list_entries()
    assert [(e.text, e.date, e.created_by) for e in entries] == [
        ("Газовик Петро", "2026-09-10", "oleh")
    ]
    assert [e.id for e in db.search_entries('"газов"*')] == [entries[0].id]
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memories" not in tables and "memories_fts" not in tables
    db.close()
    again = Database(path)  # the second start finds nothing to do
    assert len(again.list_entries()) == 1
    again.close()


def test_commitment_lifecycle(db: Database) -> None:
    mid = db.insert_message("anna", "anna", "завтра стоматолог")
    cid = db.create_commitment(
        "Стоматолог",
        owner="anna",
        created_by="anna",
        source_message_id=mid,
        due_at="2026-09-10T12:30:00Z",
    )
    assert [c.id for c in db.open_commitments()] == [cid]

    assert db.update_commitment(cid, due_to="2026-09-23") is True
    assert db.update_commitment(cid, bogus="x") is False
    c = db.get_commitment(cid)
    assert c and c.due_to == "2026-09-23" and c.due_at == "2026-09-10T12:30:00Z"

    assert db.close_commitment(cid, "done") is True
    assert db.close_commitment(cid, "done") is False  # not open any more
    assert db.close_commitment(999, "done") is False  # does not exist
    assert db.open_commitments() == []
    assert db.update_commitment(cid, text="x") is False
    c = db.get_commitment(cid)
    assert c and c.status == "done" and c.closed_at

    assert [x.id for x in db.search_commitments(r"\bстомат")] == [cid]
    assert db.search_commitments(r"\bтомат") == []  # a word start, not a substring


def test_messages_order_and_last_user_message(db: Database) -> None:
    a = db.insert_message("oleh", "oleh", "one")
    b = db.insert_message("bot", "oleh", "reply", tg_message_id=None)
    c = db.insert_message("anna", "anna", "two", is_voice=True)
    assert [m.id for m in db.recent_messages(2)] == [b, c]
    assert [m.id for m in db.list_messages()] == [c, b, a]
    last = db.last_user_message("oleh")
    assert last and last.id == a
    assert db.get_message(c).is_voice is True
    db.set_tg_message_id(b, 42)
    assert db.get_message(b).tg_message_id == 42
