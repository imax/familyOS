# Family EA

Private family assistant in Telegram: two adults throw text and voice at the bot, it keeps
one shared state (memories, events, commitments, reminders), answers questions from it,
pushes a morning digest and sends reminders at the asked time. Deployed to Fly.io, SQLite on a volume, in real use since 2026-09-10.

`docs/spec-v3.md` is the original spec (Ukrainian). It was retired on 2026-09-10: read it
for the idea, not for what to build next. What to build next comes from real usage; the
current list is in «Now» below. Delete items there when they ship.

**Start a session with `git log`, and when the question is behaviour, with a fresh copy of
production:** `pull` then `log` (see Commands).

## Commands

```bash
uv sync                                   # install (creates .venv)
uv run python -m family_ea                # run bot + web in one process
uv run python -m family_ea chat --as oleh --name Олег   # the pipeline as a REPL, no Telegram
uv run python -m family_ea pull           # production db snapshot -> data/prod.db
uv run python -m family_ea log --db data/prod.db --last 50   # messages + what the LLM did
uv run pytest                             # tests
uv run ruff check . && uv run ruff format .
fly deploy --ha=false                     # deploy; never without --ha=false (see Production)
```

`pull` needs `WEB_URL`, `WEB_USER` and `WEB_PASSWORD` in `.env`; it downloads
`GET /backup.db`, a consistent online backup, WAL included.

## Layout

```
family_ea/
  config.py     env -> Settings (.env loaded in dev)
  family.py     Family over the members table (+ ADMIN_USER_ID); slugify() makes ids from names
  db.py         SQLite schema + all queries; dataclasses Message/Memory/Event/Commitment/
                Reminder; backup_to() is the online backup behind GET /backup.db
  context.py    deterministic LLM context, event agenda (today/tomorrow/later/recent),
                commitment buckets (today/overdue/open/later), the digest text, the web
                timeline (overdue / days / undated), FTS query
  llm.py        pydantic output schema, system prompt, the one messages.parse() call
  ops.py        apply LLM ops to db, with validation and an `applied` log
  pipeline.py   store -> context -> LLM -> ops -> reply
  transcribe.py OpenAI gpt-4o-transcribe via httpx
  ical.py       an event or dated commitment -> .ics bytes (timed or all-day)
  bot.py        python-telegram-bot handlers (/start /today /debug /facts /web, text, voice),
                the 08:30 digest job, the per-minute reminder job, «📅» buttons that send an .ics
  web.py        FastAPI + Jinja: GET / (the timeline; ?q= searches), GET/POST /facts,
                GET/POST /family, GET /memories, GET /messages, GET /events/:id.ics,
                GET /commitments/:id.ics, GET /backup.db
  main.py       serve() runs bot + uvicorn in one loop; chat() REPL; pull(); show_log()
tests/          deterministic; the LLM is faked, nothing hits the network
```

## Principles

- **LLM understands, code executes.** One structured-output call per incoming message
  returns `reply` plus memory/event/commitment ops. Everything else is deterministic code.
- **Events and commitments are separate tables, not a `kind` column.** An event happens at a
  time or on a day and then passes (never overdue, only cancelled); a commitment is done or
  dropped and can be overdue. Different lifecycles, different data. The same goes for any
  new kind of thing (reminders): its own table, its own ops.
- **Original messages are never mutated.** `messages.raw_text` is append-only.
- `messages.chat_with` is the family member whose chat the row belongs to, so bot replies
  and pushes can be attributed in context; `messages.llm_result` holds
  `{model, usage, request_id, output, applied}`, not the bare LLM output.
- **Commitments close only through the LLM `close` op.** Web done/drop buttons were built
  and removed (2026-09-10, ugly); if closing on the web comes back, it must reuse
  `db.close_commitment`, no parallel logic.
- **Every push is deterministic and stored.** The morning digest renders today's and
  tomorrow's events, then commitments due today and overdue (undated ones only on Mondays),
  no LLM call, and is silent when empty. A reminder is text the LLM wrote at request time,
  sent by a per-minute job when `at` comes, to the one member it is for or to everyone.
  Both are stored as bot messages in each recipient's chat so replies to them have
  context. Anything else the bot sends on its own must follow the same two rules.
- The bot must not promise what the code cannot do. The prompt lists what the bot does
  (digest, reminders, «📅»); when a capability is added or removed, that list changes in
  the same commit.
- Keep it small. Two people use this; a feature earns its place by removing a real pain.

## Production

- Fly app `family-ea`, https://family-ea.fly.dev, one `shared-cpu-1x` machine in `fra`,
  volume `data` at `/data`, database `/data/family.db`. Fly keeps daily volume snapshots
  for 5 days.
