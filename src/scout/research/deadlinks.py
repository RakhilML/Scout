"""Dead cited pages, and which archived copy, or live page it moved to, may be cited in place
of each.

Link checkers only say a link is dead, and archive bots swap in the nearest copy unchecked: often
an archived error page, a parked domain, or a later page that no longer says what the text cites
it for. Here a copy stands in for a dead page only when a quote verified on the copy backs a
claim cited to the page, which proves it holds what the text cited; and the page it moved to
stands in, ahead of the copy, only when that quote is on it too. Everything is read from a
cite-check's result, so a stored run tells the same.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

from scout.research.factcheck import (
    NOT_JUDGED,
    cited_label,
    gone,
    no_copy,
    not_looked_up,
    why_unread,
)
from scout.research.results import ClaimCheck, Finding, RunResult, Source
from scout.web import fragments
from scout.web.archive import snapshot_of
from scout.web.domains import canonical_url

MOVED = "moved"  # the live page it moved to holds a quote backing a claim: cite that instead
REPLACE = "replace"  # its copy backs a claim cited to it: cite the copy instead
CONTRADICTED = "contradicted"
NOT_FOUND = "not found"
COPY_UNREADABLE = "copy unreadable"
NOT_ARCHIVED = "not archived"
NOT_LOOKED_UP = "not looked up"
_ANOTHER_SOURCE = (CONTRADICTED, NOT_FOUND, COPY_UNREADABLE, NOT_ARCHIVED)


class DeadLink(NamedTuple):
    n: int  # the text's citation number
    url: str
    why: str  # why it could not be read: "not found: HTTP 404"
    state: str
    claims: tuple[int, ...]  # the checked claims citing it, numbered from 1
    copy: Source | None = None  # its archived copy, when one was read
    # The claims a quote verified on the copy backs (once it MOVED, on the page it moved to),
    # and those a quote on the copy contradicts.
    backs: tuple[int, ...] = ()
    contradicts: tuple[int, ...] = ()
    quote: Finding | None = None  # the first quote backing a claim, or else contradicting one
    note: str | None = None  # why the copy could not be read, or the page was not looked up
    moved: Source | None = None  # the live page it moved to

    @property
    def link(self) -> str | None:
        """The page to cite instead, opening at the quote: where it moved, or else its copy;
        None unless it backs a claim."""
        anchor = self.quote.anchor if self.quote is not None else None
        if self.state == MOVED and self.moved is not None:
            return fragments.link(self.moved.url, anchor)
        if self.state == REPLACE and self.copy is not None:
            return fragments.link(self.copy.url, anchor)
        return None

    @property
    def taken(self) -> str | None:
        snapshot = snapshot_of(self.copy.url) if self.copy is not None else None
        return snapshot.taken_on if snapshot is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "url": self.url,
            "why": self.why,
            "state": self.state,
            "copy": self.copy.url if self.copy is not None else None,
            "taken": self.taken,
            "moved": self.moved.url if self.moved is not None else None,
            "link": self.link,
            "quote": self.quote.quote if self.quote is not None else None,
            "claims": list(self.claims),
            "backs": list(self.backs),
            "contradicts": list(self.contradicts),
            "note": self.note,
        }


def dead_links(result: RunResult) -> list[DeadLink]:
    """The gone pages that *result*'s checked claims cite, in citation order, each with what its
    archived copy said, and where it moved. Empty unless *result* is a cite-check that looked
    copies up: one in which a claim has an archived reading, as every claim citing a gone page
    then has."""
    if not result.cited or all(claim.archived is None for claim in result.claims):
        return []
    copies = {source.copy_of: source for source in result.sources if source.copy_of is not None}
    moves = {s.moved_from: s for s in result.sources if s.moved_from is not None}
    found = []
    for page in result.sources:
        citing = [
            (number, claim)
            for number, claim in enumerate(result.claims, start=1)
            if page.index in claim.pages
        ]
        if page.copy_of is None and citing and gone(page):
            copy, moved = copies.get(page.index), moves.get(page.index)
            found.append(_dead(page, citing, copy, moved, result))
    return found


def _dead(
    page: Source,
    citing: Sequence[tuple[int, ClaimCheck]],
    copy: Source | None,
    moved: Source | None,
    result: RunResult,
) -> DeadLink:
    numbers = tuple(number for number, _ in citing)
    readings = [(number, claim.archived) for number, claim in citing if claim.archived is not None]
    dead = DeadLink(page.index, page.url, why_unread(page), NOT_LOOKED_UP, numbers)
    if copy is None:
        problems = [problem for _, reading in readings for problem in reading.problems]
        if no_copy(page.index) in problems:
            return dead._replace(state=NOT_ARCHIVED)
        prefix = not_looked_up(page.index, "")
        why = next((p.removeprefix(prefix) for p in problems if p.startswith(prefix)), None)
        return dead._replace(note=why)
    if copy.snippet_only:
        return dead._replace(state=COPY_UNREADABLE, copy=copy, note=why_unread(copy))
    evidence = result.numbered

    def quoted(side: str, on: int) -> list[tuple[int, Finding]]:
        return [
            (number, evidence[n - 1])
            for number, reading in readings
            for n in getattr(reading, side)
            if evidence[n - 1].source == on
        ]

    backing, against = quoted("supports", copy.index), quoted("refutes", copy.index)
    live = quoted("supports", moved.index) if moved is not None else []
    contradicts = tuple(dict.fromkeys(number for number, _ in against))
    if live:
        state, backing = MOVED, live
    elif backing:
        state = REPLACE
    elif contradicts:
        state = CONTRADICTED
    elif any(cited_label(reading, result.sources) == NOT_JUDGED for _, reading in readings):
        state = NOT_JUDGED  # it may back the claim the model could not judge on it
    else:
        state = NOT_FOUND
    backs = tuple(dict.fromkeys(number for number, _ in backing))
    quote = next((finding for _, finding in (*backing, *against)), None)
    return dead._replace(
        state=state, copy=copy, backs=backs, contradicts=contradicts, quote=quote, moved=moved
    )


def summary(links: Sequence[DeadLink]) -> str:
    """What can be done about the dead pages, as their list starts ("2 cited pages are gone: 1
    can be replaced by its archived copy, which backs a claim the text cites it for; 1 needs
    another source.")."""
    states = Counter(link.state for link in links)
    parts = []
    if moved := states[MOVED]:
        new = (
            "its new address still states what the text cites it for"
            if moved == 1
            else "their new addresses still state what the text cites them for"
        )
        parts.append(f"{moved} moved, and {new}")
    if fixed := states[REPLACE]:
        copy, backs = (
            ("its archived copy", "backs a claim the text cites it for")
            if fixed == 1
            else ("their archived copies", "back claims the text cites them for")
        )
        parts.append(f"{fixed} can be replaced by {copy}, which {backs}")
    if other := sum(states[state] for state in _ANOTHER_SOURCE):
        parts.append(f"{other} need{'s' if other == 1 else ''} another source")
    if states[NOT_JUDGED]:
        parts.append(f"{states[NOT_JUDGED]} could not be judged")
    if unasked := states[NOT_LOOKED_UP]:
        parts.append(f"{unasked} {'was' if unasked == 1 else 'were'} not looked up")
    pages = "1 cited page is" if len(links) == 1 else f"{len(links)} cited pages are"
    return f"{pages} gone: {'; '.join(parts)}."


def replacements(links: Iterable[DeadLink]) -> dict[str, str]:
    """The address to cite in place of each dead page whose copy, or the page it moved to,
    backs it, by the dead page's canonical_url: what citations.relinked takes."""
    found: dict[str, str] = {}
    for link in links:
        if link.link is not None:
            found.setdefault(key(link), link.link)
    return found


def key(link: DeadLink) -> str:
    """*link*'s page as replacements() and citations.relinked know it."""
    return canonical_url(link.url)
