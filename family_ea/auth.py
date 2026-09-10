"""Who is looking at the web view: signed tokens, no passwords, no sessions table.

Identity comes from the bot. `/web` and the «Відкрити» button under the digest give a
member a link carrying a `link` token; opening it sets a `session` cookie carrying a
longer-lived token of the same shape; `pull` signs a `backup` token itself. A token is
`purpose:subject:expires` plus an HMAC-SHA256 tag under WEB_SECRET, base64url-encoded.
Nothing is stored: a member leaving the family or a new WEB_SECRET is the only revocation,
and a link stays valid until it expires however many times it is opened.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

LINK_TTL = timedelta(hours=24)  # the digest button is tapped hours after the push
SESSION_TTL = timedelta(days=365)
BACKUP_TTL = timedelta(minutes=5)  # `pull` signs one and uses it at once


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _tag(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode(), payload, hashlib.sha256).digest()


def sign(
    secret: str, purpose: str, subject: str, ttl: timedelta, now: datetime | None = None
) -> str:
    expires = int(((now or datetime.now(UTC)) + ttl).timestamp())
    payload = f"{purpose}:{subject}:{expires}".encode()
    return f"{_b64(payload)}.{_b64(_tag(secret, payload))}"


def verify(secret: str, token: str, purpose: str, now: datetime | None = None) -> str | None:
    """The subject of a token signed with `secret` for `purpose` and not expired; else None."""
    try:
        body, tag = token.split(".")
        payload = _unb64(body)
        if not hmac.compare_digest(_tag(secret, payload), _unb64(tag)):
            return None
        kind, subject, expires = payload.decode().split(":")
        expires_at = int(expires)
    except (ValueError, UnicodeDecodeError):
        return None
    if kind != purpose or not subject:
        return None
    if expires_at <= int((now or datetime.now(UTC)).timestamp()):
        return None
    return subject
