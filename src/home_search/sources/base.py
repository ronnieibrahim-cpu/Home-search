"""Source contract. Every listing source is one class implementing `Source`."""

from __future__ import annotations

import os
from typing import Protocol

from ..config import SearchConfig
from ..models import Listing


class SourceUnavailable(Exception):
    """Blocked, rate-limited, or changed format. The pipeline logs it, carries on
    with the other sources, and reports the outage in the digest footer."""


class Source(Protocol):
    name: str

    def fetch(self, cfg: SearchConfig) -> list[Listing]:
        """Return current sale (and, if cfg.rent.enabled, rental) listings for
        every zip in every area, already filtered to the price band. Raise
        SourceUnavailable rather than returning a partial, silently short list."""
        ...


SOURCE_NAMES = ("har_browser", "email_alerts", "har_web", "redfin_csv")


def enabled_sources(only: list[str] | None = None) -> list[Source]:
    """Instantiate the sources whose prerequisites (secrets, network) are present,
    in this order:

    - har_browser (BrowserInbox): always; an empty inbox simply yields [].
    - email_alerts (EmailAlerts): only when GMAIL_USER and GMAIL_APP_PASSWORD are set.
    - har_web (HarWeb): only when HOME_SEARCH_ENABLE_HAR_WEB=1 (meant for the
      owner's own computer; HAR refuses most datacenter IPs).
    - redfin_csv (RedfinCsv): unless HOME_SEARCH_DISABLE_REDFIN=1.

    `only`, if given, keeps just the named sources (still subject to the rules
    above). An unknown name raises ValueError.
    """
    from .browser_inbox import BrowserInbox
    from .email_alerts import EmailAlerts
    from .har_web import HarWeb
    from .redfin_csv import RedfinCsv

    if only is not None:
        unknown = sorted(set(only) - set(SOURCE_NAMES))
        if unknown:
            raise ValueError(f"unknown source(s) {unknown}; known: {list(SOURCE_NAMES)}")

    sources: list[Source] = [BrowserInbox()]
    if os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
        sources.append(EmailAlerts())
    if os.environ.get("HOME_SEARCH_ENABLE_HAR_WEB") == "1":
        sources.append(HarWeb())
    if os.environ.get("HOME_SEARCH_DISABLE_REDFIN") != "1":
        sources.append(RedfinCsv())
    if only is not None:
        sources = [s for s in sources if s.name in only]
    return sources
