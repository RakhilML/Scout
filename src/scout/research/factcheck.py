"""Fact-checking: which claims a text makes, and what quotes verified on the web say about each.

The model lists the claims and quotes pages for and against each one. Scout keeps a claim only
when the text really makes it, and rules on it from the quotes it verified, never from the
model's verdict. A confirming quote must state every number of the claim by itself, so a ruling
can be checked by eye from the quote shown. Whether a verified quote confirms or refutes stays
the model's reading: it is shown beside the quote, so people can judge it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import NamedTuple

from scout.research.results import ClaimCheck, Confidence, Finding, Ruling, Source, Verdict
from scout.research.schema import ClaimToCheck, Judgment, ModelFinding
from scout.research.verify import MIN_QUOTE_CHARS, numbers, numbers_as_written, verify
from scout.textutil import clean, fold, shorten
from scout.web.domains import hostname, registrable_domain

MAX_CLAIMS = 6
CLAIM_LIMIT = 12
MAX_EVIDENCE = 4  # quotes per claim; also stated in the prompt
NO_CLAIMS = "The text makes no claim that a web page could confirm or refute."
NOTHING_READ = "none of the pages could be read"
_MIN_SENTENCE_WORDS = 4
# A sentence ends at . ! or ? before a space, but not after an initial ("U.S.") or a title.
_SENTENCE_END = re.compile(
    r"(?<=[.!?])(?<!\b[A-Za-z]\.)(?<!\b(?:Dr|Mr|Ms|St|No|vs)\.)(?<!\bMrs\.)(?=\s)|(?<=\n)"
)
_MARKERS = re.compile(r"\[\d+\]|^\d+[.)]\s")  # citation markers, and a list item's number
_CITATIONS = re.compile(r"\[\d+\]")
_NEGATION = re.compile(r"\b(?:not|no|never|none|nobody|nothing|neither|nor|without|cannot)\b|n't\b")
_NOT_NEGATION = re.compile(
    r"\b(?:not only|not just|no longer|not until|no (?:fewer|less|more) than|no\. ?\d)"
)
_YEAR = re.compile(r"(?:1[89]|2[01])\d\d")
_LIST_ITEM = re.compile(r"(?:[-*\N{BULLET}]|\d+[.)])\s+")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_JOINED = re.compile(r"\d+(?:[-./]\d+)+")  # a date or a version: one value in several numbers
_TRAILING = ".,;:!? "


class Weighed(NamedTuple):
    claim: ClaimToCheck
    supports: list[Finding]
    refutes: list[Finding]
    note: str | None  # the model's
    problems: tuple[str, ...] = ()  # why this claim may have no evidence
    caveat: str | None = None  # why its sentence is not marked in the text


def anchored(
    listed: Sequence[ClaimToCheck], text: str, limit: int
) -> tuple[list[ClaimToCheck], list[str]]:
    """The claims *text* really makes, at most *limit*, and a warning for each one set aside.

    A claim's passage must be in the text as written, and each of the claim's numbers in that
    passage. A number may also come from before it where "it" is named: "Python 3.13", "Windows
    7". Turning the text's 2023 into 2024, a list's "4 moons" into "1 moon", or a year into one
    stated earlier is not restating.
    """
    folded = fold(text)
    kept: list[ClaimToCheck] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for item in listed:
        claim, excerpt = clean(item.claim), clean(item.excerpt)
        if not claim or fold(claim) in seen:
            continue
        why = _not_made(claim, excerpt, folded)
        if why is not None:
            warnings.append(
                f'set aside a claim the text does not make: "{shorten(claim, 80)}" ({why})'
            )
            continue
        seen.add(fold(claim))
        kept.append(ClaimToCheck(claim=claim, excerpt=excerpt, query=clean(item.query) or claim))
        if len(kept) == limit:
            break
    return kept, warnings


def _not_made(claim: str, excerpt: str, text: str) -> str | None:
    """Why the (folded) *text* does not make *claim* in *excerpt*, or None if it does."""
    if len(fold(excerpt)) < MIN_QUOTE_CHARS:
        return "its passage is too short to place"
    span = place(text, excerpt)
    if span is None:
        return "its passage is not in the text"
    folded = fold(claim)
    stated = numbers(_MARKERS.sub(" ", text[span[0] : span[1]]), min_digits=1)
    borrowed = numbers(folded, min_digits=1) - stated
    before = _CITATIONS.sub(" ", text[: span[0]])
    named = numbers(before) | {n for n in borrowed if _names(folded, n, before)}
    if borrowed - named:
        return f"its passage does not say {', '.join(sorted(borrowed - named))}"
    dropped = stated - numbers(folded, min_digits=1)
    if any(map(_YEAR.fullmatch, borrowed)) and any(map(_YEAR.fullmatch, dropped)):
        return "it trades its passage's year for an earlier one"
    return None


def _names(claim: str, number: str, before: str) -> bool:
    """*number* is part of a name the text gave before the passage ("Windows 7", "GPT-4")."""
    pattern = rf"\b[a-z][\w.]*[ -]{re.escape(number)}\b"
    return any(name in before for name in re.findall(pattern, claim))


def caveat(claim: ClaimToCheck, text: str) -> str | None:
    """Why the claim's sentence is left uncoloured: it words a negation differently from its
    passage (a model dropped a "not", or reworded "ineffective" as "not effective"), or its
    sentence negates more than the passage holds ("It is not true that vaccines cause autism"
    quoted from "vaccines cause autism")."""
    pieces, ranges, whole = _pieces(text)
    span = place(whole, claim.excerpt)
    around = " ".join(
        fold(piece)
        for piece, where in zip(pieces, ranges, strict=True)
        if span and where and where[0] < span[1] and span[0] < where[1]
    )
    negated = _negations(claim.excerpt)
    if _negations(claim.claim) != negated or _negations(around) > negated:
        return "the claim words a negation differently from the text: compare them"
    return None


def _negations(text: str) -> int:
    return len(_NEGATION.findall(_NOT_NEGATION.sub(" ", fold(text))))


def place(text: str, excerpt: str) -> tuple[int, int] | None:
    """Where *excerpt* is in the (folded) *text*, word for word: a model copying from text it
    was given verbatim has no extraction noise to excuse, and one changed word can reverse a
    claim. Only the final punctuation may differ, and it must not cut a word or a number: "grew
    3" is not in "grew 35%"."""
    passage = fold(excerpt).rstrip(_TRAILING)
    start = text.find(passage) if passage else -1
    while start >= 0:
        end = start + len(passage)
        if not (start and text[start - 1].isalnum()) and not text[end : end + 1].isalnum():
            return start, end
        start = text.find(passage, start + 1)
    return None


