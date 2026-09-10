"""Telegram side: allowlist, commands, text and voice handlers, the digest and reminder jobs."""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .auth import LINK_TTL, sign
from .config import Settings
from .context import bucket_commitments, build_agenda, digest_text, parse_iso
from .db import Commitment, Database, Event, Member, Reminder
from .family import Family
from .ical import commitment_ics, event_ics, ics_filename
from .pipeline import Pipeline
from .transcribe import Transcriber

log = logging.getLogger(__name__)

TG_MAX_LEN = 4000
PRIVATE_BOT = "Це приватний сімейний бот."
OPEN_ITEMS_WEEKDAY = 0  # Monday: the one morning the digest also lists undated items
REMINDER_INTERVAL = 60  # seconds between checks for due reminders
REMINDER_MAX_LATE = timedelta(hours=3)  # due longer ago than this (downtime): missed, not sent
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # login links in text: no preview fetch
COMMANDS = [
    BotCommand("today", "що сьогодні, що прострочено, що висить"),
    BotCommand("web", "відкрити веб-сторінку сім'ї"),
    BotCommand("facts", "факти про сім'ю, які бачить асистент"),
    BotCommand("help", "що вміє бот і його команди"),
    BotCommand("debug", "що модель повернула на останнє повідомлення"),
]


def _clip(text: str) -> str:
    return text if len(text) <= TG_MAX_LEN else text[: TG_MAX_LEN - 1] + "…"


def help_text(settings: Settings) -> str:
    """What the bot does and its commands; /start and /help say this."""
    when = settings.digest_time.strftime("%H:%M")
    commands = "\n".join(f"/{c.command} — {c.description}" for c in COMMANDS)
    return (
        "Пиши або наговорюй що завгодно: що сталося, що треба зробити, кого як звати. "
        f"Питай — відповім з того, що знаю. Щоранку о {when} надсилаю дайджест, а нагадую, "
        f"коли попросиш.\n\nКоманди:\n{commands}"
    )


def login_link(settings: Settings, member: Member, path: str = "/") -> str | None:
    """A link that logs `member` into the web view and opens `path`; None when the web is
    not configured. Valid for LINK_TTL, then the person asks for a new one with /web."""
    if not settings.web_url or not settings.web_secret:
        return None
    query = {"t": sign(settings.web_secret, "link", member.id, LINK_TTL)}
    if path != "/":
        query["next"] = path
    return f"{settings.web_url.rstrip('/')}/login?{urlencode(query)}"


def ics_rows(items: list[Event | Commitment]) -> list[list[InlineKeyboardButton]]:
    """One «📅 …» button per event or dated commitment; tapping it sends an .ics file."""
    rows = []
    for item in items:
        if isinstance(item, Event):
            data = f"ics:e:{item.id}"
        elif item.has_due:
            data = f"ics:c:{item.id}"
        else:
            continue
        rows.append([InlineKeyboardButton(f"📅 {item.text[:40]}", callback_data=data)])
    return rows


def ics_keyboard(items: list[Event | Commitment]) -> InlineKeyboardMarkup | None:
    rows = ics_rows(items)
    return InlineKeyboardMarkup(rows) if rows else None


def digest_keyboard(
    items: list[Event | Commitment], web: str | None
) -> InlineKeyboardMarkup | None:
    """«Відкрити» the web view on top, then «📅» per dated item."""
    rows = [[InlineKeyboardButton("Відкрити", url=web)]] if web else []
    rows += ics_rows(items)
    return InlineKeyboardMarkup(rows) if rows else None


def reminder_recipients(r: Reminder, family: Family) -> list[Member]:
    """The one member a reminder is for, or everyone with a Telegram id when `who` is null."""
    members = [m for m in family.members if m.telegram_id is not None]
    return [m for m in members if r.who is None or m.id == r.who]


async def deliver_due_reminders(
    db: Database,
    family: Family,
    now: datetime,
    send: Callable[[Member, str], Awaitable[int]],
) -> list[Reminder]:
    """Send every pending reminder whose time has come; returns those marked sent.

    `send` delivers a text to a member and returns the Telegram message id. Each delivery
    is stored as a bot message so a reply to it («перенеси на 17») has context. A reminder
    counts as sent once at least one recipient got it; if nobody could be reached it stays
    pending for the next tick, until it is REMINDER_MAX_LATE old and becomes `missed`.
    """
    now_iso = now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    sent: list[Reminder] = []
    for r in db.due_reminders(now_iso):
        recipients = reminder_recipients(r, family)
        if not recipients or parse_iso(r.at) < now - REMINDER_MAX_LATE:
            db.finish_reminder(r.id, "missed")
            why = "too late" if recipients else "no recipient"
            log.warning("reminder %s missed: %s", r.id, why)
            continue
        text = f"⏰ {r.text}"
        delivered = False
        for member in recipients:
            try:
                tg_message_id = await send(member, text)
            except Exception:
                log.warning("reminder %s: could not message %s", r.id, member.id, exc_info=True)
                continue
            mid = db.insert_message("bot", member.id, text)
            db.set_tg_message_id(mid, tg_message_id)
            delivered = True
        if delivered:
            db.finish_reminder(r.id, "sent")
            sent.append(r)
    return sent


