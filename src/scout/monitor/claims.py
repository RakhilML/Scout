"""Claim watches: a text's claims fact-checked on every run, alerting when a ruling changes
because a page did.

A claim watch remembers each claim's trusted evidence as facts in the ledger, one per quote and
page. The ruling it alerts on rests only on evidence a page proves: a quote cited when the watch
began checking the claim, a quote that appeared on a page the watch had read in full without it,
or a quote on a page that dates itself on or after the last run. A quote the model merely
noticed is kept and shown, but never rules: one already on its page, one on a page first read
now, or one in a search snippet (which is not the page).

Proven evidence leaves when its page, read in full, no longer carries it: at once if that
reading verified another quote cited now, else only on the second reading in a row without it,
since one bad fetch (a cookie wall, a page that did not render) is not a change. A page not read
again proves nothing, so its quotes keep counting. So a ruling changes, and can alert, only when a
page changed; the model reading unchanged text differently never alerts.

A citation watch audits the citations of a text or a web page. A claim is known by its words and
the addresses of the pages its sentence cites, so one fact cited to two pages is two claims, and
renumbered references change nothing. Its ruling reads as a cite-check's label. A claim whose
sentence left the text leaves quietly, and each cited page is remembered as read or not, so that
a page that cannot be read two runs in a row can be told.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta

from scout.monitor.diff import Change, Delta, Fact
from scout.research.factcheck import (
    CITED_LABELS,
    NOT_JUDGED,
    UNREADABLE,
    cited_label,
    gone,
    why_unread,
)
from scout.research.results import ClaimCheck, Ruling, RunResult, Source, ruling_of
from scout.research.verify import evidence, quoted_in
from scout.textutil import fold
from scout.web.domains import canonical_url

CLAIM = "claim|"
EVIDENCE = "evidence|"
PAGE = "page|"
READ = "read"  # a cited page's state while it can be read; otherwise why it cannot
SUPPORTS, REFUTES = "supports", "refutes"
_UNJUDGED = (UNREADABLE, NOT_JUDGED)  # a citation watch's label for a claim it never judged
DEAD_AFTER = timedelta(hours=1)  # failures closer together may be one, served from the cache


def compare(
    previous: Sequence[Fact],
    result: RunResult,
    *,
    earlier: Mapping[str, str],
    published: Mapping[str, date],
    since: datetime | None,
) -> tuple[list[Delta], list[Delta]]:
    """The run's claims and their evidence compared with what the watch remembers: (rulings,
    evidence), the rulings in the order of the claims. *earlier* holds each page the run read,
    folded, as the watch last read it in full, and *published* the date each page gives itself,
    both by canonical URL; *since* is when the watch last ran."""
    checks: dict[str, ClaimCheck] = {}
    for key, check in zip(ruling_keys(result), result.claims, strict=True):
        checks.setdefault(key, check)
    rulings = {fact.key: fact for fact in previous if is_ruling(fact)}
    remembered = {
        fact.key: fact
        for fact in previous
        if fact.key.startswith(EVIDENCE) and _ruling_of(fact.key) in checks
    }
    judged = {key for key, fact in rulings.items() if fact.value not in _UNJUDGED}
    proof = _Proof(earlier, published, since, judged if since is not None else set())
    weighed = _weigh(remembered, result, proof)
    return [
        _ruling(key, check, result, rulings.get(key), weighed) for key, check in checks.items()
    ], weighed


def left(previous: Sequence[Fact], result: RunResult) -> list[Delta]:
    """What a citation watch remembers of claims its audit no longer makes (the sentence left
    the text, or changed). Missing once, a claim is kept, marked missed: a partial read of the
    watched page, or an edit soon reverted, is no change, and back it rules as before. Missing
    twice in a row, its ruling and evidence leave. Never alerted on: an editor rewording a
    sentence is no change of a page."""
    if not result.cited:
        return []
    now = set(ruling_keys(result))
    absent = [fact for fact in previous if is_ruling(fact) and fact.key not in now]
    gone = {fact.key for fact in absent if fact.missed}
    kept = [
        Delta(Change.SAME, replace(fact, missed=True), fact) for fact in absent if not fact.missed
    ]
    return kept + [
        Delta(Change.GONE, fact)
        for fact in previous
        if fact.key in gone or (fact.key.startswith(EVIDENCE) and _ruling_of(fact.key) in gone)
    ]


def pages(previous: Sequence[Fact], result: RunResult, weighed: Iterable[Delta]) -> list[Delta]:
    """Each page a citation audit's claims cite, as read or why it could not be, compared with
    what the watch remembers. Each names the claims citing it, and what it said: a proven quote
    of it, from *weighed*.

    A page that was read is dead only when it is gone (factcheck.gone) on every run from one
    at least DEAD_AFTER ago (its fact's seen, while missed): a timeout, a server error or a
    refusal says nothing of the page, and a run soon after another may be served the same
    failure from the cache. One unreadable from the start is never dead.
    """
    if not result.cited:
        return []
    known = {fact.key: fact for fact in previous if fact.key.startswith(PAGE)}
    said: dict[str, str] = {}
    for delta in weighed:
        if delta.change is not Change.GONE and not delta.fact.noticed:
            said.setdefault(canonical_url(delta.fact.url), delta.fact.quote)
    read: dict[str, Source] = {}
    citing: dict[str, list[str]] = {}
    for check in result.claims:
        for source in filter(None, map(result.source, check.pages)):
            page = canonical_url(source.url)
            read.setdefault(page, source)
            citing.setdefault(page, []).append(check.claim)
    deltas = []
    for page, cited in citing.items():
        source, (first, *more) = read[page], dict.fromkeys(cited)
        also = f" (and {len(more)} more claim{'s' if len(more) > 1 else ''})" if more else ""
        fact = Fact(
            key=f"{PAGE}{page}",
            claim=first + also,
            quote=said.get(page, ""),
            url=source.url,
            seen=result.started_at,
            value=why_unread(source) if source.snippet_only else READ,
        )
        lost = source.snippet_only and gone(source)
        deltas.append(_page(fact, known.get(fact.key), gone=lost))
    deltas += [
        Delta(Change.GONE, old) for key, old in known.items() if key[len(PAGE) :] not in read
    ]
    return deltas


def _page(fact: Fact, old: Fact | None, *, gone: bool) -> Delta:
    """A cited page's fact: READ while it can be read, or why not once it is dead, with
    *missed* set while a read page is gone (seen: since when), and once dead (it was read
    before)."""
    if old is None:
        return Delta(Change.NEW, fact)
    if old.value == READ:
        if fact.value == READ:
            return Delta(Change.SAME, replace(fact, seen=old.seen), old)
        if not gone:  # no news of the page
            return Delta(Change.SAME, replace(old, claim=fact.claim, quote=fact.quote), old)
        if old.missed and fact.seen - old.seen >= DEAD_AFTER:
            return Delta(Change.CHANGED, replace(fact, missed=True), old)  # dead
        since = old.seen if old.missed else fact.seen
        return Delta(Change.SAME, replace(fact, value=READ, seen=since, missed=True), old)
    if fact.value == READ:  # back (old.missed: it was read before), or read at last
        return Delta(Change.CHANGED, fact, old)
    return Delta(Change.SAME, replace(fact, seen=old.seen, missed=old.missed), old)


def is_ruling(fact: Fact) -> bool:
    return fact.key.startswith(CLAIM)


def claim_key(claim: str) -> str:
    return f"{CLAIM}{_digest(claim)}"


def ruling_keys(result: RunResult) -> list[str]:
    """Each claim's ruling key, in the order of the claims. A cite-check's claim is also known
    by the addresses of the pages it was judged on: one fact cited to two pages is two claims,
    each with its own evidence."""
    if not result.cited:
        return [claim_key(check.claim) for check in result.claims]
    urls = {source.index: canonical_url(source.url) for source in result.sources}
    return [
        claim_key(" ".join([check.claim, *sorted({urls[n] for n in check.pages if n in urls})]))
        for check in result.claims
    ]


def evidence_for(ruling: str, facts: Iterable[Fact]) -> list[Fact]:
    """The evidence facts remembered for the ruling keyed *ruling*."""
    return [fact for fact in facts if fact.key.startswith(_evidence_prefix(ruling))]


def noticed_for(ruling: str, deltas: Iterable[Delta], *, standing: bool = False) -> list[Fact]:
    """The evidence on the ruling keyed *ruling* the model noticed in a run, shown but never
    ruling: what it newly noticed, or with *standing* all it ever noticed still on its page."""
    wanted = (Change.NOTICED, Change.SAME) if standing else (Change.NOTICED,)
    return [
        delta.fact
        for delta in _about(ruling, deltas)
        if delta.change in wanted and delta.fact.noticed
    ]


def departed(ruling: Delta, deltas: Iterable[Delta]) -> bool:
    """The quote a ruling is shown with left its page: it explains the change, not the ruling."""
    fact = ruling.fact
    return bool(fact.quote) and any(
        delta.change is Change.GONE and (delta.fact.quote, delta.fact.url) == (fact.quote, fact.url)
        for delta in _about(fact.key, deltas)
    )


def _about(ruling: str, deltas: Iterable[Delta]) -> list[Delta]:
    """The evidence deltas on the ruling keyed *ruling*."""
    return [delta for delta in deltas if delta.fact.key.startswith(_evidence_prefix(ruling))]


class _Proof:
    """Whether a quote cited for the first time proves something about its page."""

    def __init__(
        self,
        earlier: Mapping[str, str],
        published: Mapping[str, date],
        since: datetime | None,
        judged: set[str],
    ) -> None:
        self._earlier = earlier
        self._published = published
        self._since = since
        self._judged = judged

    def __call__(self, fact: Fact, source: Source) -> bool:
        """Yes when the claim is judged for the first time, the page lacked the quote when last
        read in full, or the page dates itself on or after the last run. A search snippet proves
        nothing about the page, nor does a date a search engine gave it."""
        if source.snippet_only:
            return False
        if self._since is None or _ruling_of(fact.key) not in self._judged:
            return True
        page = canonical_url(source.url)
        before = self._earlier.get(page)
        if before is not None:
            return not quoted_in(before, fold(fact.quote))
        dated = self._published.get(page)
        return dated is not None and dated >= self._since.date()


def _weigh(remembered: Mapping[str, Fact], result: RunResult, proof: _Proof) -> list[Delta]:
    readings: dict[str, list[str]] = {}
    for source in result.sources:
        if not source.snippet_only and source.text:
            readings.setdefault(canonical_url(source.url), []).append(fold(evidence(source)))
    deltas = []
    for fact, source in _evidence(result):
        old = remembered.get(fact.key)
        if old is not None:
            again = replace(fact, seen=old.seen, noticed=old.noticed)
            deltas.append(Delta(Change.SAME, again, old))
        elif proof(fact, source):
            deltas.append(Delta(Change.NEW, fact))
        else:
            deltas.append(Delta(Change.NOTICED, replace(fact, noticed=True)))
    cited = {delta.fact.key for delta in deltas}
    sound = {canonical_url(delta.fact.url) for delta in deltas}  # pages that verified a quote
    for old in remembered.values():
        if old.key not in cited:
            page = canonical_url(old.url)
            deltas.append(_still(old, readings.get(page, []), sound=page in sound))
    return deltas


def _still(old: Fact, readings: Sequence[str], *, sound: bool) -> Delta:
    """Remembered evidence the model did not cite: still on its page, missing once, or gone."""
    if not readings or any(quoted_in(text, fold(old.quote)) for text in readings):
        return Delta(Change.SAME, replace(old, missed=False), old)
    if old.noticed or old.missed or sound:
        return Delta(Change.GONE, old)
    return Delta(Change.SAME, replace(old, missed=True), old)


def _ruling(
    key: str, check: ClaimCheck, result: RunResult, old: Fact | None, weighed: Sequence[Delta]
) -> Delta:
    mine = _about(key, weighed)
    proven = [d.fact for d in mine if d.change is not Change.GONE and not d.fact.noticed]
    sides = {fact.value for fact in proven}
    ruling = ruling_of(SUPPORTS in sides, REFUTES in sides)
    value = _label(ruling, check, result, old)
    first = old is None or (old.value in _UNJUDGED and old.value != value)
    changed = old is not None and not first and old.value != value
    behind = _behind(ruling, mine, proven, changed=changed)
    fact = Fact(
        key=key,
        claim=check.claim,
        quote=behind.quote if behind else "",
        url=behind.url if behind else "",
        seen=result.started_at,  # when this ruling began
        # An alert reads "changed: CLAIM: supported -> refuted".
        entity=check.claim.removesuffix("."),
        value=value,
        anchor=behind.anchor if behind else None,
    )
    if old is None or first:
        return Delta(Change.NEW, fact)
    if changed:
        return Delta(Change.CHANGED, fact, old)
    kept = replace(fact, seen=old.seen)
    noticed = any(delta.change is Change.NOTICED for delta in mine)
    return Delta(Change.NOTICED if noticed else Change.SAME, kept, old)


def _label(ruling: Ruling, check: ClaimCheck, result: RunResult, old: Fact | None) -> str:
    """The ruling as the watch words it: a citation watch's as a cite-check's label. Until a
    claim's pages are first read and judged, its label says why they were not (unreadable, not
    judged); after that a run that could not judge them keeps the label the evidence gives,
    which an unread page leaves as it was."""
    if not result.cited:
        return ruling.value
    now = cited_label(check, result.sources)
    if now in _UNJUDGED and (old is None or old.value in _UNJUDGED):
        return now
    return CITED_LABELS[ruling]


def _behind(
    ruling: Ruling, mine: Sequence[Delta], proven: Sequence[Fact], *, changed: bool
) -> Fact | None:
    """The evidence a ruling is shown with. For a change, its cause: a quote that arrived (on
    the ruling's side first), else one that left its page, no longer placed on it. Otherwise a
    quote on its side."""
    side = SUPPORTS if ruling is Ruling.SUPPORTED else REFUTES
    if changed:
        arrived = sorted(
            (d.fact for d in mine if d.change is Change.NEW), key=lambda f: f.value != side
        )
        left = (
            replace(d.fact, anchor=None)
            for d in mine
            if d.change is Change.GONE and not d.fact.noticed
        )
        return next(iter(arrived), None) or next(left, None)
    if ruling is Ruling.UNCLEAR:
        return None
    return next((fact for fact in proven if fact.value == side), None)


def _evidence(result: RunResult) -> list[tuple[Fact, Source]]:
    """Each claim's trusted evidence as a fact, one per quote and page, with the reading it was
    found in; when two share a key, the first wins."""
    numbered = result.numbered
    found: dict[str, tuple[Fact, Source]] = {}
    for key, check in zip(ruling_keys(result), result.claims, strict=True):
        for stance, numbers in ((SUPPORTS, check.supports), (REFUTES, check.refutes)):
            for number in numbers:
                finding = numbered[number - 1]
                source = result.source(finding.source)
                if source is None:
                    continue
                fact = Fact(
                    key=(
                        f"{_evidence_prefix(key)}{stance}|{canonical_url(source.url)}|"
                        f"{_digest(finding.quote)}"
                    ),
                    claim=check.claim,
                    quote=finding.quote,
                    url=source.url,
                    seen=result.started_at,
                    value=stance,
                    anchor=finding.anchor,
                )
                found.setdefault(fact.key, (fact, source))
    return list(found.values())


def _evidence_prefix(ruling: str) -> str:
    return f"{EVIDENCE}{ruling.removeprefix(CLAIM)}|"


def _ruling_of(evidence_key: str) -> str:
    return f"{CLAIM}{evidence_key.removeprefix(EVIDENCE).split('|', 1)[0]}"


def _digest(text: str) -> str:
    return hashlib.blake2b(fold(text).encode(), digest_size=6).hexdigest()
