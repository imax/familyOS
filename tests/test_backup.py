"""The backup archive: a checked database snapshot plus the notes as Markdown, zipped."""

import sqlite3
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from family_ea.backup import build_archive, notes_markdown, snapshot_bytes
from family_ea.db import Database
from family_ea.family import Family
from family_ea.files import FileStore
from family_ea.main import backup
from tests.conftest import KYIV
from tests.test_web import JPEG, _settings


def _seed(db: Database) -> None:
    db.create_entry("Заміна масла: 2 400 грн\nСТО на Лівобережній", "2026-08-30", "oleh", 1)
    db.create_entry("Купили новий чайник", "2026-09-02", "anna", 2)
    db.create_entry("Поміняли фільтр у котлі", "2026-09-05", "oleh", 3)
    gone = db.create_entry("помилкова", "2026-09-06", "anna", 4)
    db.delete_entry(gone)


def test_notes_markdown_is_the_journal_page_as_text(db: Database, family: Family) -> None:
    _seed(db)
    assert notes_markdown(db, family) == (
        "# Нотатки\n"
        "\n"
        "## Вересень 2026\n"
        "\n"
        "- **2026-09-05**, Олег: Поміняли фільтр у котлі\n"
        "\n"
        "- **2026-09-02**, Анна: Купили новий чайник\n"
        "\n"
        "## Серпень 2026\n"
        "\n"
        "- **2026-08-30**, Олег: Заміна масла: 2 400 грн\n"
        "  СТО на Лівобережній\n"
    )


def test_notes_markdown_when_empty(db: Database, family: Family) -> None:
    assert notes_markdown(db, family) == "# Нотатки\n\nпоки порожньо\n"


def test_snapshot_is_a_checked_copy(db: Database, tmp_path: Path) -> None:
    _seed(db)
    data = snapshot_bytes(db)
    copy = tmp_path / "copy.db"
    copy.write_bytes(data)
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert conn.execute("SELECT count(*) FROM journal").fetchone() == (4,)
    conn.close()


def test_archive_holds_the_snapshot_and_the_notes(db: Database, family: Family) -> None:
    _seed(db)
    name, data, missing = build_archive(
        db, family, KYIV, now=datetime(2026, 9, 11, 3, 30, tzinfo=KYIV)
    )
    assert name == "family-2026-09-11.zip" and missing == []
    with zipfile.ZipFile(BytesIO(data)) as zf:
        assert zf.namelist() == ["family-2026-09-11.db", "notes.md"]
        assert zf.read("family-2026-09-11.db")[:16] == b"SQLite format 3\x00"
        assert zf.read("notes.md").decode().startswith("# Нотатки\n\n## Вересень 2026\n")


def test_archive_holds_the_files_and_names_the_missing(
    db: Database, family: Family, tmp_path: Path
) -> None:
    store = FileStore(tmp_path / "files")
    sha = store.put(JPEG, "image/jpeg")
    m1 = db.insert_message("oleh", "oleh", "чек", photo_file_id="f")
    m2 = db.insert_message("anna", "anna", "той самий чек", photo_file_id="g")
    db.add_attachment(m1, sha, "image/jpeg", len(JPEG))
    db.add_attachment(m2, sha, "image/jpeg", len(JPEG))  # the same bytes twice: one entry
    db.add_attachment(m2, "0" * 64, "image/png", 1)  # never mirrored
    name, data, missing = build_archive(db, family, KYIV, store=store)
    assert missing == ["0" * 64]
    with zipfile.ZipFile(BytesIO(data)) as zf:
        assert zf.namelist()[2:] == [f"files/{sha[:2]}/{sha}.jpg"]
        assert zf.read(f"files/{sha[:2]}/{sha}.jpg") == JPEG
        assert zf.getinfo(f"files/{sha[:2]}/{sha}.jpg").compress_type == zipfile.ZIP_STORED


def test_backup_command_writes_the_archive(tmp_path: Path, capsys) -> None:
    src = Database(tmp_path / "src.db")
    Family(src, admin_telegram_id=1).add("Олег", 1, member_id="oleh")
    _seed(src)
    sha = FileStore(tmp_path / "src-files").put(JPEG, "image/jpeg")
    mid = src.insert_message("oleh", "oleh", "чек", photo_file_id="f")
    src.add_attachment(mid, sha, "image/jpeg", len(JPEG))
    src.add_attachment(mid, "0" * 64, "image/png", 1)
    src.close()
    path = backup(_settings(), tmp_path / "src.db", tmp_path / "src-files", tmp_path / "backups")
    assert path.parent == tmp_path / "backups"
    assert path.name.startswith("family-") and path.suffix == ".zip"
    with zipfile.ZipFile(path) as zf:
        assert f"files/{sha[:2]}/{sha}.jpg" in zf.namelist()
    out = capsys.readouterr().out
    assert str(path) in out and "WARNING: 1 file(s)" in out and "0" * 64 in out
