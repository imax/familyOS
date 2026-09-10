from family_ea.db import Database
from family_ea.family import Family
from family_ea.llm import LlmResult
from family_ea.ops import apply_ops, normalize_datetime
from tests.conftest import KYIV

SPEC_EXAMPLE = {
    "reply": "Записав.",
    "memories": [
        {"op": "create", "text": "Газовик Петро замінив клапан у котлі 9.09. Тел +380…"},
        {"op": "delete", "id": 5},
    ],
    "events": [
        {
            "op": "create",
            "text": "Стоматолог Олі",
            "who": "anna",
            "starts_at": "2026-09-10T15:30:00+03:00",
        },
        {"op": "cancel", "id": 9},
    ],
    "commitments": [
        {"op": "create", "text": "Попрати форму Олі", "owner": "anna", "due_from": "2026-09-10"},
        {"op": "create", "text": "Стоматолог", "owner": "anna", "due_at": "2026-09-10T12:30:00Z"},
        {"op": "update", "id": 12, "due_to": "2026-09-23"},
        {"op": "close", "id": 7, "status": "done"},
    ],
}


def test_schema_accepts_spec_example() -> None:
    r = LlmResult.model_validate(SPEC_EXAMPLE)
    assert r.reply == "Записав."
    assert [m.op for m in r.memories] == ["create", "delete"]
    assert [e.op for e in r.events] == ["create", "cancel"]
    assert LlmResult.model_validate({"reply": "Ок."}).events == []
    assert r.commitments[1].due_at == "2026-09-10T12:30:00Z"
    assert LlmResult.model_validate({"reply": "Ок."}).commitments == []


def test_normalize_datetime() -> None:
    assert normalize_datetime("2026-09-10T15:30:00+03:00", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("2026-09-10T15:30:00", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("2026-09-10T12:30:00Z", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("завтра", KYIV) is None
    assert normalize_datetime(None, KYIV) is None


def test_apply_ops_spec_example(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    applied = apply_ops(
        db,
        LlmResult.model_validate(SPEC_EXAMPLE),
        author_id="oleh",
        message_id=mid,
        family=family,
        tz=KYIV,
    )
    by = {(a.kind, a.op): a for a in applied}
    assert by[("memory", "create")].ok and by[("memory", "create")].id == 1
    assert by[("memory", "delete")].ok is False  # id 5 never existed
    assert by[("event", "create")].ok and db.get_event(1).starts_at == "2026-09-10T12:30:00Z"
    assert by[("event", "cancel")].ok is False  # id 9 never existed
    assert by[("commitment", "update")].ok is False
    assert by[("commitment", "close:done")].ok is False
    created = [a for a in applied if a.kind == "commitment" and a.op == "create"]
    assert all(a.ok for a in created) and len(created) == 2
    open_items = db.open_commitments()
    assert [c.text for c in open_items] == ["Попрати форму Олі", "Стоматолог"]
    assert open_items[0].owner == "anna" and open_items[0].due_from == "2026-09-10"
    assert open_items[1].due_at == "2026-09-10T12:30:00Z"


def test_apply_ops_validates_and_updates(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    r = LlmResult.model_validate(
        {
            "reply": "",
            "commitments": [
                {"op": "create", "text": "Щось", "owner": "olia", "due_at": "коли-небудь"},
                {"op": "create", "text": "   "},
            ],
            "memories": [{"op": "create", "text": ""}],
        }
    )
    applied = apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [a.ok for a in applied] == [False, True, False]
    first = applied[1]
    assert "unknown owner" in first.note and "bad due_at" in first.note
    c = db.get_commitment(first.id)
    assert c and c.owner is None and c.due_at is None

    r2 = LlmResult.model_validate(
        {
            "reply": "",
            "commitments": [
                {"op": "update", "id": c.id, "due_at": "2026-09-10T15:30+03:00", "owner": "oleh"},
                {"op": "close", "id": c.id},
                {"op": "update", "id": c.id, "text": "after close"},
            ],
        }
    )
    applied = apply_ops(db, r2, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [(a.op, a.ok) for a in applied] == [
        ("update", True),
        ("close:done", True),
        ("update", False),
    ]
    c = db.get_commitment(c.id)
    assert c and c.due_at == "2026-09-10T12:30:00Z" and c.owner == "oleh" and c.status == "done"
