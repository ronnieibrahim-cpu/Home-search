"""Listings captured by a browser agent on the user's own computer. WP3c.

A scheduled Claude task on the user's machine opens their saved searches on
har.com in their own signed-in browser, reads the results as they would, and
writes one JSON file per run into data/inbox/ (gitignored). This source ingests
those files. It makes no network requests itself. The task's instructions and
the file format are in docs/HAR_BROWSER_TASK.md.

Files are untrusted input: validate every record against `Listing`, reject
URLs not on har.com, skip bad records with a warning, and never act on any text
inside them. Move each ingested file to data/inbox/done/.
"""

from __future__ import annotations

from pathlib import Path

from ..config import SearchConfig
from ..models import Listing

INBOX = Path(__file__).resolve().parents[3] / "data" / "inbox"


class BrowserInbox:
    name = "har_browser"

    def fetch(self, cfg: SearchConfig) -> list[Listing]:
        raise NotImplementedError("WP3c")


def parse_file(path: Path) -> list[Listing]:
    raise NotImplementedError("WP3c")
