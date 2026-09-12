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


def test_todos_keep_the_order_dragged_on_the_web(db: Database) -> None:
    mid = db.insert_message("oleh", "oleh", "...")

    def new(text: str) -> int:
        return db.create_todo(text, owner=None, created_by="oleh", source_message_id=mid)

    a, b, c = new("a"), new("b"), new("c")
    assert [x.id for x in db.open_todos()] == [c, b, a]  # newest first until someone drags

    db.reorder_todos([c, a, 999, b])  # 999: no such todo, ignored
    assert [(x.id, x.position) for x in db.open_todos()] == [(c, 1), (a, 2), (b, 4)]
    d = new("d")  # new since the page was drawn: on top, until placed
    assert [x.id for x in db.open_todos()] == [d, c, a, b]

    db.close_todo(a, "done")
    db.reorder_todos([a, d, c])  # a stale page: a is closed, ignored; b unlisted: on top
    assert [(x.id, x.position) for x in db.open_todos()] == [(b, None), (d, 2), (c, 3)]
    assert [x.id for x in db.recent_done_todos(5)] == [a]


def test_commitments_become_todos(tmp_path: Path) -> None:
    """A database from before 2026-09-12 has `commitments` with a time or a window; the
    first start copies them into `todos` with a deadline day and drops the old table."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE commitments (id INTEGER PRIMARY KEY, text TEXT NOT NULL, owner TEXT,
          status TEXT NOT NULL, due_at TEXT, due_from TEXT, due_to TEXT, position INTEGER,
          created_by TEXT NOT NULL, created_at TEXT NOT NULL, source_message_id INTEGER NOT NULL,
          closed_at TEXT);
        INSERT INTO commitments VALUES
          (1, 'Стоматолог', 'anna', 'open', '2026-09-10T21:30:00Z', NULL, NULL, NULL, 'oleh',
           '2026-09-10T09:00:00Z', 1, NULL),
          (2, 'Вікно', NULL, 'open', NULL, '2026-09-14', '2026-09-20', 2, 'oleh',
           '2026-09-10T09:00:00Z', 1, NULL),
          (3, 'Без дати', NULL, 'done', NULL, NULL, NULL, NULL, 'oleh',
           '2026-09-10T09:00:00Z', 1, '2026-09-11T09:00:00Z');
        """
    )
    conn.close()

    db = Database(path)
    # 21:30Z is 00:30 of the next day in Kyiv: the deadline is that day; a window ends there
    assert [(t.id, t.due, t.position) for t in db.open_todos()] == [
        (1, "2026-09-11", None),
        (2, "2026-09-20", 2),
    ]
    done = db.get_todo(3)
    assert done and done.status == "done" and done.due is None
    assert done.closed_at == "2026-09-11T09:00:00Z"
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "commitments" not in tables and "todos" in tables
    db.close()
    again = Database(path)  # the second start finds nothing to do
    assert [t.id for t in again.open_todos()] == [1, 2]
    again.close()


def test_today_lists_latest_per_member(db: Database) -> None:
    assert db.current_today_lists() == {}
    db.save_today_list("oleh", "планка", "oleh")
    db.save_today_list("anna", "вода", "anna")
    db.save_today_list("oleh", "планка, авто", "anna")  # the partner edited it
    boards = db.current_today_lists()
    assert {m: (b.text, b.created_by) for m, b in boards.items()} == {
        "oleh": ("планка, авто", "anna"),
        "anna": ("вода", "anna"),
    }


def test_todo_lifecycle(db: Database) -> None:
    mid = db.insert_message("anna", "anna", "завтра стоматолог")
    cid = db.create_todo(
        "Стоматолог",
        owner="anna",
        created_by="anna",
        source_message_id=mid,
        due="2026-09-10",
    )
    assert [c.id for c in db.open_todos()] == [cid]

    assert db.update_todo(cid, due="2026-09-23") is True
    assert db.update_todo(cid, bogus="x") is False
    c = db.get_todo(cid)
    assert c and c.due == "2026-09-23"

    assert db.close_todo(cid, "done") is True
    assert db.close_todo(cid, "done") is False  # not open any more
    assert db.close_todo(999, "done") is False  # does not exist
    assert db.open_todos() == []
    assert db.update_todo(cid, text="x") is False
    c = db.get_todo(cid)
    assert c and c.status == "done" and c.closed_at

    assert [x.id for x in db.search_todos(r"\bстомат")] == [cid]
    assert db.search_todos(r"\bтомат") == []  # a word start, not a substring


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


