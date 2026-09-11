from datetime import datetime

import pytest

from family_ea.context import (
    Agenda,
    bucket_commitments,
    build_context,
    digest_text,
    fmt_due,
    fts_query,
    render_digest,
    stale_label,
    stems,
    today_blocks,
    today_lines,
    word_pattern,
)
from family_ea.db import Commitment, Database, Member, TodayList
from family_ea.family import Family
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


def test_fmt_due() -> None:
    assert fmt_due(_c(1, due_at="2026-09-10T12:30:00Z"), KYIV) == "10.09 15:30"
    assert fmt_due(_c(1, due_from="2026-09-08", due_to="2026-09-20"), KYIV) == "08.09–20.09"
    assert fmt_due(_c(1, due_from="2026-09-08"), KYIV) == "08.09"
    assert fmt_due(_c(1), KYIV) == ""


def test_search_stems() -> None:
    # endings go, long words keep a 5-char prefix, function words and digits drop
    assert fts_query("Хто ремонтував котел?") == '"ремон"* OR "котел"*'
    assert fts_query("ок") == ""
    assert fts_query('він сказав "привіт" 12345') == '"сказа"* OR "приві"*'
    assert stems("діти дітям дітьми") == ["діт"]
    assert stems("Коля Колі Колею коли") == ["кол"]  # «коли» is a function word
    assert stems("Марія Марії Марією") == ["марі"]
    assert stems("Оля Олю кум кумом") == ["ол", "кум"]
    assert stems("газовика стоматологу") == ["газов", "стома"]
    assert word_pattern("діти, Коля?") == r"\b(?:діт|кол)"
    assert word_pattern("що це") is None


def test_render_digest_caps_open_list(family: Family) -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    b = bucket_commitments([_c(i) for i in range(1, 9)], now)
    text = render_digest(Agenda(), b, family, KYIV, max_open=5)
    assert "і ще 3" in text
    assert "Сьогодні" not in text


def test_digest_text(family: Family) -> None:
    now = datetime(2026, 9, 10, 8, 30, tzinfo=KYIV)
    items = [
        _c(1, text="Стоматолог", owner="anna", due_at="2026-09-10T12:30:00Z"),
        _c(2, text="Поговорити з Марією", due_to="2026-09-09"),
        _c(3, text="Купити лампочки"),
    ]
    b = bucket_commitments(items, now)
    text = digest_text(Agenda(), b, family, KYIV)
    assert text == (
        "Справи на сьогодні:\n- Стоматолог (Анна, 10.09 15:30)\n"
        "Прострочено:\n- Поговорити з Марією (09.09)"
    )
    assert "Купити лампочки" not in text and "[#" not in text  # no undated ones, no ids

    only_undated = bucket_commitments([_c(3)], now)
    assert digest_text(Agenda(), only_undated, family, KYIV) is None  # nothing to say
    assert digest_text(Agenda(), bucket_commitments([], now), family, KYIV) is None


def test_today_blocks_and_lines(family: Family) -> None:
    now = datetime(2026, 9, 11, 8, 0, tzinfo=KYIV)
    lists = {
        "oleh": TodayList(1, "oleh", "сходити на НП\nпланка\n", "2026-09-10T18:00:00Z", "oleh"),
        "anna": TodayList(2, "anna", "вода", "2026-09-11T04:30:00Z", "anna"),  # this morning
    }
    blocks = today_blocks(lists, family, "anna", now)  # the viewer's own first
    assert [(b.member, b.text, b.stale) for b in blocks] == [
        ("anna", "вода", ""),
        ("oleh", "сходити на НП\nпланка", "вчора"),
    ]
    assert today_lines(blocks, "anna") == [
        "На сьогодні (твоє):",
        "- вода",
        "На сьогодні (Олег, оновлено вчора):",
        "- сходити на НП",
        "- планка",
    ]
    assert stale_label("2026-09-01T10:00:00Z", now) == "01.09"
    empty = today_blocks({}, family, "oleh", now)
    assert [(b.member, b.text, b.stale) for b in empty] == [("oleh", "", ""), ("anna", "", "")]
    assert today_lines(empty, "oleh") == []

    head = today_lines(blocks, "anna")[:2]
    nothing = bucket_commitments([], now)
    assert digest_text(Agenda(), nothing, family, KYIV, today=head) == (
        "На сьогодні (твоє):\n- вода"
    )
    assert digest_text(Agenda(), nothing, family, KYIV, today=[]) is None


def test_context_shows_today_boards(
    db: Database, family: Family, oleh: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-10T05:12:00Z")
    db.save_today_list("oleh", "сходити на НП\nпланка", "oleh")
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, family, now, oleh, "привіт")
    assert (
        "## Списки на сьогодні (today: дошка кожного, змінюється лише на явне прохання)\n"
        "- oleh (Олег), оновлено 10.09 08:12:\n  сходити на НП\n  планка\n"
        "- anna (Анна): порожньо\n" in ctx
    )


def test_build_context_sections(
    db: Database, family: Family, oleh: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T12:00:00Z")
    mid = db.insert_message("oleh", "oleh", "Газовик Петро ремонтував котел")
    db.create_entry("Газовик Петро замінив клапан у котлі", "2026-09-09", "oleh", mid)
    db.create_item(
        "Паспорт Олі",
        owner="Оля",
        place="квартира",
        spot="білий комод",
        note=None,
        created_by="oleh",
        source_message_id=mid,
    )
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-07-01T12:00:00Z")
    db.create_entry("Котел чистили, 800 грн", "2026-07-01", "anna", mid)  # old: search only
    db.create_item(
        "Ключі від офісу",
        owner=None,
        place="офіс",
        spot="сейф",
        note="запасні",
        created_by="anna",
        source_message_id=mid,
    )
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T12:00:00Z")
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
    assert "## Сім'я (пишуть боту; решта людей — у фактах і нотатках)\n- oleh: Олег" in ctx
    assert "[#1] Поговорити з пані Марією (Олег, 08.09–20.09)" in ctx
    assert "Справи на сьогодні:\n- [#1]" in ctx
    assert "## Події (минулі за 7 днів і всі майбутні)\nнемає" in ctx
    assert "## Нотатки (journal) за останні 2 дні\n- [#1] 09.09, Олег: Газовик Петро" in ctx
    assert "## Старіші нотатки, схожі на повідомлення\n- [#2] 01.07, Анна: Котел чистили" in ctx
    assert "## Речі (items), змінені за останні 2 дні\n- [#1] Паспорт Олі (Оля) → квартира" in ctx
    assert "## Речі, схожі на повідомлення\nнемає" in ctx
    assert "## Відомі місця (place), де лежать речі\nквартира (1), офіс (1)" in ctx
    ctx2 = build_context(db, family, now, oleh, "Де ключі від офісу?")
    assert "## Речі, схожі на повідомлення\n- [#2] Ключі від офісу → офіс / сейф; запасні" in ctx2
    assert "[09.09 15:00] бот → Олег: Записав." in ctx
    assert ctx.rstrip().endswith("від oleh (Олег):\nХто ремонтував котел?")