def sentences(text: str, limit: int) -> list[ClaimToCheck]:
    """Each sentence (or line) of *text* as a claim: what is checked when the model cannot say
    which claims the text makes."""
    parts = (part.strip().lstrip("-*\N{BULLET} ") for part in _SENTENCE_END.split(text))
    found = []
    for part in dict.fromkeys(parts):
        stated = " ".join(_CITATIONS.sub(" ", part).split())  # "[2]" is no number of the claim
        if len(stated.split()) >= _MIN_SENTENCE_WORDS:
            found.append(ClaimToCheck(claim=stated, excerpt=part, query=stated))
    return found[:limit]


def renumber(found: Sequence[Source], known: list[Source]) -> list[Source]:
    """One claim's pages numbered run-wide: a page read for an earlier claim keeps its number,
    a new one takes the next and joins *known*, the run's sources."""
    indexes = {source.reading: source.index for source in known}
    renumbered = []
    for source in found:
        index = indexes.get(source.reading)
        if index is None:
            index = indexes[source.reading] = len(known) + 1
            known.append(replace(source, index=index))
        renumbered.append(replace(source, index=index))
    return renumbered


def weigh(
    claim: str, judgment: Judgment, sources: Sequence[Source]
) -> tuple[list[Finding], list[Finding]]:
    """The model's evidence on *claim*, verified: (supporting, refuting). Each finding states
    what its page says (the model's account of the quote, or the quote), never the claim
    under test, so that memory learns only what pages state.

    A supporting quote must itself state the claim, every number included: a page giving 2024
    never confirms a claim of 2023, nor does a page merely dated 2023. A refuting quote that
    states every number of the claim confirms its numbers rather than refuting them. Every quote
    must state what the model says it does.
    """
    items = judgment.evidence[:MAX_EVIDENCE]
    stated = [clean(item.says) or clean(item.quote) for item in items]
    checked = verify(
        [
            ModelFinding(
                claim=f"{claim} {said}" if item.stance == "supports" else said,
                quote=item.quote,
                source=item.source,
            )
            for item, said in zip(items, stated, strict=True)
        ],
        sources,
        strict=True,
    )
    claimed = numbers(fold(claim), min_digits=1)
    found = [
        (item.stance, replace(finding, claim=said))
        for item, finding, said in zip(items, checked, stated, strict=True)
    ]
    found = [
        (stance, _restating(finding, claimed) if stance == "refutes" else finding)
        for stance, finding in found
    ]
    return (
        [finding for stance, finding in found if stance == "supports"],
        [finding for stance, finding in found if stance == "refutes"],
    )


