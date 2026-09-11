"""Process entry points: `serve` (bot + web in one event loop), `chat` (local REPL),
`pull` (snapshot of the deployed database) and `log` (messages with what the LLM did)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import uvicorn

from .auth import BACKUP_TTL, sign
from .backup import write_archive
from .bot import build_bot
from .config import Settings
from .context import fmt_dt
from .db import Database
from .family import Family
from .llm import Llm
from .pipeline import Pipeline
from .transcribe import Transcriber
from .web import build_web

log = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in ("httpx", "httpx2"):  # the anthropic SDK logs requests via httpx2
        logging.getLogger(name).setLevel(logging.WARNING)


def build_pipeline(settings: Settings, db: Database, family: Family) -> Pipeline:
    llm = Llm(settings.llm_model, settings.llm_effort, api_key=settings.anthropic_api_key)
    return Pipeline(db, family, llm, settings.tz)


async def serve(settings: Settings) -> None:
    settings.require("telegram_token", "anthropic_api_key", "web_secret")
    if settings.admin_user_id is None:
        # Bootstrap mode: nobody is let in, but the bot answers strangers with their id.
        log.warning("ADMIN_USER_ID is not set: write to the bot to learn your id, then set it")
    db = Database(settings.database_path)
    family = Family(db, settings.admin_user_id)
    pipeline = build_pipeline(settings, db, family)
    transcriber = Transcriber(settings.openai_api_key) if settings.openai_api_key else None

    tg_app = build_bot(settings, family, db, pipeline, transcriber)
    web_app = build_web(settings, family, db)
    server = uvicorn.Server(
        uvicorn.Config(web_app, host="0.0.0.0", port=settings.port, log_level="info")
    )

    log.info(
        "starting: model=%s db=%s members=%s voice=%s digest=%s %s",
        settings.llm_model,
        settings.database_path,
        [p.id for p in family.members],
        "on" if transcriber else "off",
        settings.digest_time.strftime("%H:%M"),
        settings.tz.key,
    )
    async with tg_app:
        await tg_app.start()
        assert tg_app.updater
        await tg_app.updater.start_polling()
        try:
            await server.serve()  # returns on SIGINT/SIGTERM
        finally:
            await tg_app.updater.stop()
            await tg_app.stop()
            db.close()


async def chat(settings: Settings, as_user: str, name: str | None = None) -> None:
    """Talk to the pipeline from the terminal, no Telegram. Same database, same code.

    `name` creates the member when `as_user` does not exist yet (handy on a fresh db).
    """
    settings.require("anthropic_api_key")
    db = Database(settings.database_path)
    family = Family(db, settings.admin_user_id)
    person = family.get(as_user)
    if person is None:
        if name is None:
            known = [p.id for p in family.members]
            raise SystemExit(
                f"unknown family member: {as_user} (have: {known});"
                f" add with --name or on the web at /family"
            )
        person = family.add(name, member_id=as_user)
        print(f"created member {person.id} ({person.name})")
    pipeline = build_pipeline(settings, db, family)
    print(f"chatting as {person.name}; db={settings.database_path}; empty line to quit")
    while True:
        try:
            text = await asyncio.to_thread(input, f"{person.id}> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not text.strip():
            break
        outcome = await pipeline.handle(person, text)
        print(f"bot> {outcome.reply}")
        for a in outcome.applied:
            flag = "ok" if a.ok else "SKIPPED"
            print(f"     [{flag}] {a.kind} {a.op} #{a.id} {a.note}")
    db.close()


def pull(
    settings: Settings,
    dest: Path,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Download a consistent snapshot of the deployed database (its `GET /backup.db`).

    Replaces `fly ssh sftp get`, which copies the main file but not the -wal file with
    the latest writes. `transport` is for tests.
    """
    base = (url or settings.web_url or "").rstrip("/")
    if not base:
        raise SystemExit("where is the web view? set WEB_URL in .env or pass --url")
    settings.require("web_secret")
    assert settings.web_secret
    token = sign(settings.web_secret, "backup", "cli", BACKUP_TTL)
    with httpx.Client(timeout=60, transport=transport) as client:
        response = client.get(f"{base}/backup.db", headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    for sidecar in (Path(f"{dest}-wal"), Path(f"{dest}-shm")):
        sidecar.unlink(missing_ok=True)  # leftovers of an sftp copy would be applied to this file
    print(f"{len(response.content)} bytes -> {dest}")
    return dest


_OP_KIND = {
    "journal": "entry",
    "items": "item",
    "memories": "memory",  # rows from before 2026-09-11
    "events": "event",
    "commitments": "commitment",
    "reminders": "reminder",
}


def llm_result_lines(raw: str) -> list[str]:
    """`messages.llm_result` as short lines: model and tokens, each op, each applied result."""
    d = json.loads(raw)
    if "error" in d:
        return [f"error: {d['error']}"]
    lines: list[str] = []
    usage = d.get("usage") or {}
    if usage:
        line = f"{d.get('model')}: {usage.get('input_tokens')} in, {usage.get('output_tokens')} out"
        if cached := usage.get("cache_read_input_tokens"):
            line += f", {cached} from cache"
        lines.append(line)
    output = d.get("output") or {}
    for key, kind in _OP_KIND.items():
        for op in output.get(key, []):
            fields = ", ".join(f"{k}={v!r}" for k, v in op.items() if k != "op" and v is not None)
            lines.append(f"{kind} {op.get('op')}: {fields}")
    for a in d.get("applied", []):
        flag = "ok" if a.get("ok") else "SKIPPED"
        note = a.get("note") or ""
        lines.append(f"[{flag}] {a.get('kind')} {a.get('op')} #{a.get('id')} {note}".rstrip())
    return lines


def backup(settings: Settings, db_path: Path, dest_dir: Path) -> Path:
    """Write `family-YYYY-MM-DD.zip` (a database snapshot plus the notes as `notes.md`) to
    `dest_dir`. Works on any database file, e.g. the one `pull` just fetched."""
    db = Database(db_path)
    try:
        path = write_archive(db, Family(db, settings.admin_user_id), settings.tz, dest_dir)
    finally:
        db.close()
    print(f"{path.stat().st_size} bytes -> {path}")
    return path


def show_log(settings: Settings, db_path: Path, last: int) -> None:
    """Print the last N messages, oldest first, with what the LLM returned and what was applied.

    Works on any database file, e.g. the one `pull` just fetched.
    """
    db = Database(db_path)
    family = Family(db, settings.admin_user_id)
    for m in db.recent_messages(last):
        if m.user_id == "bot":
            who = f"bot -> {family.display_name(m.chat_with)}"
        else:
            who = family.display_name(m.user_id)
        voice = " (voice)" if m.is_voice else ""
        print(f"#{m.id} {fmt_dt(m.created_at, settings.tz)} {who}{voice}: {m.raw_text}")
        if m.llm_result:
            for line in llm_result_lines(m.llm_result):
                print(f"    {line}")
    db.close()
