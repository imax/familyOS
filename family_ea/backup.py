"""The backup archive: a consistent snapshot of the database plus the notes as one Markdown
file, zipped. Built by code only (no LLM); `python -m family_ea backup` writes one locally,
the nightly mail job will send the same bytes."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .context import group_by_month
from .db import Database, Entry
from .family import Family


def notes_markdown(db: Database, family: Family) -> str:
    """All active notes as one Markdown document: a heading per month, newest first,
    an entry per note with its day and author, the way the Нотатки page shows them."""
    lines = ["# Нотатки", ""]
    months = group_by_month(db.list_entries(limit=100_000))
    if not months:
        lines += ["поки порожньо", ""]
    for title, entries in months:
        lines += [f"## {title}", ""]
        for e in entries:
            lines += [_entry_markdown(e, family), ""]
    return "\n".join(lines)


def _entry_markdown(e: Entry, family: Family) -> str:
    # Continuation lines are indented so a multi-line note stays one list item.
    text = e.text.strip().replace("\n", "\n  ")
    return f"- **{e.date}**, {family.display_name(e.created_by)}: {text}"


def snapshot_bytes(db: Database) -> bytes:
    """The online backup as bytes, checked with `integrity_check` before it goes anywhere."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.db"
        db.backup_to(path)
        copy = sqlite3.connect(path)
        try:
            (verdict,) = copy.execute("PRAGMA integrity_check").fetchone()
        finally:
            copy.close()
        if verdict != "ok":
            raise RuntimeError(f"database snapshot failed integrity_check: {verdict}")
        return path.read_bytes()


def build_archive(
    db: Database, family: Family, tz: ZoneInfo, now: datetime | None = None
) -> tuple[str, bytes]:
    """(file name, zip bytes): `family-YYYY-MM-DD.db` and `notes.md` in `family-YYYY-MM-DD.zip`."""
    stamp = (now or datetime.now(tz)).astimezone(tz).strftime("%Y-%m-%d")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"family-{stamp}.db", snapshot_bytes(db))
        zf.writestr("notes.md", notes_markdown(db, family))
    return f"family-{stamp}.zip", buf.getvalue()


def write_archive(db: Database, family: Family, tz: ZoneInfo, dest_dir: Path) -> Path:
    name, data = build_archive(db, family, tz)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    path.write_bytes(data)
    return path