def _restating(finding: Finding, claimed: set[str]) -> Finding:
    if finding.trusted and claimed and claimed <= numbers(fold(finding.quote), min_digits=1):
        note = "it states every number of the claim, so it does not refute it"
        return replace(finding, verdict=Verdict.UNVERIFIED, note=note)
    return finding


def assemble(weighed: Sequence[Weighed]) -> tuple[list[Finding], list[ClaimCheck]]:
    """The run's findings, every claim's evidence with the trusted first (as reports number
    them), and each claim with the numbers of its evidence.

    The model's note is kept only beside trusted evidence: an Unclear claim's note may restate a
    quote Scout set aside.
    """
    evidence = [finding for item in weighed for finding in (*item.supports, *item.refutes)]
    findings = sorted(evidence, key=lambda finding: not finding.trusted)
    number = {id(finding): n for n, finding in enumerate(findings, start=1)}
    unchecked = _unchecked(weighed)

    def numbered(found: Sequence[Finding], *, trusted: bool) -> tuple[int, ...]:
        return tuple(sorted(number[id(f)] for f in found if f.trusted == trusted))

    claims = [
        ClaimCheck(
            claim=item.claim.claim,
            excerpt=item.claim.excerpt,
            query=item.claim.query,
            supports=numbered(item.supports, trusted=True),
            refutes=numbered(item.refutes, trusted=True),
            set_aside=numbered([*item.supports, *item.refutes], trusted=False),
            note=item.note if any(f.trusted for f in (*item.supports, *item.refutes)) else None,
            unchecked=unchecked[fold(item.claim.excerpt)],
            problems=item.problems,
            caveat=item.caveat,
        )
        for item in weighed
    ]
    return findings, claims


def _unchecked(weighed: Sequence[Weighed]) -> dict[str, tuple[str, ...]]:
    """For each (folded) passage, the numbers it states that none of its claims carries: told,
    not hidden, when the model left part of a passage out. A date or version counts as one
    value (a claim of "October 7, 2024" covers 2024-10-07), and a time of day is not a claim."""
    carried: dict[str, set[str]] = {}
    for item in weighed:
        claimed = numbers(fold(item.claim.claim), min_digits=1)
        carried.setdefault(fold(item.claim.excerpt), set()).update(claimed)
    unchecked = {}
    for passage, claimed in carried.items():
        text = _TIME.sub(" ", _MARKERS.sub(" ", passage))
        written = numbers_as_written(text, min_digits=1)
        stated = set(written)
        for joined in _JOINED.findall(text):
            parts = numbers(joined, min_digits=1)
            if parts & claimed:
                stated -= parts
        unchecked[passage] = tuple(written[n] for n in sorted(stated - claimed))
    return unchecked


