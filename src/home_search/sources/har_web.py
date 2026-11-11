"""HAR.com public search results, per zip. WP3b. Preferred web source.

HAR is the Houston MLS's own consumer site, so its listings are the most
complete and current, and each carries HAR's zoned-school fields (kept as a
LISTING_CLAIM cross-check only; the boundary polygon still decides zoning).

Access rules, same as every listing site here (see CLAUDE.md): fetch the public
search-results view a signed-out visitor gets, one request per zip per kind per
run with jittered pauses, an honest User-Agent, no login automation, no
detail-page crawling, and no evasion of any kind. If HAR answers with a
challenge page, 403 or 429, raise SourceUnavailable and stop for the run.

HAR is expected to refuse datacenter IPs more often than not, so this source is
built to run from the user's own computer (`home-search scan --sources har_web`)
and its results reach CI through the shared state database. The HAR alert-email
source (WP4) is the always-on path and needs no web access at all.
"""

from __future__ import annotations

from ..config import SearchConfig
from ..models import Listing


class HarWeb:
    name = "har_web"

    def fetch(self, cfg: SearchConfig) -> list[Listing]:
        raise NotImplementedError("WP3b")


def parse_results(html: str, kind: str) -> list[Listing]:
    """Pure parser over a saved results page; tested against tests/fixtures."""
    raise NotImplementedError("WP3b")
