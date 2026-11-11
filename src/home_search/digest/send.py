"""Deliver an Email. WP10.

Gmail SMTP over TLS using GMAIL_USER / GMAIL_APP_PASSWORD, to DIGEST_TO.
With --dry-run, or when the secrets are absent, write to out/ (gitignored)
instead and say so. Never log the body: it contains profile-derived numbers.

Details:
- smtp.gmail.com:587 with STARTTLS (certificate verified), then login with the
  app password. The message is multipart/alternative: text/plain first, then
  text/html, so clients that cannot show HTML get the plain-text part.
- DIGEST_TO may hold several addresses separated by commas; it defaults to
  GMAIL_USER.
- Files are written as `out/<YYYYmmdd-HHMMSS>-<slug>.html` and `.txt`. The
  slug comes from the fixed part of the subject before the colon ("home-search",
  "hot-listing"), never from the profile-derived rest of it.
- `SMTP` and `OUT_DIR` are module attributes so tests can replace them; tests
  never open a socket.
- Logging names only the file path or "sent"; never the subject, body or
  recipients. A failed send saves the email to out/ and raises
  `DeliveryError` naming only the exception type and SMTP status code.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from ..config import ROOT
from .render import Email

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT_S = 30
SMTP = smtplib.SMTP            # replaced in tests
OUT_DIR = ROOT / "out"         # gitignored
SENT = "sent"


class DeliveryError(RuntimeError):
    """Sending failed. The message never contains the email's subject or body."""


def _credentials() -> tuple[str, str, list[str]] | None:
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not user or not password:
        return None
    to = [a.strip() for a in os.environ.get("DIGEST_TO", "").split(",") if a.strip()]
    return user, password, to or [user]


def _slug(subject: str) -> str:
    head = subject.split(":", 1)[0] if ":" in subject else "email"
    slug = re.sub(r"[^a-z0-9]+", "-", head.lower()).strip("-")
    return slug[:40] or "email"


def write_files(email: Email, out_dir: Path | None = None) -> Path:
    """Write `.html` and `.txt` next to each other; returns the html path."""
    out = Path(out_dir) if out_dir is not None else Path(OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{_slug(email.subject)}"
    base, n = stem, 1
    while (out / f"{base}.html").exists() or (out / f"{base}.txt").exists():
        n += 1
        base = f"{stem}-{n}"
    html_path = out / f"{base}.html"
    html_path.write_text(email.html, encoding="utf-8")
    (out / f"{base}.txt").write_text(email.text, encoding="utf-8")
    return html_path


def build_message(email: Email, sender: str, recipients: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = " ".join(email.subject.split())     # no header injection via newlines
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    msg.set_content(email.text, subtype="plain", charset="utf-8")
    msg.add_alternative(email.html, subtype="html", charset="utf-8")
    return msg


def send(email: Email, dry_run: bool = False) -> str:
    """Returns "sent" or the path written to."""
    creds = None if dry_run else _credentials()
    if creds is None:
        path = write_files(email)
        reason = "dry run" if dry_run else "GMAIL_USER / GMAIL_APP_PASSWORD not set"
        log.info("%s: email written to %s (not sent)", reason, path)
        return str(path)

    user, password, recipients = creds
    msg = build_message(email, user, recipients)
    try:
        with SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg, from_addr=user, to_addrs=recipients)
    except (smtplib.SMTPException, OSError) as e:
        code = getattr(e, "smtp_code", None)
        detail = f"{type(e).__name__}" + (f", SMTP {code}" if code else "")
        try:
            saved = f"; the email was saved to {write_files(email)} instead"
        except OSError:
            saved = ""
        # `from None`: the original exception can carry server text and addresses.
        raise DeliveryError(f"email delivery via {SMTP_HOST} failed ({detail}){saved}") from None
    log.info("email sent via %s", SMTP_HOST)
    return SENT