def summarize(
    claims: Sequence[ClaimCheck],
    findings: Sequence[Finding],
    sources: Sequence[Source],
    *,
    set_aside: int = 0,
) -> tuple[str, Confidence]:
    """The answer (how many claims got each ruling) and the confidence, from evidence alone:
    high only when quotes from two or more sites settle every claim. *set_aside* claims the
    model listed could not be placed in the text."""
    if not claims and set_aside:
        listed = f"{set_aside} claim{'s' if set_aside > 1 else ''}"
        answer = (
            f"Nothing was checked: the {listed} the model listed could not be placed in the text."
        )
        return answer, Confidence("low", "no claim could be placed in the text")
    if not claims:
        return NO_CLAIMS, Confidence("low", "no checkable claims")
    rulings = Counter(claim.ruling for claim in claims)
    tally = ", ".join(f"{rulings[ruling]} {ruling.value}" for ruling in Ruling if rulings[ruling])
    answer = f"Of {len(claims)} claim{'s' if len(claims) > 1 else ''}: {tally}."
    settled = [claim for claim in claims if claim.ruling in (Ruling.SUPPORTED, Ruling.REFUTED)]
    corroborated = sum(
        1
        for claim in settled
        if len(sites((*claim.supports, *claim.refutes), findings, sources)) > 1
    )
    reason = f"{len(settled)} of {len(claims)} claims settled, {corroborated} by two or more sites"
    level = "high" if corroborated == len(claims) else "medium" if settled else "low"
    return answer, Confidence(level, reason)


def sites(
    numbered: Iterable[int], findings: Sequence[Finding], sources: Sequence[Source]
) -> set[str]:
    """The sites (registrable domains) of the pages that findings *numbered* quote."""
    urls = {source.index: source.url for source in sources}
    quoted = (findings[n - 1].source for n in numbered)
    return {registrable_domain(hostname(urls[index])) for index in quoted if index in urls}


def annotate(text: str, claims: Sequence[ClaimCheck]) -> list[tuple[str, tuple[int, ...]]]:
    """*text* cut into sentences, each with the numbers (from 1) of the claims whose passage
    covers it. A claim marks text only when it surely belongs there: its passage is whole
    sentences, found word for word and only once, and the claim keeps its sense (no caveat).
    The rest are told on their cards alone. The pieces join back into *text*."""
    pieces, ranges, whole = _pieces(text)
    starts = {where[0] for where in ranges if where}
    starts |= {  # a list item's sentence starts after its bullet or number
        where[0] + item.end()
        for piece, where in zip(pieces, ranges, strict=True)
        if where and (item := _LIST_ITEM.match(fold(piece)))
    }
    ends = {
        where[0] + len(fold(piece).rstrip(_TRAILING))
        for piece, where in zip(pieces, ranges, strict=True)
        if where
    }
    spans = [_markable(whole, claim, starts, ends) for claim in claims]
    return [
        (
            piece,
            tuple(
                n
                for n, span in enumerate(spans, start=1)
                if span and where and span[0] < where[1] and where[0] < span[1]
            ),
        )
        for piece, where in zip(pieces, ranges, strict=True)
    ]


def _pieces(text: str) -> tuple[list[str], list[tuple[int, int] | None], str]:
    """*text* cut into sentences, where each one's folded form sits in the folded whole (None
    for blank pieces), and that whole: one place for annotate and caveat to look things up."""
    pieces = _SENTENCE_END.split(text)
    ranges: list[tuple[int, int] | None] = []
    folded: list[str] = []
    position = 0
    for piece in pieces:
        part = fold(piece)
        ranges.append((position, position + len(part)) if part else None)
        if part:
            folded.append(part)
            position += len(part) + 1
    return pieces, ranges, " ".join(folded)


def _markable(
    text: str, claim: ClaimCheck, starts: set[int], ends: set[int]
) -> tuple[int, int] | None:
    if claim.caveat:
        return None
    span = place(text, claim.excerpt)
    if span is None or text.count(text[span[0] : span[1]]) > 1:
        return None
    return span if span[0] in starts and span[1] in ends else None
