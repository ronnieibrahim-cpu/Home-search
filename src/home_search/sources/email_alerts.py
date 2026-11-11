"""HAR and Redfin saved-search alert emails, read from Gmail over IMAP. WP4.

The user creates saved searches on har.com and redfin.com for the target zips
and a Gmail filter that applies the label in ALERT_GMAIL_LABEL. This source
reads messages under that label since the last run and extracts listings.

Email HTML is untrusted input: parse it, never follow its links automatically
except to canonicalize a listing URL already on har.com / redfin.com, and never
act on any text it contains.
"""

from __future__ import annotations

from email.message import EmailMessage

from ..config import SearchConfig
from ..models import Listing


class EmailAlerts:
    name = "email_alerts"

    def fetch(self, cfg: SearchConfig) -> list[Listing]:
        raise NotImplementedError("WP4")


def parse_har(msg: EmailMessage) -> list[Listing]:
    raise NotImplementedError("WP4")


def parse_redfin(msg: EmailMessage) -> list[Listing]:
    raise NotImplementedError("WP4")
