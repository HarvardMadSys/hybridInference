"""Email-domain blocklist for signup."""

from __future__ import annotations

BLOCKED_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "test.com",
        "test.org",
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "tempmail.com",
        "tempmail.net",
        "10minutemail.com",
        "yopmail.com",
        "trashmail.com",
        "throwawaymail.com",
        "fakeinbox.com",
        "getairmail.com",
        "dispostable.com",
        "maildrop.cc",
    }
)

# RFC 2606 reserved TLDs that must never receive real mail.
_RESERVED_TLDS: tuple[str, ...] = (".test", ".example", ".invalid", ".localhost")


def is_email_domain_blocked(email: str) -> bool:
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return False
    if domain in BLOCKED_EMAIL_DOMAINS:
        return True
    return any(domain == tld[1:] or domain.endswith(tld) for tld in _RESERVED_TLDS)
