from family_ea.db import Database


def test_memory_fts_prefix_and_soft_delete(db: Database) -> None:
    mid = db.insert_message("oleh", "oleh", "hi")
    m1 = db.create_memory("Газовик Петро замінив клапан у котлі, тел +380501234567", "oleh", mid)
    m2 = db.create_memory("Стоматолог Олі — вул. Хрещатик 1", "anna", mid)

    hits = db.search_memories('"газов"* OR "котл"*')
    assert [m.id for m in hits] == [m1]

    assert db.delete_memory(m1) is True
    assert db.delete_memory(m1) is False  # already deleted
    assert db.search_memories('"газов"*') == []
    assert [m.id for m in db.list_memories()] == [m2]


def test_search_memories_swallows_bad_query(db: Database) -> None:
    assert db.search_memories('"unbalanced') == []
    assert db.search_memories("") == []


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

    assert [x.id for x in db.search_commitments("стомат")] == [cid]


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
