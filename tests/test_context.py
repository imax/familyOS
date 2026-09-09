from datetime import datetime

from family_ea.context import (
    bucket_commitments,
    build_context,
    fmt_due,
    fts_query,
    render_digest,
)
from family_ea.db import Commitment, Database
from family_ea.family import Family, Member
from tests.conftest import KYIV


def _c(id: int, **kw) -> Commitment:
    base = dict(
        text=f"c{id}",
        owner=None,
        status="open",
        due_at=None,
        due_from=None,
        due_to=None,
        created_by="oleh",
        created_at="2026-09-01T00:00:00Z",
        source_message_id=1,
        closed_at=None,
    )
    base.update(kw)
    return Commitment(id=id, **base)


def test_bucket_commitments() -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)  # 05:00Z
    items = [
        _c(1, due_at="2026-09-10T12:30:00Z"),  # today 15:30 Kyiv
        _c(2, due_at="2026-09-10T04:00:00Z"),  # today 07:00 Kyiv, already passed -> overdue
        _c(3, due_at="2026-09-11T09:00:00Z"),  # tomorrow -> later
        _c(4, due_from="2026-09-08", due_to="2026-09-20"),  # window covers today
        _c(5, due_from="2026-09-09"),  # was for yesterday, no end -> overdue
        _c(6, due_to="2026-09-09"),  # ended yesterday -> overdue
        _c(7, due_from="2026-09-12"),  # future window -> later
        _c(8),  # no dates -> open
        _c(9, status="done", due_at="2026-09-10T12:30:00Z"),  # closed, ignored
    ]
    b = bucket_commitments(items, now)
    assert [c.id for c in b.today] == [1, 4]
    assert {c.id for c in b.overdue} == {2, 5, 6}
    assert [c.id for c in b.later] == [3, 7]
    assert [c.id for c in b.open] == [8]
    assert b.digest_empty is False
    assert bucket_commitments([_c(7, due_from="2026-09-12")], now).digest_empty is True


def test_fmt_due() -> None:
    assert fmt_due(_c(1, due_at="2026-09-10T12:30:00Z"), KYIV) == "10.09 15:30"
    assert fmt_due(_c(1, due_from="2026-09-08", due_to="2026-09-20"), KYIV) == "08.09–20.09"
    assert fmt_due(_c(1, due_from="2026-09-08"), KYIV) == "08.09"
    assert fmt_due(_c(1), KYIV) == ""


def test_fts_query() -> None:
    assert fts_query("Хто ремонтував котел?") == '"ремон"* OR "котел"*'
    assert fts_query("ок") == ""
    assert fts_query('він сказав "привіт" 12345') == '"сказа"* OR "приві"*'


def test_render_digest_caps_open_list(family: Family) -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    b = bucket_commitments([_c(i) for i in range(1, 9)], now)
    text = render_digest(b, family, KYIV, max_open=5)
    assert "і ще 3" in text
    assert "Сьогодні" not in text


def test_build_context_sections(db: Database, family: Family, oleh: Member) -> None:
    mid = db.insert_message("oleh", "oleh", "Газовик Петро ремонтував котел")
    db.create_memory("Газовик Петро замінив клапан у котлі 9.09", "oleh", mid)
    db.create_commitment(
        "Поговорити з пані Марією",
        owner="oleh",
        created_by="oleh",
        source_message_id=mid,
        due_from="2026-09-08",
        due_to="2026-09-20",
    )
    db.insert_message("bot", "oleh", "Записав.")
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, family, now, oleh, "Хто ремонтував котел?")
    assert "2026-09-10 08:00 (Europe/Kyiv), четвер" in ctx
    assert "## Сім'я (пишуть боту; решта людей — у memories)\n- oleh: Олег\n- anna: Анна" in ctx
    assert "[#1] Поговорити з пані Марією (Олег, 08.09–20.09)" in ctx
    assert "Сьогодні:\n- [#1]" in ctx
    assert "[#1] 09.09, Олег: Газовик Петро" in ctx
    assert "бот → Олег: Записав." in ctx
    assert ctx.rstrip().endswith("від oleh (Олег):\nХто ремонтував котел?")
