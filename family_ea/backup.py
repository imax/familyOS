"""The backup archive: a consistent snapshot of the database and the files it refers to,
zipped. Built by code only (no LLM);
`python -m family_ea backup` writes one locally, the nightly mail job will send the same
bytes. Restore: the .db to DATABASE_PATH, `files/` to FILES_DIR."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import Database
from .files import FileStore


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
    db: Database,
    tz: ZoneInfo,
    now: datetime | None = None,
    store: FileStore | None = None,
) -> tuple[str, bytes, list[str]]:
    """(file name, zip bytes, missing files): `family-YYYY-MM-DD.db` and, with a `store`,
    `files/ab/ab12….jpg` for every row of `attachments` whose bytes are there; the hashes
    of those that are not come back so the caller can say so."""
    stamp = (now or datetime.now(tz)).astimezone(tz).strftime("%Y-%m-%d")
    buf = io.BytesIO()
    missing: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"family-{stamp}.db", snapshot_bytes(db))
        seen: set[Path] = set()
        for a in db.list_attachments() if store else []:
            assert store
            path = store.path(a.sha256, a.mime)
            if path in seen:
                continue  # the same bytes sent twice: one file
            seen.add(path)
            if not path.is_file():
                missing.append(a.sha256)
                continue
            # already compressed (jpeg, png, pdf): stored as is
            zf.write(path, f"files/{path.relative_to(store.root)}", zipfile.ZIP_STORED)
    return f"family-{stamp}.zip", buf.getvalue(), missing


def write_archive(
    db: Database, tz: ZoneInfo, dest_dir: Path, store: FileStore | None = None
) -> tuple[Path, list[str]]:
    name, data, missing = build_archive(db, tz, store=store)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    path.write_bytes(data)
    return path, missing
