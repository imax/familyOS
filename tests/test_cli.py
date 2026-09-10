"""`GET /backup.db`, `pull` and `log`: getting the production database onto a laptop."""

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from family_ea.db import Database
from family_ea.family import Family
from family_ea.main import llm_result_lines, pull, show_log
from family_ea.web import build_web
from tests.test_web import _auth, _settings

LLM_RESULT = (
    '{"model": "m", "usage": {"input_tokens": 10, "output_tokens": 2},'
    ' "output": {"reply": "Записав.", "commitments": [{"op": "create", "text": "Стоматолог",'
    ' "owner": "anna", "due_at": "2000-01-01T15:30:00+02:00"}]},'
    ' "applied": [{"kind": "commitment", "op": "create", "id": 1, "ok": true, "note": ""}]}'
)

OP_LINE = "commitment create: text='Стоматолог', owner='anna', due_at='2000-01-01T15:30:00+02:00'"


def _seed(db: Database) -> None:
    mid = db.insert_message("oleh", "oleh", "Стоматолог завтра о 15:30", tg_message_id=1)
    db.set_llm_result(mid, LLM_RESULT)
    db.insert_message("bot", "oleh", "Записав.")
    db.create_commitment("Стоматолог", owner="anna", created_by="oleh", source_message_id=mid)


def _rows(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT user_id, raw_text FROM messages ORDER BY id").fetchall()
    finally:
        conn.close()


def test_backup_endpoint_is_a_consistent_sqlite_file(
    db: Database, family: Family, tmp_path: Path
) -> None:
    _seed(db)
    client = TestClient(build_web(_settings(), family, db))
    assert client.get("/backup.db").status_code == 401

    response = client.get("/backup.db", headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.sqlite3")
    assert 'filename="family-' in response.headers["content-disposition"]
    copy = tmp_path / "copy.db"
    copy.write_bytes(response.content)
    assert _rows(copy) == [("oleh", "Стоматолог завтра о 15:30"), ("bot", "Записав.")]
    # a snapshot, not a link: writing to the copy leaves the live database alone
    sqlite3.connect(copy).execute("DELETE FROM messages").connection.commit()
    assert len(db.list_messages()) == 2


def test_pull_downloads_the_snapshot_and_drops_sftp_leftovers(
    db: Database, family: Family, tmp_path: Path
) -> None:
    _seed(db)
    app = build_web(_settings(), family, db)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        with TestClient(app) as client:
            r = client.get(request.url.path, headers=dict(request.headers))
        return httpx.Response(r.status_code, content=r.content, headers=dict(r.headers))

    dest = tmp_path / "data" / "prod.db"
    dest.parent.mkdir()
    Path(f"{dest}-wal").write_bytes(b"stale")  # what `fly ssh sftp get` used to leave behind
    pull(
        _settings(web_url="https://ea.example/"),
        dest,
        transport=httpx.MockTransport(handler),
    )
    assert [str(r.url) for r in seen] == ["https://ea.example/backup.db"]
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert _rows(dest)[0] == ("oleh", "Стоматолог завтра о 15:30")
    assert not Path(f"{dest}-wal").exists()


def test_pull_needs_a_url(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="WEB_URL"):
        pull(_settings(web_url=None), tmp_path / "prod.db")


def test_llm_result_lines() -> None:
    assert llm_result_lines(LLM_RESULT) == [
        "m: 10 in, 2 out",
        OP_LINE,
        "[ok] commitment create #1",
    ]
    assert llm_result_lines('{"error": "boom"}') == ["error: boom"]


def test_show_log_prints_messages_with_ops(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "family.db"
    db = Database(path)
    Family(db, admin_telegram_id=1).add("Олег", 1, member_id="oleh")
    _seed(db)
    db.close()

    show_log(_settings(database_path=path), path, last=10)
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("Олег: Стоматолог завтра о 15:30")
    assert out[1:4] == [
        "    m: 10 in, 2 out",
        f"    {OP_LINE}",
        "    [ok] commitment create #1",
    ]
    assert out[4].endswith("bot -> Олег: Записав.")
