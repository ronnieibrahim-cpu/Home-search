"""Source contract. Every listing source is one class implementing `Source`."""

from __future__ import annotations

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


def enabled_sources() -> list[Source]:
    """Instantiate the sources whose prerequisites (secrets, network) are present."""
    raise NotImplementedError("WP3/WP4")
