"""Process entry points: `serve` (bot + web in one event loop) and `chat` (local REPL)."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .bot import build_bot
from .config import Settings
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
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_pipeline(settings: Settings, db: Database, family: Family) -> Pipeline:
    llm = Llm(settings.llm_model, settings.llm_effort, api_key=settings.anthropic_api_key)
    return Pipeline(db, family, llm, settings.tz)


async def serve(settings: Settings) -> None:
    settings.require("family", "telegram_token", "anthropic_api_key", "web_user", "web_password")
    family = Family.from_env(settings.family or "")
    db = Database(settings.database_path)
    pipeline = build_pipeline(settings, db, family)
    transcriber = Transcriber(settings.openai_api_key) if settings.openai_api_key else None

    tg_app = build_bot(settings, family, db, pipeline, transcriber)
    web_app = build_web(settings, family, db)
    server = uvicorn.Server(
        uvicorn.Config(web_app, host="0.0.0.0", port=settings.port, log_level="info")
    )

    log.info(
        "starting: model=%s db=%s family=%s voice=%s",
        settings.llm_model,
        settings.database_path,
        [p.id for p in family.members],
        "on" if transcriber else "off",
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


async def chat(settings: Settings, as_user: str) -> None:
    """Talk to the pipeline from the terminal, no Telegram. Same database, same code."""
    settings.require("family", "anthropic_api_key")
    family = Family.from_env(settings.family or "")
    person = family.get(as_user)
    if person is None:
        known = [p.id for p in family.members]
        raise SystemExit(f"unknown family member: {as_user} (have: {known})")
    db = Database(settings.database_path)
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
