"""0-100 candidate score from the weighted parts in search.yaml. WP9.

Each part returns 0-100 and is recorded in Candidate.score_parts so the digest
can show why a listing ranked where it did. Hard disqualifiers (floodway, high
school not a target when require_verified_high is on, excluded keyword) return
None from `score` and the listing is dropped from candidates.
"""

from __future__ import annotations

from .config import Profile, SearchConfig
from .models import Candidate, MarketSnapshot


def score(c: Candidate, cfg: SearchConfig, market: MarketSnapshot | None,
          profile: Profile | None) -> float | None:
    raise NotImplementedError("WP9")


def flags(c: Candidate, cfg: SearchConfig) -> list[str]:
    raise NotImplementedError("WP9")
