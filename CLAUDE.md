# Family EA

Private family assistant in Telegram. Spec: `docs/spec-v3.md` (Ukrainian; treat as
guidelines, not law; it will drift as we build). Deployed to Fly.io, SQLite on a volume.

**Start every session by reading `docs/progress.md`** (state, next steps, open decisions)
and update it before the session ends.

## Commands

```bash
uv sync                          # install (creates .venv)
uv run python -m family_ea       # run bot + web in one process
uv run pytest                    # tests
uv run ruff check . && uv run ruff format .
```

## Layout

```
family_ea/
  config.py     env -> Settings (.env loaded in dev)
  family.py     Family over the members table (+ ADMIN_USER_ID); slugify() makes ids from names
  db.py         SQLite schema + all queries; dataclasses Message/Memory/Event/Commitment
  context.py    deterministic LLM context, event agenda (today/tomorrow/later/recent),
                commitment buckets (today/overdue/open/later), the digest text, FTS query
  llm.py        pydantic output schema, system prompt, the one messages.parse() call
  ops.py        apply LLM ops to db, with validation and an `applied` log
  pipeline.py   store -> context -> LLM -> ops -> reply
  transcribe.py OpenAI gpt-4o-transcribe via httpx
  ical.py       an event or dated commitment -> .ics bytes (timed or all-day)
  bot.py        python-telegram-bot handlers (/start /today /debug /facts /web, text, voice),
                the 08:30 digest job, «📅» buttons that send an .ics
  web.py        FastAPI + Jinja: GET / (?q=), GET/POST /facts, GET/POST /family, GET /messages,
                GET /events/:id.ics, GET /commitments/:id.ics
  main.py       serve() runs bot + uvicorn in one loop; chat() is a local REPL
tests/          deterministic; the LLM is faked, nothing hits the network
```

## Principles

- **LLM understands, code executes.** One structured-output call per incoming message
  returns `reply` plus memory/event/commitment ops. Everything else is deterministic code.
- **Events and commitments are separate tables, not a `kind` column.** An event happens at a
  time or on a day and then passes (never overdue, only cancelled); a commitment is done or
  dropped and can be overdue. Different lifecycles, different data.
- **Original messages are never mutated.** `messages.raw_text` is append-only.
- **Schema deviation from spec:** `messages.chat_with` (family member whose chat the row
  belongs to) so bot replies can be attributed in context; and `messages.llm_result` holds
  `{model, usage, output, applied}` rather than the bare LLM output.
- **Commitments close only through the LLM `close` op.** Web done/drop buttons were built
  and removed (2026-09-10, ugly); if closing on the web comes back, it must reuse
  `db.close_commitment`, no parallel logic.
- **The morning digest is deterministic.** Code renders today's and tomorrow's events, then
  commitments due today and overdue (undated ones only on Mondays), no LLM call; the push is
  stored as a bot message so replies to it have context.
- Keep it small. If a feature isn't testing the spec's hypothesis, it's not in MVP
  (spec section 10 lists what we deliberately don't build).

## Conventions

- Code, comments, commit messages: English. Bot replies, prompts, UI copy: Ukrainian.
- Timezone `Europe/Kyiv` for everything user-facing; store ISO UTC in SQLite.
- Secrets and personal data (`.env`, `data/`) are gitignored. This repo is public. Never
  commit tokens, Telegram ids, real names, or real conversation data, including in docs,
  prompts, tests, and fixtures. Every name in the repo is a fictional placeholder
  (Олег, Анна, Оля, пані Марія, газовик Петро); keep it that way.
- Three kinds of knowledge, three owners: `members` table (who talks to the bot, the
  Telegram allowlist; only `ADMIN_USER_ID` is env, the admin edits the rest on the web),
  `facts` (stable background about the family; the human edits it on the web, the LLM only
  reads it), memories/commitments (everything people tell the bot; the LLM writes them).
- Python 3.12, `uv` for deps, `ruff` for lint/format, `pytest` with `asyncio_mode=auto`.
- FastAPI modules must not use `from __future__ import annotations`: postponed `Annotated`
  dependencies referencing closure variables break dependency resolution (silent 422s).
- SQLite `LIKE`/`lower()` are ASCII-only; use the registered `ufold()` for Ukrainian text.

## Voice

Transcription goes through OpenAI `gpt-4o-transcribe` (`language=uk`) via a single httpx
multipart POST in `family_ea/transcribe.py`. That is the only OpenAI usage; the LLM is Claude.

## Claude API notes

- `anthropic` SDK 1.x. Structured output via `output_config.format` / `client.messages.parse()`,
  not tool_use tricks and not assistant prefill (prefill is rejected on current models).
- Model comes from `LLM_MODEL` env; default `claude-sonnet-5` per spec. Compare with
  `claude-haiku-4-5` once scenario tests exist.
- No thinking/`budget_tokens` config needed; use `thinking: {type: "adaptive"}` if enabling.
