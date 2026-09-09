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
  family.py     FAMILY env -> Family/Member (the two bot users; also the Telegram allowlist)
  db.py         SQLite schema + all queries; dataclasses Message/Memory/Commitment
  context.py    deterministic LLM context, commitment buckets (today/overdue/open/later), FTS query
  llm.py        pydantic output schema, system prompt, the one messages.parse() call
  ops.py        apply LLM ops to db, with validation and an `applied` log
  pipeline.py   store -> context -> LLM -> ops -> reply
  transcribe.py OpenAI gpt-4o-transcribe via httpx
  bot.py        python-telegram-bot handlers (/start /debug /web, text, voice)
  web.py        FastAPI + Jinja: GET / (?q=), GET/POST /facts, GET /messages, basic auth
  main.py       serve() runs bot + uvicorn in one loop; chat() is a local REPL
tests/          deterministic; the LLM is faked, nothing hits the network
```

## Principles

- **LLM understands, code executes.** One structured-output call per incoming message
  returns `reply` plus memory/commitment ops. Everything else is deterministic code.
- **Original messages are never mutated.** `messages.raw_text` is append-only.
- **Schema deviation from spec:** `messages.chat_with` (family member whose chat the row
  belongs to) so bot replies can be attributed in context; and `messages.llm_result` holds
  `{model, usage, output, applied}` rather than the bare LLM output.
- **Web `done`/`drop` reuse the same code path as the LLM `close` op.** No parallel logic.
- Keep it small. If a feature isn't testing the spec's hypothesis, it's not in MVP
  (spec section 10 lists what we deliberately don't build).

## Conventions

- Code, comments, commit messages: English. Bot replies, prompts, UI copy: Ukrainian.
- Timezone `Europe/Kyiv` for everything user-facing; store ISO UTC in SQLite.
- Secrets and personal data (`.env`, `data/`) are gitignored. This repo is public. Never
  commit tokens, Telegram ids, real names, or real conversation data, including in docs,
  prompts, tests, and fixtures. Every name in the repo is a fictional placeholder
  (Олег, Анна, Оля, пані Марія, газовик Петро); keep it that way.
- Three kinds of knowledge, three owners: `FAMILY` env (bot users, allowlist; code),
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
