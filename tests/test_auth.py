"""Signed web tokens: links, cookies, the backup bearer."""

from datetime import UTC, datetime, timedelta

from family_ea.auth import sign, verify

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def test_roundtrip_and_expiry() -> None:
    token = sign("k", "link", "anna", HOUR, now=NOW)
    assert verify("k", token, "link", now=NOW) == "anna"
    assert verify("k", token, "link", now=NOW + timedelta(minutes=59)) == "anna"
    assert verify("k", token, "link", now=NOW + HOUR) is None


def test_rejects_other_purpose_secret_and_tampering() -> None:
    token = sign("k", "link", "anna", HOUR, now=NOW)
    assert verify("k", token, "session", now=NOW) is None
    assert verify("other", token, "link", now=NOW) is None
    tag = token.split(".")[1]
    forged = sign("k", "link", "oleh", HOUR, now=NOW).split(".")[0] + "." + tag
    assert verify("k", forged, "link", now=NOW) is None
    for junk in ["", ".", "a.b", "notatoken", token + "x", "x" + token, "%%%.%%%", "a.b.c"]:
        assert verify("k", junk, "link", now=NOW) is None, junk
