"""Who is looking at the web view: signed tokens, no passwords, no sessions table.

Identity comes from the bot. The digest, /today and /web end with a link carrying a `link`
token; opening it sets a `session` cookie carrying a longer-lived token of the same shape;
`pull` signs a `backup` token itself. A token is `subject.expires.tag`: the expiry in
base36 seconds and a 15-byte HMAC-SHA256 tag under WEB_SECRET over
`purpose:subject:expires`, base64url. The purpose is not in the token: where a token is
presented (the login path, the cookie, the bearer header) says which one it must be, and
the tag binds it. Short on purpose: the link ends a Telegram message. Nothing is stored:
a member leaving the family or a new WEB_SECRET is the only revocation, and a link stays
valid until it expires however many times it is opened.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

LINK_TTL = timedelta(hours=24)  # the digest link is tapped hours after the push
SESSION_TTL = timedelta(days=365)
BACKUP_TTL = timedelta(minutes=5)  # `pull` signs one and uses it at once
TAG_BYTES = 15  # 120 bits of HMAC: 20 base64url characters, no padding

_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _DIGITS[r] + out
    return out or "0"


def _tag(secret: str, purpose: str, subject: str, expires: int) -> str:
    payload = f"{purpose}:{subject}:{expires}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:TAG_BYTES]
    return base64.urlsafe_b64encode(digest).decode()


def sign(
    secret: str, purpose: str, subject: str, ttl: timedelta, now: datetime | None = None
) -> str:
    expires = int(((now or datetime.now(UTC)) + ttl).timestamp())
    return f"{subject}.{_b36(expires)}.{_tag(secret, purpose, subject, expires)}"


def verify(secret: str, token: str, purpose: str, now: datetime | None = None) -> str | None:
    """The subject of a token signed with `secret` for `purpose` and not expired; else None."""
    try:
        subject, expires36, tag = token.split(".")
        expires = int(expires36, 36)
    except ValueError:
        return None
    expected = _tag(secret, purpose, subject, expires)
    if not subject or not hmac.compare_digest(expected.encode(), tag.encode()):
        return None
    if expires <= int((now or datetime.now(UTC)).timestamp()):
        return None
    return subject
