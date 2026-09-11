# Family EA

A private executive assistant for one family, living in Telegram.

Two people throw context at the bot during the day: text or voice, in natural
language, without deciding whether it's a task, a note, or a contact. The bot
keeps one shared state, answers questions about it, and every morning writes
each person what's on today and what's still hanging. A small web page shows
what the system actually stored.

The whole idea in one line: **the LLM understands, the code executes.** One
structured-output call per message returns what to remember, which events to
put on the calendar, which open loops to create, update, or close, and what to
remind whom when. Everything else, including the morning digest, the reminder
job, the "what's today" query, storage, and the web view, is plain
deterministic code. The model remembers nothing between calls.

## Status

Deployed; now in real use. What it does today, the backlog and the dated
changes: [docs/status.md](docs/status.md) (Ukrainian).

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
uv run python -m family_ea pull                          # snapshot of the deployed db -> data/prod.db
uv run python -m family_ea log --db data/prod.db         # messages with what the LLM did
```

`.env` and `data/` are gitignored. They hold the admin's Telegram id, secrets,
and the database and must never be committed. Who is who in the family is not
config either: the people who talk to the bot are added on the web (`/family`;
the admin appears there by writing to the bot, and a stranger who writes gets
the admin a link to add them), stable background goes into *facts*, a free-text
page you edit on the web, and everything else the bot learns from conversation
as journal entries (Нотатки). All names in this repo's docs, prompts, and tests are fictional
placeholders.

## Deploying

```bash
fly launch --no-deploy        # first time only
fly volumes create data --size 1 --region fra
fly secrets set ADMIN_USER_ID=... TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... OPENAI_API_KEY=... WEB_URL=https://<app>.fly.dev WEB_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
fly deploy --ha=false         # one machine: one poller on the bot token, one SQLite
```

The web view has no password: `/web` in Telegram (and the «Відкрити» button under the
morning digest) sends a signed link that logs that browser in for a year. `GET /backup.db`
is a consistent online backup of the database; `python -m family_ea pull` signs a
short-lived bearer token with the same `WEB_SECRET` (put it in `.env`) and downloads it.

## License

MIT.
