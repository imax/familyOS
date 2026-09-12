"""Files that came with messages: the store on disk, the links to notes and items, the rows
on the item page."""

import json
from pathlib import Path

from family_ea.db import Database
from family_ea.files import (
    FileStore,
    applied_of,
    files_for,
    sha256_hex,
)

JPEG = b"\xff\xd8\xff\xe0not-really-a-jpeg"


def _applied(*ops: tuple[str, str, int, bool]) -> str:
    return json.dumps(
        {"applied": [{"kind": k, "op": op, "id": i, "ok": ok, "note": ""} for k, op, i, ok in ops]}
    )


def _item(db: Database, name: str, mid: int) -> int:
    return db.create_item(
        name,
        owner=None,
        place="сейф",
        spot=None,
        note=None,
        created_by="oleh",
        source_message_id=mid,
    )


def test_store_is_content_addressed(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "files")
    sha = store.put(JPEG, "image/jpeg")
    assert sha == sha256_hex(JPEG) and len(sha) == 64
    path = store.path(sha, "image/jpeg")
    assert path == tmp_path / "files" / sha[:2] / f"{sha}.jpg"
    assert path.read_bytes() == JPEG and store.has(sha, "image/jpeg")
    assert store.put(JPEG, "image/jpeg") == sha  # the same bytes again: one file
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # and no temp leftovers
    assert store.path("ab" * 32, "application/pdf").suffix == ".pdf"
    assert store.path("ab" * 32, "text/x-unknown").suffix == ".bin"
    assert not store.has("ab" * 32, "image/png")


def test_files_follow_their_message_into_notes_and_items(db: Database) -> None:
    # a photo the model made a note and an item of
    m1 = db.insert_message("oleh", "oleh", "додай у нотатки", photo_file_id="f1")
    a1 = db.add_attachment(m1, "a" * 64, "image/jpeg", 3)
    e1 = db.create_entry("ТО: 4 500 грн", "2026-09-11", "oleh", m1)
    i1 = _item(db, "Сервісна книжка", m1)
    db.set_llm_result(m1, _applied(("entry", "create", e1, True), ("item", "create", i1, True)))
    # a second photo that updated the same note («ось ще скан»); a failed op links nothing
    m2 = db.insert_message("anna", "anna", "ось ще скан", photo_file_id="f2")
    a2 = db.add_attachment(m2, "b" * 64, "image/jpeg", 3)
    db.set_llm_result(m2, _applied(("entry", "update", e1, True), ("entry", "update", 5, False)))
    # a photo the model made nothing of, and a note from a message without a photo
    m3 = db.insert_message("oleh", "oleh", "", photo_file_id="f3")
    db.add_attachment(m3, "c" * 64, "image/jpeg", 3)
    m4 = db.insert_message("oleh", "oleh", "без фото")
    e2 = db.create_entry("Без фото", "2026-09-11", "oleh", m4)

    by_entry = files_for(db, "entry", db.list_entries())
    assert [a.id for a in by_entry[e1]] == [a1, a2]  # made by m1, touched by m2, in that order
    assert e2 not in by_entry
    item = db.get_item(i1)
    assert item and [a.id for a in files_for(db, "item", [item])[i1]] == [a1]
    assert files_for(db, "entry", []) == {}


def test_files_for_without_any_file_is_empty(db: Database) -> None:
    m = db.insert_message("oleh", "oleh", "x")
    e = db.create_entry("x", "2026-09-11", "oleh", m)
    assert files_for(db, "entry", [db.get_entry(e)]) == {}


def test_applied_of_tolerates_missing_or_odd_results(db: Database) -> None:
    m = db.insert_message("oleh", "oleh", "x")
    msg = db.get_message(m)
    assert msg and applied_of(msg) == []
    for raw in ("not json", '{"error": "boom"}', '{"applied": [1, "x"]}'):
        db.set_llm_result(m, raw)
        msg = db.get_message(m)
        assert msg and applied_of(msg) == []
