"""Settings from the environment. A local `.env` is loaded if present (dev only)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_token: str | None
    anthropic_api_key: str | None
    openai_api_key: str | None
    database_path: Path
    family: str | None  # id:telegram_id:name,... see family.py
    web_user: str | None
    web_password: str | None
    web_url: str | None
    llm_model: str
    llm_effort: str
    port: int
    tz: ZoneInfo

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = ", ".join(n.upper() for n in missing)
            raise SystemExit(f"missing required settings: {env_names}")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def load_settings() -> Settings:
    return Settings(
        telegram_token=_env("TELEGRAM_TOKEN"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        openai_api_key=_env("OPENAI_API_KEY"),
        database_path=Path(_env("DATABASE_PATH", "./data/family.db") or ""),
        family=_env("FAMILY"),
        web_user=_env("WEB_USER"),
        web_password=_env("WEB_PASSWORD"),
        web_url=_env("WEB_URL"),
        llm_model=_env("LLM_MODEL", "claude-sonnet-5") or "",
        llm_effort=_env("LLM_EFFORT", "medium") or "",
        port=int(_env("PORT", "8080") or 8080),
        tz=ZoneInfo(_env("TZ", "Europe/Kyiv") or "Europe/Kyiv"),
    )
