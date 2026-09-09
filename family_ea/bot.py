"""Telegram side: allowlist, commands, text and voice handlers."""

from __future__ import annotations

import html
import json
import logging

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .db import Database
from .family import Family, Member
from .pipeline import Pipeline
from .transcribe import Transcriber

log = logging.getLogger(__name__)

TG_MAX_LEN = 4000
PRIVATE_BOT = "Це приватний сімейний бот."


def _clip(text: str) -> str:
    return text if len(text) <= TG_MAX_LEN else text[: TG_MAX_LEN - 1] + "…"


def build_bot(
    settings: Settings,
    family: Family,
    db: Database,
    pipeline: Pipeline,
    transcriber: Transcriber | None,
) -> Application:
    assert settings.telegram_token

    def person_of(update: Update) -> Member | None:
        if update.effective_user is None:
            return None
        return family.by_telegram_id(update.effective_user.id)

    async def send_outcome(update: Update, person: Member, text: str, is_voice: bool) -> None:
        assert update.message
        outcome = await pipeline.handle(
            person, text, is_voice=is_voice, tg_message_id=update.message.message_id
        )
        sent = await update.message.reply_text(_clip(outcome.reply))
        db.set_tg_message_id(outcome.bot_message_id, sent.message_id)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = person_of(update)
        assert update.message
        if person is None:
            # Not on the allowlist: show the id so it can be added to FAMILY.
            tg_id = update.effective_user.id if update.effective_user else "?"
            await update.message.reply_text(f"{PRIVATE_BOT} Твій Telegram id: {tg_id}")
            return
        hint = ""
        if settings.web_url:
            hint = f" Факти про сім'ю можна заповнити на web: {settings.web_url}/facts"
        await update.message.reply_text(
            f"Привіт, {person.name}! Пиши або наговорюй що завгодно: що сталося, що треба "
            f"зробити, кого як звати. Питай — відповім з того, що знаю.{hint}"
        )

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = person_of(update)
        assert update.message and update.message.text
        if person is None:
            return
        await update.message.chat.send_action(ChatAction.TYPING)
        await send_outcome(update, person, update.message.text, is_voice=False)

    async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = person_of(update)
        assert update.message and update.message.voice
        if person is None:
            return
        if transcriber is None:
            await update.message.reply_text("Голосові не налаштовані: нема OPENAI_API_KEY.")
            return
        await update.message.chat.send_action(ChatAction.TYPING)
        tg_file = await update.message.voice.get_file()
        audio = bytes(await tg_file.download_as_bytearray())
        try:
            text = await transcriber.transcribe(audio)
        except Exception:
            log.exception("transcription failed")
            await update.message.reply_text(
                "Не зміг розпізнати голосове. Спробуй ще раз або напиши текстом."
            )
            return
        if not text:
            await update.message.reply_text("Нічого не почув у цьому голосовому.")
            return
        await update.message.chat.send_action(ChatAction.TYPING)
        await send_outcome(update, person, text, is_voice=True)

    async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = person_of(update)
        assert update.message
        if person is None:
            return
        msg = db.last_user_message(person.id)
        if msg is None:
            await update.message.reply_text("Ще нема повідомлень.")
            return
        if msg.llm_result:
            pretty = json.dumps(json.loads(msg.llm_result), ensure_ascii=False, indent=2)
        else:
            pretty = "(llm_result порожній)"
        text = f"#{msg.id} {msg.created_at} {msg.user_id}: {msg.raw_text}\n\n{pretty}"
        await update.message.reply_text(
            f"<pre>{html.escape(_clip(text))}</pre>", parse_mode=ParseMode.HTML
        )

    async def facts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = person_of(update)
        assert update.message
        if person is None:
            return
        current = db.current_facts()
        if current and current.text.strip():
            await update.message.reply_text(_clip(current.text))
        else:
            where = f" Заповни на web: {settings.web_url}/facts" if settings.web_url else ""
            await update.message.reply_text(f"Фактів ще нема.{where}")

    async def web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = person_of(update)
        assert update.message
        if person is None:
            return
        await update.message.reply_text(settings.web_url or "WEB_URL не налаштовано.")

    async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            log.info("ignored update from non-family user %s", update.effective_user)
            await update.message.reply_text(PRIVATE_BOT)

    async def post_init(app: Application) -> None:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "привітання"),
                BotCommand("debug", "що LLM повернув на останнє повідомлення"),
                BotCommand("facts", "факти про сім'ю, які бачить асистент"),
                BotCommand("web", "лінк на web view"),
            ]
        )

    app = Application.builder().token(settings.telegram_token).post_init(post_init).build()
    allowed = filters.User(user_id=family.telegram_ids)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug", debug, filters=allowed))
    app.add_handler(CommandHandler("facts", facts, filters=allowed))
    app.add_handler(CommandHandler("web", web, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(allowed & filters.VOICE, on_voice))
    app.add_handler(MessageHandler(~allowed, unknown))
    return app
