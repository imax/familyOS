from datetime import datetime

from family_ea.db import Database
from family_ea.family import Family
from family_ea.llm import LlmResult
from family_ea.ops import apply_ops, normalize_datetime
from tests.conftest import KYIV

SPEC_EXAMPLE = {
    "reply": "Записав.",
    "journal": [
        {
            "op": "create",
            "text": "газовик Петро замінив клапан у котлі. Тел +380…",
            "date": "2026-09-09",
        },
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
    assert [j.op for j in r.journal] == ["create", "delete"]
    assert [e.op for e in r.events] == ["create", "cancel"]
    assert LlmResult.model_validate({"reply": "Ок."}).events == []
    assert r.commitments[1].due_at == "2026-09-10T12:30:00Z"
    assert LlmResult.model_validate({"reply": "Ок."}).commitments == []


def test_today_op_replaces_a_board(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")

    def run(payload: list[dict]) -> list:
        result = LlmResult.model_validate({"reply": "Ок.", "today": payload})
        return apply_ops(db, result, author_id="oleh", message_id=mid, family=family, tz=KYIV)

    applied = run(
        [
            {"text": " сходити на НП, планка "},
            {"member": "anna", "text": "вода"},
            {"member": "nobody", "text": "x"},
        ]
    )
    assert [(a.kind, a.op, a.ok, a.note) for a in applied] == [
        ("today", "set", True, ""),
        ("today", "set", True, "for anna"),
        ("today", "set", False, "unknown member 'nobody'"),
    ]
    boards = db.current_today_lists()
    assert boards["oleh"].text == "сходити на НП, планка" and boards["oleh"].created_by == "oleh"
    assert boards["anna"].text == "вода" and boards["anna"].created_by == "oleh"

    applied = run([{"text": "сходити на НП, планка"}, {"member": "anna", "text": ""}])
    assert [(a.ok, a.note) for a in applied] == [(False, "unchanged"), (True, "for anna")]
    assert db.current_today_lists()["anna"].text == ""  # cleared


def test_normalize_datetime() -> None:
    assert normalize_datetime("2026-09-10T15:30:00+03:00", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("2026-09-10T15:30:00", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("2026-09-10T12:30:00Z", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("завтра", KYIV) is None
    assert normalize_datetime("", KYIV) is None


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
    assert by[("entry", "create")].ok and by[("entry", "create")].id == 1
    entry = db.get_entry(1)
    assert entry and entry.text.startswith("Газовик Петро") and entry.date == "2026-09-09"
    assert by[("entry", "delete")].ok is False  # id 5 never existed
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
            "journal": [{"op": "create", "text": ""}],
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


def test_commitment_window_replaces_time_and_back(db: Database, family: Family) -> None:
    """«Сніданок завтра о 10» then «перенесли на наступний тиждень»: the 10:00 must go."""
    mid = db.insert_message("oleh", "oleh", "...")

    def apply(ops: list[dict]) -> None:
        r = LlmResult.model_validate({"reply": "", "commitments": ops})
        applied = apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)
        assert all(a.ok for a in applied), applied

    apply([{"op": "create", "text": "Сніданок", "due_at": "2026-09-11T10:00:00+03:00"}])
    apply([{"op": "update", "id": 1, "due_from": "2026-09-14", "due_to": "2026-09-20"}])
    c = db.get_commitment(1)
    assert c and (c.due_at, c.due_from, c.due_to) == (None, "2026-09-14", "2026-09-20")

    apply([{"op": "update", "id": 1, "due_at": "2026-09-16T10:00:00+03:00"}])
    c = db.get_commitment(1)
    assert c and (c.due_at, c.due_from, c.due_to) == ("2026-09-16T07:00:00Z", None, None)

    apply([{"op": "update", "id": 1, "due_to": "2026-09-19"}])  # «до суботи»: a window again
    c = db.get_commitment(1)
    assert c and (c.due_at, c.due_from, c.due_to) == (None, None, "2026-09-19")

    apply([{"op": "update", "id": 1, "text": "Сніданок з командою"}])  # text only: dates untouched
    c = db.get_commitment(1)
    assert c and (c.due_at, c.due_from, c.due_to) == (None, None, "2026-09-19")


def test_apply_journal_ops(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    r = LlmResult.model_validate(
        {
            "reply": "",
            "journal": [
                {"op": "create", "text": "  зробив ТО: масло, фільтри, 4500 грн"},
                {"op": "create", "text": "Обід з кумом", "date": "вчора"},
            ],
        }
    )
    applied = apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [a.ok for a in applied] == [True, True]
    today = datetime.now(KYIV).date().isoformat()
    first, second = db.get_entry(applied[0].id or 0), db.get_entry(applied[1].id or 0)
    assert first and first.text == "Зробив ТО: масло, фільтри, 4500 грн" and first.date == today
    assert second and second.date == today and "bad date" in applied[1].note

    r2 = LlmResult.model_validate(
        {
            "reply": "",
            "journal": [
                {
                    "op": "update",
                    "id": first.id,
                    "text": "зробив ТО: 4800 грн",
                    "date": "2026-09-10",
                },
                {"op": "update", "id": 99, "text": "x"},
                {"op": "update", "id": second.id},
                {"op": "delete", "id": second.id},
            ],
        }
    )
    applied = apply_ops(db, r2, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [a.ok for a in applied] == [True, False, False, True]
    assert applied[2].note == "nothing to update"
    first = db.get_entry(first.id)
    assert first and first.text == "Зробив ТО: 4800 грн" and first.date == "2026-09-10"
    assert [e.id for e in db.list_entries()] == [first.id]


def test_apply_item_ops(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    r = LlmResult.model_validate(
        {
            "reply": "",
            "items": [
                {
                    "op": "create",
                    "name": " Паспорт Олі ",
                    "owner": "Оля",
                    "place": "офіс",
                    "spot": "сейф",
                },
                {"op": "create", "name": "  "},
                {"op": "update", "id": 99, "place": "x"},
            ],
        }
    )
    applied = apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [(a.ok, a.note) for a in applied] == [
        (True, ""),
        (False, "empty name"),
        (False, "not found, gone or unchanged"),
    ]
    iid = applied[0].id or 0
    assert db.get_item(iid).name == "Паспорт Олі"

    r2 = LlmResult.model_validate(
        {
            "reply": "",
            "items": [
                {"op": "update", "id": iid, "place": "квартира"},  # the spot goes with the place
                {"op": "update", "id": iid},
                {"op": "update", "id": iid, "place": "квартира"},  # nothing new
                {"op": "update", "id": iid, "name": "", "owner": " "},  # blanks mean «not given»
                {"op": "remove", "id": iid},
                {"op": "remove", "id": iid},
            ],
        }
    )
    applied = apply_ops(db, r2, author_id="anna", message_id=mid, family=family, tz=KYIV)
    assert [(a.ok, a.note) for a in applied] == [
        (True, "moved"),
        (False, "nothing to update"),
        (False, "not found, gone or unchanged"),
        (False, "nothing to update"),
        (True, ""),
        (False, "not found or already gone"),
    ]
    item = db.get_item(iid)
    assert item and item.place == "квартира" and item.spot is None and item.owner == "Оля"
    assert item.name == "Паспорт Олі" and item.removed_at
