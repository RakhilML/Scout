"""What Scout has learned about sites: which ones let it read, and whose pages hold up.

Every fetch and every run adds to a site's record. Two things follow, both explainable:

* a site that failed several times in a row (blocked, timing out, serving empty script shells) is
  avoided for a while, so its slot goes to a page that can be read; it gets another chance later;
* among equally relevant results, sites whose quotes keep verifying rank a little higher, and
  sites whose findings people rated wrong a little lower.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from scout.web.fetch import FetchStatus

# Failures that say something about the site, not just one page (a 404 is about the page).
SITE_FAILURES = frozenset(
    {
        FetchStatus.BLOCKED,
        FetchStatus.TIMEOUT,
        FetchStatus.NETWORK_ERROR,
        FetchStatus.SERVER_ERROR,
        FetchStatus.JUNK,
        FetchStatus.EMPTY,
    }
)
AVOID_AFTER = 3  # failures in a row
RETRY_AFTER = timedelta(days=7)
RANK_WEIGHT = 0.1  # at most this much relevance is added or taken away


@dataclass(frozen=True, slots=True)
class SiteRecord:
    site: str
    reads: int = 0
    failures: int = 0
    streak: int = 0  # failed fetches in a row
    last_failure: datetime | None = None
    findings: int = 0  # the model's findings from this site
    verified: int = 0  # of which the quote held up
    rated_bad: int = 0  # trusted findings people said were wrong

    def avoided(self, now: datetime) -> str | None:
        """Why the site is skipped for now, or None."""
        if self.streak < AVOID_AFTER or self.last_failure is None:
            return None
        if now - self.last_failure >= RETRY_AFTER:
            return None  # time for another chance
        return f"{self.site} failed {self.streak} times in a row"

    @property
    def standing(self) -> float:
        """From -1 (its findings fail or were rated wrong) to 1 (they verify); 0 when unknown.
        A verified finding people said was wrong is not good evidence, and weighs double."""
        good = max(self.verified - self.rated_bad, 0) + 1
        bad = self.findings - self.verified + 2 * self.rated_bad + 1
        return (good - bad) / (good + bad)

    @property
    def bonus(self) -> float:
        """What the site's standing adds to a search result's relevance."""
        return RANK_WEIGHT * self.standing


class SiteBook(Protocol):
    def site_records(self, sites: Collection[str]) -> dict[str, SiteRecord]: ...
