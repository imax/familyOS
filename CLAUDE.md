# Family EA

Private family assistant in Telegram: two adults throw text and voice at the bot, it keeps
one shared state (journal, items, events, commitments, reminders), answers questions from it,
pushes a morning digest and sends reminders at the asked time. Deployed to Fly.io, SQLite on a volume, in real use since 2026-09-10.

`docs/spec-v3.md` is the original spec (Ukrainian). It was retired on 2026-09-10: read it
for the idea, not for what to build next. What to build next comes from real usage; the
list is in `docs/status.md` (see «Status» below).

**Start a session with `git log`, and when the question is behaviour, with a fresh copy of
production:** `pull` then `log` (see Commands).

## Commands

```bash
uv sync                                   # install (creates .venv)
uv run python -m family_ea                # run bot + web in one process
uv run python -m family_ea chat --as oleh --name Олег   # the pipeline as a REPL, no Telegram
uv run python -m family_ea pull           # production db snapshot -> data/prod.db
uv run python -m family_ea backup --db data/prod.db   # zip: db snapshot + notes.md -> data/backups/
uv run python -m family_ea log --db data/prod.db --last 50   # messages + what the LLM did
uv run pytest                             # tests
uv run ruff check . && uv run ruff format .
fly deploy --ha=false                     # deploy; never without --ha=false (see Production)
```

`make` lists the shortcuts: `make backup` (pull + zip), `make log` (pull + log, `LAST=`),
`make test`, `make lint`, `make fmt`, `make deploy` (with `--ha=false` baked in).

`pull` needs `WEB_URL` and `WEB_SECRET` in `.env`; it signs a short-lived bearer token and
downloads `GET /backup.db`, a consistent online backup, WAL included.

## Layout

```
family_ea/
  config.py     env -> Settings (.env loaded in dev)
  auth.py       signed tokens (HMAC under WEB_SECRET): a `link` from the bot becomes a
                `session` cookie; `pull` signs a `backup` bearer. Nothing is stored.
  family.py     Family over the members table (+ ADMIN_USER_ID); slugify() makes ids from names
  db.py         SQLite schema + all queries; dataclasses Message/Entry/Item/Event/
                Commitment/Reminder/TodayList; item_history is written by the item methods only;
                _migrate() for what CREATE IF NOT EXISTS cannot express;
                backup_to() is the online backup behind GET /backup.db
  context.py    deterministic LLM context, event agenda (today/tomorrow/later/recent),
                commitment buckets (today/overdue/open/later), today boards (blocks for the
                web and the digest head), the digest text, the web timeline (overdue / days /
                undated), FTS query
  llm.py        pydantic output schema, system prompt, the one messages.parse() call
  ops.py        apply LLM ops to db, with validation and an `applied` log
  pipeline.py   store -> context -> LLM -> ops -> reply
  transcribe.py OpenAI gpt-4o-transcribe via httpx
  ical.py       an event or dated commitment -> .ics bytes (timed or all-day)
  backup.py     the backup archive: a checked db snapshot + the notes as one Markdown, zipped
  bot.py        python-telegram-bot handlers (/start /help /today /debug /facts /web, text,
                voice), the 08:30 digest job, the per-minute reminder job, «Відкрити» (a
                login link) under the digest, /today and /web
  web.py        FastAPI + Jinja: GET /login?t= (the bot's link; sets the cookie), GET / (the
                boards, the timeline, the last done ones; ?q= searches), GET /journal (Нотатки, by month), GET /items (Речі:
                places, recent; ?place= ?owner= list), GET /items/:id (history), GET/POST
                /facts, GET/POST /family, GET /messages, GET /events/:id.ics,
                GET /commitments/:id.ics, POST /commitments/order (the undated list after
                a drag), GET /backup.db (bearer token)
  main.py       serve() runs bot + uvicorn in one loop; chat() REPL; pull(); backup(); show_log()
tests/          deterministic; the LLM is faked, nothing hits the network
```

## Principles

- **LLM understands, code executes.** One structured-output call per incoming message
  returns `reply` plus memory/event/commitment ops. Everything else is deterministic code.
- **Events and commitments are separate tables, not a `kind` column.** An event happens at a
  time or on a day and then passes (never overdue, only cancelled); a commitment is done or
  dropped and can be overdue. Different lifecycles, different data. The same goes for any
  new kind of thing (reminders): its own table, its own ops.
- **Notes and items stay out of the default context.** Both can be long and many; the LLM
  sees only what changed in the last two days plus the search hits for the incoming
  message, the rest is on the web. Which item a message is about, the LLM decides from
  those candidates (an update with an id, or a question in the reply); there is no
  matching code, and an ambiguous message must change nothing.
