# Family EA

A private executive assistant for one family, living in Telegram.

Two people throw context at the bot during the day: text or voice, in natural
language, without deciding whether it's a task, a note, or a contact. The bot
keeps one shared state, answers questions about it, and every morning writes
each person what's on today and what's still hanging. A small web page shows
what the system actually stored.

The whole idea in one line: **the LLM understands, the code executes.** One
structured-output call per message returns what to remember and which open
loops to create, update, or close. Everything else, including the morning
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
cp .env.example .env          # fill in tokens and FAMILY
uv run python -m family_ea            # bot + web on :8080
uv run python -m family_ea chat --as oleh   # talk to the pipeline without Telegram
uv run pytest
```

`.env` and `data/` are gitignored. They hold Telegram ids, secrets, and the
database and must never be committed. Who is who in the family is not config:
the bot learns it from conversation and keeps it in memories. All names in
this repo's docs, prompts, and tests are fictional placeholders.

## Deploying

```bash
fly launch --no-deploy        # first time only
fly volumes create data --size 1 --region waw
fly secrets set FAMILY=... TELEGRAM_TOKEN=... ANTHROPIC_API_KEY=... OPENAI_API_KEY=... WEB_USER=... WEB_PASSWORD=...
fly deploy
```

## License

MIT.

## Status

Slice 1 of 2 (spec section 12): text and voice in, memories and commitments out,
Q&A from context, read-only web view. Next: the morning digest and done/drop
buttons on the web.
