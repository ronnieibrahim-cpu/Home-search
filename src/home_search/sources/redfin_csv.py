"""Redfin search-results CSV download, per zip. WP3.

Uses the same CSV export a signed-out browser gets from a search page. Rules:
one request per zip per run, a 5-10 s jittered pause between requests, an honest
User-Agent, no retries on 403/429 (raise SourceUnavailable), and no detail-page
fetching. If Redfin blocks the runner, the email source still covers the run.
"""

from __future__ import annotations

from ..config import SearchConfig
from ..models import Listing


class RedfinCsv:
    name = "redfin_csv"

    def fetch(self, cfg: SearchConfig) -> list[Listing]:
        raise NotImplementedError("WP3")


def parse_csv(text: str, kind: str) -> list[Listing]:
    """Pure parser, separated so it is tested against tests/fixtures without network."""
    raise NotImplementedError("WP3")
