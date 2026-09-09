"""People known to the assistant, loaded from family.yaml.

Not stored in the database: the file is read at startup and goes into the prompt.
`telegram_id` doubles as the allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    telegram_id: int | None = None
    related_to: str | None = None


class People:
    def __init__(self, persons: list[Person]) -> None:
        self._by_id = {p.id: p for p in persons}
        self._by_tg = {p.telegram_id: p for p in persons if p.telegram_id is not None}

    @classmethod
    def load(cls, path: Path) -> People:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        persons = []
        for pid, attrs in raw.items():
            attrs = attrs or {}
            tg = attrs.get("telegram_id")
            persons.append(
                Person(
                    id=str(pid),
                    name=str(attrs.get("name", pid)),
                    role=str(attrs.get("role", "family")),
                    telegram_id=int(tg) if tg is not None else None,
                    related_to=attrs.get("related_to"),
                )
            )
        return cls(persons)

    def get(self, pid: str) -> Person | None:
        return self._by_id.get(pid)

    def by_telegram_id(self, tg_id: int) -> Person | None:
        return self._by_tg.get(tg_id)

    @property
    def all(self) -> list[Person]:
        return list(self._by_id.values())

    @property
    def family(self) -> list[Person]:
        """People who talk to the bot and can own commitments."""
        return [p for p in self.all if p.role == "family"]

    @property
    def telegram_ids(self) -> list[int]:
        return list(self._by_tg)

    def display_name(self, pid: str | None) -> str:
        if pid is None:
            return "—"
        p = self.get(pid)
        return p.name if p else pid

    def describe(self) -> str:
        """Prompt-friendly list of people."""
        lines = []
        for p in self.all:
            line = f"- {p.id}: {p.name}, {p.role}"
            if p.related_to:
                line += f", пов'язано з {self.display_name(p.related_to)}"
            if p.telegram_id is not None:
                line += " (пише боту)"
            lines.append(line)
        return "\n".join(lines)