- **Always `fly deploy --ha=false`.** Two machines would mean two pollers on one bot token
  and two SQLite files.
- Secrets on Fly: `ADMIN_USER_ID`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `WEB_USER`, `WEB_PASSWORD`, `WEB_URL`. The rest is in `fly.toml`.
- A deploy ships code only. Tables are created at start (`CREATE TABLE IF NOT EXISTS`);
  there are no migrations yet, so a new column means a new table or a hand-run `ALTER`.
- Local `data/family.db` and production are separate databases; nothing syncs. To look at
  production: `pull`, then `log --db data/prod.db` or `sqlite3 data/prod.db`. Fix production
  data through the bot itself where possible (tell it what changed), not with SQL.
  `fly ssh sftp get` copies the main file but not the `-wal` file with the latest writes;
  that is why `/backup.db` exists.
- Never run a local `serve` with the production bot token while Fly is up: Telegram answers
  409 on getUpdates. Locally use `chat`, or a separate dev bot token.

## Now

Found on the first day of real use (2026-09-10). Delete when done.

- **Verify reminders live** (built 2026-09-10 evening, not yet seen in production): ask
  for one an hour before an event, check `log` shows a `reminder create` with the right
  `at` and `who`, and that «⏰ …» arrives to the right people; move the event and check the
  reminder moved with it. Reminders that fall due while the bot is down are sent late,
  up to `REMINDER_MAX_LATE`; a deploy takes a minute, so this rarely matters.
- **Prompt caching** is on for the system prompt only (1-hour TTL). Facts and the family
  list would cache too if they moved before the clock in the context and the call sent
  them as a separate cached block; not worth it until a call costs more than a cent.
- **Small:** today's commitments in the digest repeat today's date; member names come from
  Telegram profiles and may be Latin, renaming is on `/family`.
- **Web home is a timeline** (2026-09-10 evening, Things-style: overdue, then days with
  events, dated commitments and reminders together, today always, then «Без дати»). The
  digest and `/today` still render the older hierarchy (events, then commitments); if the
  timeline feels right after a few days, render them from `build_timeline` too: one agenda
  model, a text and an HTML rendering.
- **Memories are unused so far** (0 rows in production after a day): everything people
  say is a task, an event or a reminder. Watch whether they are needed at all.
- **Web login:** basic auth is tiring to type on a phone every time. Replace it with a
  magic link or similar; the natural source of identity is the bot itself (`/web` could
  send a signed one-time link that sets a long-lived cookie). Not designed yet.
- Scenario tests through the real model (inputs → expected ops) are still the way to change
  the prompt or compare `claude-haiku-4-5` without regressions; not started.

## Conventions

- Code, comments, commit messages: English. Bot replies, prompts, UI copy: Ukrainian.
- Timezone `Europe/Kyiv` for everything user-facing; store ISO UTC in SQLite.
- Secrets and personal data (`.env`, `data/`) are gitignored. This repo is public. Never
  commit tokens, Telegram ids, real names, or real conversation data, including in docs,
  prompts, tests, fixtures and this file. Every name in the repo is a fictional placeholder
  (Олег, Анна, Оля, пані Марія, газовик Петро); keep it that way. Grep for real names
  before every push.
- Three kinds of knowledge, three owners: `members` table (who talks to the bot, the
  Telegram allowlist; only `ADMIN_USER_ID` is env, the admin edits the rest on the web),
  `facts` (stable background about the family; the human edits it on the web, the LLM only
  reads it), memories/events/commitments (everything people tell the bot; the LLM writes
  them).
- Python 3.12, `uv` for deps, `ruff` for lint/format, `pytest` with `asyncio_mode=auto`.
- FastAPI modules must not use `from __future__ import annotations`: postponed `Annotated`
  dependencies referencing closure variables break dependency resolution (silent 422s).
- SQLite `LIKE`/`lower()` are ASCII-only; use the registered `ufold()` for Ukrainian text.
- Tests that check dates in the context monkeypatch `family_ea.db.utc_now_iso`; otherwise
  they drift with the calendar.

## Voice

Transcription goes through OpenAI `gpt-4o-transcribe` (`language=uk`) via a single httpx
multipart POST in `family_ea/transcribe.py`. That is the only OpenAI usage; the LLM is Claude.

## Claude API notes

- `anthropic` SDK 1.x. Structured output via `output_config.format` / `client.messages.parse()`,
  not tool_use tricks and not assistant prefill (prefill is rejected on current models).
- Model comes from `LLM_MODEL` env; default `claude-sonnet-5` at effort `medium`. Compare
  with `claude-haiku-4-5` once scenario tests exist.
- No thinking/`budget_tokens` config needed; use `thinking: {type: "adaptive"}` if enabling.
