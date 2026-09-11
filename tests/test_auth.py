"""Signed web tokens: links, cookies, the backup bearer."""

from datetime import UTC, datetime, timedelta

from family_ea.auth import sign, verify

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def test_roundtrip_and_expiry() -> None:
    token = sign("k", "link", "anna", HOUR, now=NOW)
    subject, expires, tag = token.split(".")
    assert subject == "anna" and len(expires) == 6 and len(tag) == 20  # short: ends a message
    assert verify("k", token, "link", now=NOW) == "anna"
    assert verify("k", token, "link", now=NOW + timedelta(minutes=59)) == "anna"
    assert verify("k", token, "link", now=NOW + HOUR) is None


def test_rejects_other_purpose_secret_and_tampering() -> None:
    token = sign("k", "link", "anna", HOUR, now=NOW)
    assert verify("k", token, "session", now=NOW) is None
    assert verify("other", token, "link", now=NOW) is None
    subject, expires, tag = token.split(".")
    assert verify("k", f"oleh.{expires}.{tag}", "link", now=NOW) is None  # another subject
    assert verify("k", f"{subject}.zzzzzz.{tag}", "link", now=NOW) is None  # a later expiry
    junk = ["", ".", "a.b", "notatoken", token + "x", "x" + token, "%%%.%%%", "a.b.c", "anna.ж.ж"]
    for t in junk:
        assert verify("k", t, "link", now=NOW) is None, t