- **The «на сьогодні» board is free text per member, replaced whole.** One `today` op: the
  LLM returns the new text of one member's board, and only when the person addresses the
  board explicitly («на сьогодні: …», «додай у сьогодні …»); everything else stays a
  commitment or an event. Code never parses the board: it is shown as kept (the web, the
  digest head, the viewer's own first) and versioned like facts. Nothing resets it;
  staleness is shown («оновлено вчора»), not acted on.
- **Original messages are never mutated.** `messages.raw_text` is append-only.
- `messages.chat_with` is the family member whose chat the row belongs to, so bot replies
  and pushes can be attributed in context; `messages.llm_result` holds
  `{model, usage, request_id, output, applied}`, not the bare LLM output.
- **Commitments close only through the LLM `close` op.** Web done/drop buttons were built
  and removed (2026-09-10, ugly); if closing on the web comes back, it must reuse
  `db.close_commitment`, no parallel logic. The one thing the web writes about a
  commitment is the order of the undated ones (`position`, dragged on the home page,
  `db.reorder_commitments`); the LLM never sets it, and `open_commitments()` returns that
  order so the timeline, the digest and the LLM context agree. Unplaced ones (new since the
  last drag) come first, newest first.
- **Every push is deterministic and stored.** The morning digest renders each member's
  board (own first), today's and tomorrow's events, then commitments due today and overdue,
  never the undated ones (they are on the web), no LLM call, and is silent when empty;
  `/today` is the same digest now. A reminder is text the LLM wrote at request time,
  sent by a per-minute job when `at` comes, to the one member it is for or to everyone.
  Both are stored as bot messages in each recipient's chat so replies to them have
  context. Anything else the bot sends on its own must follow the same two rules.
- **Web identity comes from the bot.** No passwords: `/web` (and «Відкрити» under the digest
  and `/today`) sends a member a signed link, opening it sets a year-long signed cookie.
  Whoever is in `members` can log in; nothing is stored, so removing a member or
  rotating `WEB_SECRET` is the only revocation.
- The bot must not promise what the code cannot do. The prompt lists what the bot does
  (digest, reminders, the web link); when a capability is added or removed, that list changes in
  the same commit.
- Keep it small. Two people use this; a feature earns its place by removing a real pain.

## Production

- Fly app `family-ea`, https://family-ea.fly.dev, one `shared-cpu-1x` machine in `fra`,
  volume `data` at `/data`, database `/data/family.db`. Fly keeps daily volume snapshots
  for 5 days.
- **Always `fly deploy --ha=false`.** Two machines would mean two pollers on one bot token
  and two SQLite files.
- Secrets on Fly: `ADMIN_USER_ID`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `WEB_SECRET`, `WEB_URL`. The rest is in `fly.toml`.
- A deploy ships code only. Tables are created at start (`CREATE TABLE IF NOT EXISTS`);
  anything else goes into `Database._migrate()`, idempotent steps that run at every start
  (the first one moved `memories` into `journal`, 2026-09-11).
- Local `data/family.db` and production are separate databases; nothing syncs. To look at
  production: `pull`, then `log --db data/prod.db` or `sqlite3 data/prod.db`. Fix production
  data through the bot itself where possible (tell it what changed), not with SQL.
  `fly ssh sftp get` copies the main file but not the `-wal` file with the latest writes;
  that is why `/backup.db` exists.
- Never run a local `serve` with the production bot token while Fly is up: Telegram answers
  409 on getUpdates. Locally use `chat`, or a separate dev bot token.

## Status

`docs/status.md` (Ukrainian) is the living description of the system: «Що вміє» (what it
does, by surface), «Ідеї та туду» (the backlog, found in real use) and «Зміни» (dated
releases, newest first). A commit that changes behaviour updates it in the same commit:
the capability text, the backlog item it closes, a line under «Зміни». «Що вміє» is
written for the people who use the bot: a page or a command name at most, no internals.
The backlog may name code.

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
  reads it), journal/items/events/commitments/reminders (everything people tell the bot; the LLM writes
  them).
- Python 3.12, `uv` for deps, `ruff` for lint/format, `pytest` with `asyncio_mode=auto`.
- FastAPI modules must not use `from __future__ import annotations`: postponed `Annotated`
  dependencies referencing closure variables break dependency resolution (silent 422s).
- SQLite `LIKE`/`lower()` are ASCII-only; use the registered `ufold()` for Ukrainian text and
  `regexp()` (Python `re`) for word-prefix search. Search terms come from `context.stems()`,
  one heuristic behind both the FTS query and the events/commitments pattern.
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
- Op fields in `llm.py` are never `X | None`: nullable fields inflate the structured-output
  grammar and the API rejects the schema («compiled grammar is too large»; hit 2026-09-11
  when items were added). `''` / `0` mean «not given». Test a schema change with `chat`
  before deploying: the failure is a 400 on every message.
