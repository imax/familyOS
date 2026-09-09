"""The family members who talk to the bot. Everything else about the family lives in memories.

Configured with one env var so nothing personal ever lands in the repo:

    FAMILY=oleh:123456:Олег,anna:234567:Анна      # id:telegram_id:display name

`telegram_id` doubles as the allowlist; `id` is what the LLM uses as `owner`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    telegram_id: int | None = None


class Family:
    def __init__(self, persons: list[Member]) -> None:
        if not persons:
            raise ValueError("at least one person is required")
        self._by_id = {p.id: p for p in persons}
        self._by_tg = {p.telegram_id: p for p in persons if p.telegram_id is not None}

    @classmethod
    def from_env(cls, spec: str) -> Family:
        """Parse `id:telegram_id:name,id:telegram_id:name`."""
        persons = []
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [x.strip() for x in chunk.split(":")]
            if len(parts) != 3 or not all(parts):
                raise ValueError(f"bad FAMILY entry {chunk!r}: expected id:telegram_id:name")
            pid, tg, name = parts
            if not _ID.match(pid):
                raise ValueError(f"bad FAMILY id {pid!r}: use lowercase latin, e.g. 'oleh'")
            if not tg.isdigit():
                raise ValueError(f"bad FAMILY telegram_id {tg!r} for {pid}: must be a number")
            persons.append(Member(pid, name, int(tg)))
        return cls(persons)

    def get(self, pid: str) -> Member | None:
        return self._by_id.get(pid)

    def by_telegram_id(self, tg_id: int) -> Member | None:
        return self._by_tg.get(tg_id)

    @property
    def members(self) -> list[Member]:
        """People who talk to the bot and can own commitments."""
        return list(self._by_id.values())

    @property
    def telegram_ids(self) -> list[int]:
        return list(self._by_tg)

    def display_name(self, pid: str | None) -> str:
        if pid is None:
            return "—"
        p = self.get(pid)
        return p.name if p else pid

    def describe(self) -> str:
        """Prompt-friendly list: `- id: Name` per line."""
        return "\n".join(f"- {p.id}: {p.name}" for p in self.members)
