"""The family members who talk to the bot. Everything else about the family lives in facts and
the journal.

Only one thing comes from the environment: `ADMIN_USER_ID`, the Telegram id of the person
who is always let in. Members live in the `members` table and are edited on the web
(`/family`); the admin's own row appears on first contact, from the Telegram profile.
`telegram_id` doubles as the allowlist; `id` is a latin slug derived from the name
(Олег -> oleh) and is what the LLM uses as `owner`.
"""

from __future__ import annotations

import re

from .db import Database, Member

_ID = re.compile(r"^[a-z][a-z0-9_]*$")

# Ukrainian -> latin per the national standard (KMU 2010), plus a few Russian letters
# that show up in Telegram names. `_INITIAL` are the word-initial variants.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ю": "iu",
    "я": "ia", "ь": "", "ы": "y", "э": "e", "ё": "e", "ъ": "",
}  # fmt: skip
_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def slugify(name: str) -> str:
    """'Олег' -> 'oleh', 'пані Марія' -> 'pani_mariia'. Never empty, always a valid id."""
    out: list[str] = []
    at_start = True
    for ch in name.casefold():
        if ch in "'’":
            continue
        if ch.isalnum():
            if ch in _TRANSLIT:
                out.append(_INITIAL[ch] if at_start and ch in _INITIAL else _TRANSLIT[ch])
            elif ch.isascii():
                out.append(ch)
            at_start = False
        else:
            out.append("_")
            at_start = True
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")
    if not slug:
        return "member"
    return slug if _ID.match(slug) else f"m_{slug}"


class Family:
    def __init__(self, db: Database, admin_telegram_id: int | None = None) -> None:
        self.db = db
        self.admin_telegram_id = admin_telegram_id

    def is_admin(self, telegram_id: int) -> bool:
        return self.admin_telegram_id is not None and telegram_id == self.admin_telegram_id

    def get(self, pid: str) -> Member | None:
        return self.db.get_member(pid)

    def by_telegram_id(self, tg_id: int) -> Member | None:
        return self.db.member_by_telegram_id(tg_id)

    @property
    def members(self) -> list[Member]:
        """People who talk to the bot and can own todos."""
        return self.db.list_members()

    def add(
        self, name: str, telegram_id: int | None = None, *, member_id: str | None = None
    ) -> Member:
        """Add a member. The id is derived from the name unless given, and made unique."""
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        if member_id is None:
            base = slugify(name)
            member_id, n = base, 2
            while self.db.get_member(member_id) is not None:
                member_id = f"{base}{n}"
                n += 1
        elif not _ID.match(member_id):
            raise ValueError(f"bad member id {member_id!r}: use lowercase latin, e.g. 'oleh'")
        return self.db.add_member(member_id, name, telegram_id)

    def update(self, pid: str, *, name: str, telegram_id: int | None) -> bool:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        return self.db.update_member(pid, name=name, telegram_id=telegram_id)

    def display_name(self, pid: str | None) -> str:
        if pid is None:
            return "—"
        p = self.get(pid)
        return p.name if p else pid

    def describe(self) -> str:
        """Prompt-friendly list: `- id: Name` per line."""
        return "\n".join(f"- {p.id}: {p.name}" for p in self.members)