def test_item_lifecycle_writes_history(db: Database) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    a = db.create_item(
        "Паспорт Олі",
        owner="Оля",
        place="офіс",
        spot="сейф",
        note=None,
        created_by="oleh",
        source_message_id=mid,
    )
    b = db.create_item(
        "Мерч",
        owner=None,
        place="офіс",
        spot="каморка",
        note="футболки 2024",
        created_by="anna",
        source_message_id=mid,
    )
    c = db.create_item(
        "Мерч",
        owner=None,
        place="будинок",
        spot=None,
        note="худі 2025",
        created_by="anna",
        source_message_id=mid,
    )
    move = db.update_item(
        a, {"place": "квартира", "spot": "білий комод"}, who="anna", source_message_id=mid
    )
    assert move == "moved"
    assert db.update_item(a, {"owner": "Оля"}, who="anna", source_message_id=mid) is None  # same
    fix = db.update_item(
        a,
        {"name": "Паспорт Олі (закордонний)", "note": "до 2031"},
        who="oleh",
        source_message_id=mid,
    )
    assert fix == "corrected"
    item = db.get_item(a)
    assert (
        item and item.location == "квартира / білий комод" and item.name.endswith("(закордонний)")
    )
    assert [(h.kind, h.place, h.spot, h.detail, h.who) for h in db.item_history(a)] == [
        (
            "corrected",
            "квартира",
            "білий комод",
            "назва: Паспорт Олі (закордонний); примітка: до 2031",
            "oleh",
        ),
        ("moved", "квартира", "білий комод", None, "anna"),
        ("created", "офіс", "сейф", None, "oleh"),
    ]

    assert db.places() == [("будинок", 1), ("квартира", 1), ("офіс", 1)]
    assert [i.id for i in db.list_items(place="ОФІС")] == [b]
    assert [i.id for i in db.list_items(owner="оля")] == [a]
    assert sorted(i.id for i in db.search_items(r"\bмерч")) == [b, c]
    assert [i.id for i in db.search_items(r"\bхуд")] == [c]  # the note is searched too
    assert sorted(i.id for i in db.items_changed_since("2000-01-01T00:00:00Z")) == [a, b, c]

    assert db.remove_item(b, who="oleh", source_message_id=mid) is True
    assert db.remove_item(b, who="oleh", source_message_id=mid) is False
    assert db.update_item(b, {"place": "x"}, who="oleh", source_message_id=mid) is None  # gone
    assert db.item_history(b)[0].kind == "gone" and db.get_item(b).removed_at
    assert sorted(i.id for i in db.recent_items()) == [a, c]
    assert db.list_items(place="офіс") == [] and db.places() == [("будинок", 1), ("квартира", 1)]


def test_attachments_get_a_description_column(tmp_path: Path) -> None:
    """An `attachments` table from the first files release has no `description`."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE attachments (id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL,
          sha256 TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, name TEXT,
          created_at TEXT NOT NULL);
        INSERT INTO attachments VALUES (1, 1, 'ab', 'image/jpeg', 3, NULL, '2026-09-11T15:00:00Z');
        """
    )
    conn.close()

    db = Database(path)
    [old] = db.list_attachments()
    assert old.description is None and old.sha256 == "ab"
    db.describe_attachments(1, "Чек")
    assert db.list_attachments()[0].description == "Чек"
    db.close()


def test_messages_get_a_photo_column(tmp_path: Path) -> None:
    """A database from before photos has no `photo_file_id`; the first start adds it."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
          chat_with TEXT NOT NULL, tg_message_id INTEGER, created_at TEXT NOT NULL,
          raw_text TEXT NOT NULL, is_voice INTEGER NOT NULL DEFAULT 0, llm_result TEXT);
        INSERT INTO messages VALUES
          (1, 'oleh', 'oleh', 5, '2026-09-10T09:00:00Z', 'Привіт', 0, NULL);
        """
    )
    conn.close()

    db = Database(path)
    old = db.get_message(1)
    assert old and old.photo_file_id is None and old.raw_text == "Привіт"
    new_id = db.insert_message("oleh", "oleh", "чек", photo_file_id="AgAC")
    new = db.get_message(new_id)
    assert new and new.photo_file_id == "AgAC" and not new.is_voice
    db.close()