def family_filter(family: Family) -> filters.BaseFilter:
    """Messages from family members, or from the admin before their row exists.

    Membership lives in the database and changes at runtime, so this is a live check
    rather than a static `filters.User` list.
    """

    class _Family(filters.MessageFilter):
        def filter(self, message: Message) -> bool:
            user = message.from_user
            if user is None:
                return False
            return family.is_admin(user.id) or family.by_telegram_id(user.id) is not None

    return _Family(name="family")


def build_bot(
    settings: Settings,
    family: Family,
    db: Database,
    pipeline: Pipeline,
    transcriber: Transcriber | None,
) -> Application:
    assert settings.telegram_token
    notified_strangers: set[int] = set()

    def member_of(update: Update) -> Member | None:
        user = update.effective_user
        if user is None:
            return None
        member = family.by_telegram_id(user.id)
        if member is None and family.is_admin(user.id):
            # First contact from the admin: their row comes from the Telegram profile.
            member = family.add(user.first_name or user.username or "admin", user.id)
            log.info("admin joined as member %s", member.id)
        return member

    async def send_outcome(update: Update, person: Member, text: str, is_voice: bool) -> None:
        assert update.message
        outcome = await pipeline.handle(
            person, text, is_voice=is_voice, tg_message_id=update.message.message_id
        )
        touched: list[Event | Commitment] = []
        for a in outcome.applied:
            if not (a.ok and a.id and a.op in ("create", "update")):
                continue
            item: Event | Commitment | None = None
            if a.kind == "event":
                item = db.get_event(a.id)
            elif a.kind == "commitment":
                item = db.get_commitment(a.id)
            if item is not None:
                touched.append(item)
        sent = await update.message.reply_text(
            _clip(outcome.reply), reply_markup=ics_keyboard(touched)
        )
        db.set_tg_message_id(outcome.bot_message_id, sent.message_id)

    def digest(now: datetime, *, include_open: bool) -> tuple[str, list[Event | Commitment]] | None:
        """The digest text and the items it lists; the keyboard is built per recipient."""
        agenda = build_agenda(db.planned_events(), now)
        buckets = bucket_commitments(db.open_commitments(), now)
        text = digest_text(agenda, buckets, family, settings.tz, include_open=include_open)
        if text is None:
            return None
        items: list[Event | Commitment] = [*agenda.today, *agenda.tomorrow]
        items += [*buckets.today, *buckets.overdue]
        return _clip(text), items

    async def send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
        """The morning job: one shared digest to every member; silence when it is empty."""
        now = datetime.now(settings.tz)
        result = digest(now, include_open=now.weekday() == OPEN_ITEMS_WEEKDAY)
        if result is None:
            log.info("digest: nothing to say today")
            return
        text, items = result
        for member in family.members:
            if member.telegram_id is None:
                continue
            keyboard = digest_keyboard(items, login_link(settings, member))
            try:
                sent = await context.bot.send_message(
                    member.telegram_id, text, reply_markup=keyboard
                )
            except Exception:
                log.warning("digest: could not message %s", member.id, exc_info=True)
                continue
            # Stored like any bot reply, so the LLM sees what the push said when they answer.
            mid = db.insert_message("bot", member.id, text)
            db.set_tg_message_id(mid, sent.message_id)

    async def send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Every minute: whatever reminders are due, to whoever they are for."""

        async def send(member: Member, text: str) -> int:
            assert member.telegram_id is not None
            sent = await context.bot.send_message(member.telegram_id, text)
            return sent.message_id

        await deliver_due_reminders(db, family, datetime.now(UTC), send)

    async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = member_of(update)
        assert update.message and person
        result = digest(datetime.now(settings.tz), include_open=True)
        if result is None:
            await update.message.reply_text("Нічого не висить.")
            return
        text, items = result
        keyboard = digest_keyboard(items, login_link(settings, person))
        sent = await update.message.reply_text(text, reply_markup=keyboard)
        mid = db.insert_message("bot", person.id, text)
        db.set_tg_message_id(mid, sent.message_id)

    async def on_ics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """«📅» button: send the event or commitment as an .ics file for the phone calendar."""
        q = update.callback_query
        assert q and q.data
        if family.by_telegram_id(q.from_user.id) is None:
            await q.answer(PRIVATE_BOT)
            return
        _, kind, raw_id = q.data.split(":")
        payload: tuple[bytes, str] | None = None
        if kind == "e":
            e = db.get_event(int(raw_id))
            if e is not None:
                payload = (event_ics(e), e.text)
        else:
            c = db.get_commitment(int(raw_id))
            if c is not None and c.has_due:
                payload = (commitment_ics(c), c.text)
        if payload is None:
            await q.answer("Нема такого запису з датою.")
            return
        await q.answer()
        data, text = payload
        await context.bot.send_document(
            q.from_user.id, InputFile(data, filename=ics_filename(text)), caption=text
        )

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = member_of(update)
        assert update.message and person
        hints = []
        if facts_link := login_link(settings, person, "/facts"):
            hints.append(f"Факти про сім'ю можна заповнити на вебі: {facts_link}")
            if family.is_admin(person.telegram_id or 0) and len(family.members) == 1:
                hints.append(f"Додати інших до сім'ї: {login_link(settings, person, '/family')}")
        hint = "".join(f"\n\n{h}" for h in hints)
        await update.message.reply_text(
            f"Привіт, {person.name}! {help_text(settings)}{hint}", link_preview_options=NO_PREVIEW
        )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        await update.message.reply_text(help_text(settings))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = member_of(update)
        assert update.message and update.message.text and person
        await update.message.chat.send_action(ChatAction.TYPING)
        await send_outcome(update, person, update.message.text, is_voice=False)

    async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        person = member_of(update)
        assert update.message and update.message.voice and person
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
        person = member_of(update)
        assert update.message and person
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
        assert update.message
        current = db.current_facts()
        if current and current.text.strip():
            await update.message.reply_text(_clip(current.text))
        else:
            where = f" Заповни на web: {settings.web_url}/facts" if settings.web_url else ""
            await update.message.reply_text(f"Фактів ще нема.{where}")

    async def web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A login link for this member; the browser then remembers them for a year."""
        person = member_of(update)
        assert update.message and person
        link = login_link(settings, person)
        if link is None:
            await update.message.reply_text("Веб не налаштовано: потрібні WEB_URL і WEB_SECRET.")
            return
        await update.message.reply_text(
            "Веб-сторінка сім'ї: усе, що я записав, по днях. Посилання діє добу; після "
            "входу браузер пам'ятає тебе.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Відкрити", url=link)]]),
        )

    async def stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Not in the family: say so, and tell the admin once who knocked."""
        user = update.effective_user
        if update.message is None or user is None:
            return
        log.info("ignored message from non-family user %s", user.id)
        await update.message.reply_text(f"{PRIVATE_BOT} Твій Telegram id: {user.id}")
        if user.id in notified_strangers or family.admin_telegram_id is None:
            return
        notified_strangers.add(user.id)
        who = user.full_name + (f" (@{user.username})" if user.username else "")
        text = f"Боту пише {who}, Telegram id {user.id}."
        admin = family.by_telegram_id(family.admin_telegram_id)
        query = urlencode({"name": user.first_name or "", "telegram_id": user.id})
        if admin and (link := login_link(settings, admin, f"/family?{query}")):
            text += f" Додати до сім'ї: {link}"
        try:
            await context.bot.send_message(
                chat_id=family.admin_telegram_id, text=text, link_preview_options=NO_PREVIEW
            )
        except Exception:
            log.warning("could not notify the admin about user %s", user.id, exc_info=True)

    async def post_init(app: Application) -> None:
        await app.bot.set_my_commands(COMMANDS)

    app = Application.builder().token(settings.telegram_token).post_init(post_init).build()
    allowed = family_filter(family)
    app.add_handler(CommandHandler("start", start, filters=allowed))
    app.add_handler(CommandHandler("help", help_cmd, filters=allowed))
    app.add_handler(CommandHandler("today", today, filters=allowed))
    app.add_handler(CommandHandler("debug", debug, filters=allowed))
    app.add_handler(CommandHandler("facts", facts, filters=allowed))
    app.add_handler(CommandHandler("web", web, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(allowed & filters.VOICE, on_voice))
    app.add_handler(MessageHandler(~allowed, stranger))
    app.add_handler(CallbackQueryHandler(on_ics, pattern=r"^ics:[ec]:\d+$"))
    assert app.job_queue
    app.job_queue.run_daily(
        send_digest, time=settings.digest_time.replace(tzinfo=settings.tz), name="digest"
    )
    app.job_queue.run_repeating(
        send_reminders, interval=REMINDER_INTERVAL, first=5, name="reminders"
    )
    return app
