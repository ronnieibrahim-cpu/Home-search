"""Deliver an Email. WP10.

Gmail SMTP over TLS using GMAIL_USER / GMAIL_APP_PASSWORD, to DIGEST_TO.
With --dry-run, or when the secrets are absent, write to out/ (gitignored)
instead and say so. Never log the body: it contains profile-derived numbers.
"""

from __future__ import annotations

from .render import Email


def send(email: Email, dry_run: bool = False) -> str:
    """Returns "sent" or the path written to."""
    raise NotImplementedError("WP10")
