"""Archived copies of pages that are gone, from the Wayback Machine (web.archive.org).

A dead page's newest working copy (the newest that is not an archived error page: working)
is the nearest one to its last good reading: every copy taken while the
page worked predates its death. Only the page's address is sent to the archive.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple, Protocol
from urllib.parse import urldefrag, urlencode

from scout.errors import ConfigError, FetchError
from scout.web import fragments
from scout.web.domains import matches_any
from scout.web.fetch import TRANSIENT_STATUSES, Document, Fetcher, FetchStatus

HOST = "web.archive.org"
CDX = f"https://{HOST}/cdx/search/cdx"
LOOKUP_TIMEOUT = 90.0  # seconds: the index often takes half a minute, at times more
_COPIES = 3  # copies read at most to find one that is not an archived error page
_STAMP = "%Y%m%d%H%M%S"
_REPLAY = re.compile(rf"https?://{re.escape(HOST)}/web/(\d{{14}})(?:[a-z]{{2}}_)?/(.+)")


class Snapshot(NamedTuple):
    url: str  # the page's address, as the archive recorded it
    taken: datetime  # UTC

    @property
    def page(self) -> str:
        """The copy as the archive shows it, which opens at a text fragment: for links."""
        return f"https://{HOST}/web/{self.taken:{_STAMP}}/{self.url}"

    @property
    def raw(self) -> str:
        """The copy as the page served it, without the archive's toolbar: for reading."""
        return f"https://{HOST}/web/{self.taken:{_STAMP}}id_/{self.url}"

    @property
    def taken_on(self) -> str:
        return self.taken.date().isoformat()


@dataclass(frozen=True, slots=True)
class Copy:
    """What the archive holds of a dead page that was cited with a quote."""

    snapshot: Snapshot
    unread: str | None = None  # why the copy could not be read
    holds: bool | None = None  # it states the quote; None: no quote to look for, or unread
    anchor: str | None = None  # where the quote is on it

    @property
    def link(self) -> str:
        """The copy, opening at the quote when it holds it."""
        return fragments.link(self.snapshot.page, self.anchor)


class Archive(Protocol):
    def recent(
        self, url: str, *, before: datetime | None = None, count: int = 1
    ) -> list[Snapshot]: ...

    def read(self, snapshot: Snapshot) -> Document: ...


class Wayback:
    """The Wayback Machine, asked through *fetcher*, whose rules (public addresses only) hold."""

    def __init__(self, fetcher: Fetcher, *, timeout: float = LOOKUP_TIMEOUT) -> None:
        self._fetcher = fetcher
        self._timeout = timeout

    def recent(self, url: str, *, before: datetime | None = None, count: int = 1) -> list[Snapshot]:
        """The *count* newest copies of *url* taken while the page worked (served whole, HTTP
        200), and before *before* if given, newest first. The nearest copy by date would not do:
        it is often an archived error page. FetchError when the archive does not answer."""
        query = {
            "url": urldefrag(url).url,
            "output": "json",
            "fl": "timestamp,original",
            "filter": "statuscode:200",
            "limit": f"-{count}",
            "fastLatest": "true",
        }
        if before is not None:
            query["to"] = f"{before.astimezone(UTC):{_STAMP}}"
        rows = self._fetcher.json(f"{CDX}?{urlencode(query)}", timeout=self._timeout)
        found = []
        for row in rows[1:] if isinstance(rows, list) else []:  # after the header row
            if isinstance(row, list) and len(row) == 2 and all(isinstance(v, str) for v in row):
                taken = _taken(row[0])
                if taken is not None:
                    found.append(Snapshot(row[1], taken))
        return found[::-1]  # the index lists them oldest first

    def read(self, snapshot: Snapshot) -> Document:
        """The copy, read like any page: a copy never changes, so a cached reading is as good.
        FetchError when the archive did not serve it (throttled, down, slow): that says nothing
        of the copy, and the next run may read it."""
        document = self._fetcher.fetch_many([snapshot.raw], deadline=self._timeout)[0]
        if document.status in TRANSIENT_STATUSES or document.status is FetchStatus.BLOCKED:
            raise FetchError(document.error or document.status.value.replace("_", " "))
        return document


def working(
    archive: Archive,
    url: str,
    *,
    rejected: Callable[[Document], bool],
    before: datetime | None = None,
) -> tuple[Snapshot, Document] | None:
    """The newest copy of *url* the archive holds (taken before *before*) that is not
    *rejected* (an archived error page), read. Copies served with HTTP 200 skip a page's
    redirects, but not a "Page not found" template or a parking page that took its place: the
    newest copy is then that page. When the few newest are all rejected, the newest, for its
    judgment to tell. None when there is no copy; FetchError when the archive did not answer."""
    newest = None
    for snapshot in archive.recent(url, before=before, count=_COPIES):
        document = archive.read(snapshot)
        if not rejected(document):
            return snapshot, document
        newest = newest or (snapshot, document)
    return newest


def check_archive(spec: str) -> None:
    """ConfigError unless *spec* names an archive SCOUT_ARCHIVE may: "wayback" or "off"."""
    if spec not in ("wayback", "off"):
        raise ConfigError(f"SCOUT_ARCHIVE must be wayback or off, not {spec!r}")


def make_archive(spec: str, fetcher: Fetcher) -> Archive | None:
    """The archive SCOUT_ARCHIVE names: "wayback" (the default), or none ("off")."""
    check_archive(spec)
    return Wayback(fetcher) if spec == "wayback" else None


def snapshot_of(url: str) -> Snapshot | None:
    """The copy a Wayback address shows; None for any other address."""
    found = _REPLAY.fullmatch(url)
    if found is None:
        return None
    taken = _taken(found[1])
    return Snapshot(found[2], taken) if taken is not None else None


def in_archive(url: str) -> bool:
    """*url* is the archive's own (an archived copy, cited as such): never looked up there."""
    return matches_any(url, ("archive.org",))


def _taken(stamp: str) -> datetime | None:
    if not (len(stamp) == 14 and stamp.isdigit()):  # strptime would read "2023012" as a date
        return None
    try:
        return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
