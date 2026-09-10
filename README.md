# Family EA

A private executive assistant for one family, living in Telegram.

Two people throw context at the bot during the day: text or voice, in natural
language, without deciding whether it's a task, a note, or a contact. The bot
keeps one shared state, answers questions about it, and every morning writes
each person what's on today and what's still hanging. A small web page shows
what the system actually stored.

The whole idea in one line: **the LLM understands, the code executes.** One
structured-output call per message returns what to remember, which events to
put on the calendar, and which open loops to create, update, or close. Everything else, including the morning
digest, the "what's today" query, storage, and the web view, is plain
deterministic code. The model remembers nothing between calls.

Full spec (in Ukrainian): [docs/spec-v3.md](docs/spec-v3.md).

## Stack

Python 3.12, `python-telegram-bot` (long polling + JobQueue), FastAPI for the
web view, SQLite with FTS5, Anthropic SDK. One process, one SQLite writer.
Deployed on [Fly.io](https://fly.io) with a persistent volume.

## Running locally

```bash
uv sync
cp .env.example .env          # fill in tokens and ADMIN_USER_ID
uv run python -m family_ea            # bot + web on :8080
uv run python -m family_ea chat --as oleh --name Олег   # the pipeline without Telegram
uv run pytest
```

`.env` and `data/` are gitignored. They hold the admin's Telegram id, secrets,
and the database and must never be committed. Who is who in the family is not
config either: the people who talk to the bot are added on the web (`/family`;
the admin appears there by writing to the bot, and a stranger who writes gets
the admin a link to add them), stable background goes into *facts*, a free-text
page you edit on the web, and everything else the bot learns from conversation
as memories. All names in this repo's docs, prompts, and tests are fictional
placeholders.

## Deploying

```bash
fly launch --no-deploy        # first time only
fly volumes create data --size 1 --region fra
fly secrets set ADMIN_USER_ID=... TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... OPENAI_API_KEY=... WEB_USER=... WEB_PASSWORD=...
fly deploy
```

## License

MIT.

## Status

Text and voice in; memories, events and commitments out; Q&A from context; a
morning digest at 08:30 with today's and tomorrow's events and the day's
commitments; `.ics` links on the web and «📅» buttons in Telegram that send an
`.ics` file. Deployed; now in real use. Next: whatever hurts.
