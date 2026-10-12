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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from functools import lru_cache
from typing import NamedTuple

from scout.research.results import (
    ClaimCheck,
    Confidence,
    Finding,
    Ruling,
    RunResult,
    Source,
    Verdict,
)
from scout.research.schema import ClaimToCheck, Judgment, ModelFinding
from scout.research.verify import MIN_QUOTE_CHARS, numbers, numbers_as_written, verify
from scout.textutil import clean, fold, shorten
from scout.web.archive import in_archive
from scout.web.domains import canonical_url, hostname, registrable_domain
from scout.web.fetch import FetchStatus

MAX_CLAIMS = 6
PAGES_PER_CLAIM = 3
CLAIM_LIMIT = 12
MAX_EVIDENCE = 4  # quotes per claim; also stated in the prompt
MAX_CITED_PAGES = 5  # pages a cite-check judges one claim on
AUDIT_LIMIT = 300  # claims an audit judges, and cited sentences it lists, at most
PART_SENTENCES = 8  # cited sentences per request when an audit lists a text's claims
ARCHIVE_LIMIT = 30  # dead cited pages a run looks up in the archive, at most
NO_CLAIMS = "The text makes no claim that a web page could confirm or refute."
NOTHING_CITED = "Nothing was checked: no claim the model listed is in a sentence that cites a page."
NOTHING_READ = "none of the pages could be read"
EVERY_CITED = "scout factcheck --cited --all checks every cited sentence"
# What a cite-check's rulings say of the pages a claim cites.
CITED_LABELS = {
    Ruling.SUPPORTED: "backed",
    Ruling.REFUTED: "contradicted",
    Ruling.DISPUTED: "disputed",
    Ruling.UNCLEAR: "not found",
}
UNREADABLE = "unreadable"  # unclear, and none of the pages it cites could be read
NOT_JUDGED = "not judged"  # unclear, because the model's judgment was unusable
_MIN_SENTENCE_WORDS = 4
_CLOSERS = "\"')\N{RIGHT SINGLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}"
# A sentence ends at . ! or ? (and any closing quotes) before a space, but not after an initial
# ("U.S.") or a title.
_SENTENCE_END = re.compile(
    r"(?<=[.!?])(?<!\b[A-Za-z]\.)(?<!\b(?:Dr|Mr|Ms|St|No|vs)\.)(?<!\bMrs\.)(?=\s)"
    rf"|(?<=[.!?][{_CLOSERS}])(?=\s)|(?<=[.!?][{_CLOSERS}]{{2}})(?=\s)|(?<=\n)"
)
_MARKERS = re.compile(r"\[\d+\]|^\d+[.)]\s")  # citation markers, and a list item's number
_MARKER = re.compile(r"\[(\d+)\]")
_CITATIONS = re.compile(r"\s*\[(\d+)\]")
_CITATION_RUN = re.compile(r"(?:\s*\[\d+\])+")
_CITATIONS_AT_END = re.compile(r"(?:\s*\[\d+\])+$")
_NEGATION = re.compile(r"\b(?:not|no|never|none|nobody|nothing|neither|nor|without|cannot)\b|n't\b")
_NOT_NEGATION = re.compile(
    r"\b(?:not only|not just|no longer|not until|no (?:fewer|less|more) than|no\. ?\d)"
)
_UNRESOLVED = re.compile(r"NameResolutionError|Failed to resolve|getaddrinfo failed|not known")
# Names no public page has: an intranet's, or kept for tests and local networks.
_PRIVATE_TLDS = frozenset(
    {"local", "internal", "corp", "lan", "home", "arpa", "invalid", "test", "localhost"}
    | {"intranet", "private", "localdomain"}
)
_WORD = re.compile(r"[^\W\d_]{2,}")
_STATING_WORDS = 3  # "It opened in 1932" states something; "OCLC 1027550705" does not
_BACK_MATTER = re.compile(
    r"^(?:#{1,6}[ \t]*)?(?:notes|references|notes and references|citations|footnotes"
    r"|sources|bibliography|works cited|further reading|external links|einzelnachweise"
    r"|literatur|weblinks|notes et r(?:e|\N{LATIN SMALL LETTER E WITH ACUTE})f"
    r"(?:e|\N{LATIN SMALL LETTER E WITH ACUTE})rences|bibliographie|liens externes)"
    r"[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
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
    pages: tuple[int, ...] = ()  # the sources it was judged on, in reading order
    kept: bool = False  # carried over from an earlier check, not judged again
    archived: Weighed | None = None  # judged on archived copies of its pages that are gone


class Coverage(NamedTuple):
    """What an audit checked of a text's cited sentences."""

    skipped: tuple[str, ...] = ()  # those it read in which no claim was checked
    unchecked: int = 0  # how many it did not check, counting those it never read
    line: str = ""  # "Audit: ...", for the answer
    unread: tuple[str, ...] = ()  # those after where it stopped, never read


def web_address(subject: str) -> bool:
    """*subject* names a page to read and check, not a text."""
    return subject.startswith(("http://", "https://")) and len(subject.split()) == 1


def anchored(
    listed: Sequence[ClaimToCheck], text: str, limit: int, *, cited: bool = False
) -> tuple[list[ClaimToCheck], list[str]]:
    """The claims *text* really makes, at most *limit*, and a warning for each one set aside.

    A claim's passage must be in the text as written, and each of the claim's numbers in that
    passage. A number may also come from before it where "it" is named: "Python 3.13", "Windows
    7". Turning the text's 2023 into 2024, a list's "4 moons" into "1 moon", or a year into one
    stated earlier is not restating. A citation marker the claim carries over ("[3]") is no
    part of what it claims. In a cite-check (*cited*) one fact cited twice, to other pages, is
    two claims: each citation is checked.
    """
    folded = fold(text)
    kept: list[ClaimToCheck] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    warnings: list[str] = []
    for item in listed:
        claim, excerpt = clean(_CITATIONS.sub("", item.claim)), clean(item.excerpt)
        key = (fold(claim), cited_by(text, excerpt) if cited else ())
        if not claim or key in seen:
            continue
        why = _not_made(claim, excerpt, folded)
        if why is not None:
            warnings.append(
                f'set aside a claim the text does not make: "{shorten(claim, 80)}" ({why})'
            )
            continue
        seen.add(key)
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
    around = " ".join(map(fold, _covered(text, claim.excerpt)))
    negated = _negations(claim.excerpt)
    if _negations(claim.claim) != negated or _negations(around) > negated:
        return "the claim words a negation differently from the text: compare them"
    return None


def cited_by(text: str, excerpt: str) -> tuple[int, ...]:
    """The citation numbers ([n]) of the sentences *excerpt* covers: a claim cites what its
    sentence cites, wherever in the sentence the markers are."""
    found = (int(n) for piece in _covered(text, excerpt) for n in _CITATIONS.findall(piece))
    return tuple(dict.fromkeys(found))


def citing(
    claims: Sequence[ClaimToCheck], text: str, pages: Mapping[int, str]
) -> tuple[list[ClaimToCheck], list[str]]:
    """The claims whose sentence cites a web page in *pages*, and why the others are set
    aside: a cite-check judges a claim on the pages its own sentence cites, and nothing else."""
    kept, uncited, unknown = [], 0, {}
    for claim in claims:
        cites = cited_by(text, claim.excerpt)
        unknown.update(dict.fromkeys(n for n in cites if n not in pages))
        if any(n in pages for n in cites):
            kept.append(claim)
        else:
            uncited += 1
    warnings = []
    if unknown:
        marks = ", ".join(f"[{n}]" for n in unknown)
        them = "it" if len(unknown) == 1 else "them"
        warnings.append(f"the text cites {marks} but gives no web address for {them}")
    if uncited:
        warnings.append(
            f"set aside {uncited} claim{'s' if uncited > 1 else ''} in sentences that cite no web "
            "page: scout factcheck checks them against independent pages"
        )
    return kept, warnings


def partly_checked(
    claims: Sequence[ClaimToCheck], text: str, pages: Mapping[int, str]
) -> str | None:
    """A note when the claims checked cite only some of the pages *text* cites: a check of a
    few claims of a long article must not pass for a check of its sources."""
    checked = {n for claim in claims for n in cited_by(text, claim.excerpt) if n in pages}
    if not claims or len(checked) == len(pages):
        return None
    return (
        f"the text cites {len(pages)} pages; the claims checked cite {len(checked)} of them; "
        f"{EVERY_CITED}"
    )


def cited_sentences(text: str, pages: Mapping[int, str]) -> list[str]:
    """The sentences of *text* that cite a page in *pages*, each once (as citing() counts a
    claim's sentence): what an audit has to check."""
    found: dict[str, str] = {}
    for piece in _SENTENCE_END.split(text):
        if _cites(piece, pages):
            found.setdefault(fold(piece), piece.strip())
    return list(found.values())


def addressed(sentence: str, pages: Mapping[int, str]) -> str:
    """*sentence* as audits of a changing text compare it: folded, with each run of markers as
    the addresses they cite in *pages*, sorted. References renumbered ([3] now [4]) or markers
    reordered ([1][2] now [2][1]) leave it the same; a word edited, or a citation gained or
    lost, does not. A marker citing no address stays as written."""

    def cited(run: re.Match[str]) -> str:
        found = {int(n) for n in _MARKER.findall(run[0])}
        named = {canonical_url(pages[n]) if n in pages else f"[{n}]" for n in found}
        return " " + " ".join(sorted(named))

    return _CITATION_RUN.sub(cited, fold(sentence))


def kept_claims(
    before: RunResult, text: str, pages: Mapping[int, str]
) -> tuple[list[ClaimToCheck], set[str]]:
    """The claims of *before*, an earlier audit, that *text* (citing *pages*) still makes, each
    passage as *text* writes it now; and the sentences already known (as addressed() has them),
    which need no listing: those the kept claims cover, and those *before* passed over that are
    still there.

    A claim is kept only when every sentence its passage covered is still in *text*, word for
    word and citing the same addresses, so an edited sentence is listed again, as is one that
    gained or lost a citation. Its claims, listed afresh, are other claims."""
    now = {addressed(piece, pages) for piece in _SENTENCE_END.split(text) if piece.strip()}
    numbers: dict[str, set[int]] = {}
    for n, url in pages.items():
        numbers.setdefault(canonical_url(url), set()).add(n)
    kept: list[ClaimToCheck] = []
    known: set[str] = set()
    for check in before.claims:
        covered = _covered(before.checked_text, check.excerpt)
        keys = {addressed(piece, before.cites) for piece in covered}
        if not keys or not keys <= now:
            continue
        excerpt = _as_cited_now(check.excerpt, text, before.cites, numbers)
        if excerpt is not None:
            kept.append(ClaimToCheck(claim=check.claim, excerpt=excerpt, query=check.query))
            known |= keys
    known |= {key for key in (addressed(s, before.cites) for s in before.skipped) if key in now}
    return kept, known


def _as_cited_now(
    excerpt: str, text: str, then: Mapping[int, str], numbers: Mapping[str, Collection[int]]
) -> str | None:
    """*excerpt*, from a text whose markers cited *then*, with the markers *text* writes: each
    run citing the same addresses, under the numbers *text* gives them (*numbers*: each
    address's) and in its order. Its words are compared as place() compares them (folded, the
    final punctuation aside). None when *text* does not have it."""
    words, runs, start = [], [], 0
    for run in _CITATION_RUN.finditer(excerpt):
        cited: set[int] = set()
        for n in map(int, _MARKER.findall(run[0])):
            cited |= set(numbers.get(canonical_url(then[n]), ())) if n in then else {n}
        if not cited:
            return None
        words.append(excerpt[start : run.start()])
        runs.append(rf"((?:\s*\[(?:{'|'.join(map(str, sorted(cited)))})\])+)\s*")
        start = run.end()
    words.append(excerpt[start:])
    folded = [re.escape(fold(piece)) for piece in words]
    folded[-1] = re.escape(fold(words[-1]).rstrip(_TRAILING))
    pattern = folded[0] + "".join(run + piece for run, piece in zip(runs, folded[1:], strict=True))
    found = re.search(pattern, fold(text))
    if found is None:
        return None
    now = words[0].rstrip() + "".join(
        marks + piece for marks, piece in zip(found.groups(), words[1:], strict=True)
    )
    return now if place(fold(text), now) else None


def unknown(
    claims: Sequence[ClaimToCheck], text: str, pages: Mapping[int, str], known: Collection[str]
) -> list[ClaimToCheck]:
    """The *claims* whose passage covers a sentence of *text* that is not *known*."""
    return [
        claim
        for claim in claims
        if any(addressed(piece, pages) not in known for piece in _covered(text, claim.excerpt))
    ]


def parts(
    text: str, pages: Mapping[int, str], budget: int, *, most: int = PART_SENTENCES
) -> list[tuple[int, int, str]]:
    """*text* cut for an audit to list its claims a part at a time: each part (with where it
    starts and ends) is whole sentences, at most *budget* characters, holding at most *most*
    sentences that cite a page in *pages*, so that a model asked for every claim of a part
    can keep up. What cites nothing is left out. A cited sentence longer than *budget* is a
    part of its own: listing it may fail, but then the audit says so."""
    pieces: list[tuple[int, int, bool]] = []
    start = 0
    for piece in _SENTENCE_END.split(text):
        pieces.append((start, start + len(piece), _cites(piece, pages)))
        start += len(piece)
    found: list[tuple[int, int, str]] = []

    def close(first: int, last: int | None) -> None:
        if last is not None:
            begin, end = pieces[first][0], pieces[last][1]
            part = text[begin:end]
            begin += len(part) - len(part.lstrip())
            end -= len(part) - len(part.rstrip())
            found.append((begin, end, text[begin:end]))

    first, last, count = 0, None, 0  # the part being made, and its last cited sentence
    for i, (begin, end, cites) in enumerate(pieces):
        if end - begin > budget:
            close(first, last)
            if cites:
                close(i, i)
            first, last, count = i + 1, None, 0
            continue
        if cites and last is not None and count == most:
            close(first, last)
            first, last, count = last + 1, None, 0
        while end - pieces[first][0] > budget:
            if last is None:
                first += 1
            else:
                close(first, last)
                first, last, count = last + 1, None, 0
        if cites:
            last, count = i, count + 1
    close(first, last)
    return found


def skipped(
    claims: Sequence[ClaimToCheck], text: str, pages: Mapping[int, str], *, upto: int | None = None
) -> tuple[str, ...]:
    """The cited sentences starting before *upto* in which no claim was checked: no claim's
    passage covers them, or any copy of them the text repeats."""
    pieces, ranges, whole = _pieces(text)
    spans = [span for claim in claims if (span := place(whole, claim.excerpt))]
    covered: set[str] = set()
    missed: dict[str, str] = {}
    start = 0
    for piece, where in zip(pieces, ranges, strict=True):
        begins, start = start, start + len(piece)
        if where is None or not _cites(piece, pages):
            continue
        if any(span[0] < where[1] and where[0] < span[1] for span in spans):
            covered.add(fold(piece))
        elif upto is None or begins < upto:
            missed.setdefault(fold(piece), piece.strip())
    return tuple(sentence for key, sentence in missed.items() if key not in covered)


def coverage(
    claims: Sequence[ClaimToCheck],
    text: str,
    pages: Mapping[int, str],
    *,
    upto: int,
    stopped: str | None,
) -> Coverage:
    """What an audit of *claims* checked of the sentences of *text* citing *pages*: it read
    them up to *upto*, having *stopped* there at a limit ("its limit of 300 claims") if it
    did (claims at the limit may have been left out too)."""
    missed = skipped(claims, text, pages, upto=upto)
    read = cited_sentences(text[:upto], pages)
    known = set(map(fold, read))
    unread = tuple(s for s in cited_sentences(text, pages) if fold(s) not in known)
    line = _audit_line(len(read), len(missed), len(claims), len(unread), stopped)
    return Coverage(missed, len(missed) + len(unread), line, unread)


def _audit_line(cited: int, skipped: int, claims: int, beyond: int, stopped: str | None) -> str:
    checked = f"{cited - skipped} of {_counted(cited, 'cited sentence')}"
    as_claims = _counted(claims, "claim")
    if stopped:
        line = f"Audit: stopped at {stopped}; {checked} up to there checked, as {as_claims}"
        if beyond:
            were = "was" if beyond == 1 else "were"
            line += f"; {_counted(beyond, 'cited sentence')} after it {were} not read"
        return f"{line}."
    if not skipped:
        return f"Audit: all {_counted(cited, 'cited sentence')} checked, as {as_claims}."
    return (
        f"Audit: {checked} checked, as {as_claims}; in {skipped} no claim was checked "
        "(listed under Not checked)."
    )


def cited_label(claim: ClaimCheck, sources: Sequence[Source]) -> str:
    """What a cite-check found of the claim on the pages it cites (CITED_LABELS), or why it
    found nothing without looking: UNREADABLE when none of them could be read, NOT_JUDGED when
    the model's judgment of them was unusable."""
    if claim.ruling is not Ruling.UNCLEAR:
        return CITED_LABELS[claim.ruling]
    read = {source.index for source in sources if not source.snippet_only}
    if not read.intersection(claim.pages):
        return UNREADABLE
    if _unjudged(claim):
        return NOT_JUDGED
    return CITED_LABELS[Ruling.UNCLEAR]


def why_unread(source: Source) -> str:
    """Why a page could not be read: "not found: HTTP 404"."""
    error = (source.error or "").removeprefix("not read: ")
    return source.status.replace("_", " ") + (f": {error}" if error else "")


def gone(source: Source) -> bool:
    """*source* could not be read because the page, or its site, is gone: not found, a client
    error, a site that no longer resolves, or a page that answers but is gone (see web/soft404).
    A timeout, a server error, a refusal or a skipped site says nothing of the page."""
    if source.status in (FetchStatus.NOT_FOUND, FetchStatus.HTTP_ERROR):
        return True
    return source.status == FetchStatus.NETWORK_ERROR and bool(
        _UNRESOLVED.search(source.error or "")
    )


def archivable(source: Source) -> bool:
    """*source*, a cited page that could not be read, may be looked up in the archive: a page
    that is gone, on a public name (an intranet host that does not resolve off its network is
    never sent to the archive), and not a copy in the archive already."""
    host = hostname(source.url)
    public = "." in host and host.rsplit(".", 1)[1] not in _PRIVATE_TLDS and not host[-1].isdigit()
    return gone(source) and public and not in_archive(source.url)


def no_copy(n: int) -> str:
    """The problem of a claim citing [n], a dead page the archive has no copy of. Its problems
    are all a stored run keeps of a lookup that found nothing: deadlinks reads them back."""
    return f"no archived copy of [{n}]"


def not_looked_up(n: int, why: str) -> str:
    """The problem of a claim citing [n], a dead page not looked up in the archive (*why*)."""
    return f"no archived copy looked up for [{n}]: {why}"


def _unjudged(claim: ClaimCheck) -> bool:
    return any(problem.startswith(NOT_JUDGED) for problem in claim.problems)


def _covered(text: str, excerpt: str) -> list[str]:
    """The sentences of *text* that *excerpt*, placed in it, covers in whole or in part."""
    pieces, ranges, whole = _pieces(text)
    span = place(whole, excerpt)
    return [
        piece
        for piece, where in zip(pieces, ranges, strict=True)
        if span and where and where[0] < span[1] and span[0] < where[1]
    ]


def _cites(sentence: str, pages: Mapping[int, str]) -> bool:
    """*sentence* cites a page in *pages* and states something: "OCLC 1027550705 [17]" and an
    infobox's "Website [14]" are no sentences to check."""
    cites = any(int(n) in pages for n in _CITATIONS.findall(sentence))
    return cites and len(_WORD.findall(_CITATIONS.sub(" ", sentence))) >= _STATING_WORDS


def back_matter(text: str) -> int:
    """Where a web page's back matter starts (References, Further reading, External links and
    the like, which an audit leaves out), or the end of *text*. Only a heading past the first
    quarter of the page counts: back matter comes last."""
    found = _BACK_MATTER.search(text, len(text) // 4)
    return found.start() if found else len(text)


def _counted(count: int, thing: str) -> str:
    return f"{count} {thing}{'' if count == 1 else 's'}"


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


def sentences(text: str, limit: int | None = None) -> list[ClaimToCheck]:
    """Each sentence (or line) of *text* as a claim: what is checked when the model cannot say
    which claims the text makes."""
    parts = (part.strip().lstrip("-*\N{BULLET} ") for part in _SENTENCE_END.split(text))
    found = []
    for part in dict.fromkeys(parts):
        stated = " ".join(_CITATIONS.sub("", part).split())  # "[2]" is no number of the claim
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


def found_again(claim: str, finding: Finding, page: Source) -> Finding | None:
    """*finding*, a quote verified on a dead page's archived copy as backing *claim*, verified
    again on *page*, the live page the dead one moved to, and placed there; None unless it is
    trusted there. The quote's stance stays the model's reading of the copy: the same words
    state the same on the new page, so the model is not asked again."""
    (again,) = verify(
        [ModelFinding(claim=f"{claim} {finding.claim}", quote=finding.quote, source=page.index)],
        [page],
        strict=True,
    )
    return replace(again, claim=finding.claim) if again.trusted else None


def _restating(finding: Finding, claimed: set[str]) -> Finding:
    if finding.trusted and claimed and claimed <= numbers(fold(finding.quote), min_digits=1):
        note = "it states every number of the claim, so it does not refute it"
        return replace(finding, verdict=Verdict.UNVERIFIED, note=note)
    return finding


def assemble(weighed: Sequence[Weighed]) -> tuple[list[Finding], list[ClaimCheck]]:
    """The run's findings, every claim's evidence with the trusted first (as reports number
    them), and each claim with the numbers of its evidence.

    The model's note is kept only beside trusted evidence: an Unclear claim's note may restate a
    quote Scout set aside. The evidence a claim's archived copies gave is numbered with the rest.
    """
    levels = [level for item in weighed for level in (item, item.archived) if level is not None]
    evidence = [finding for level in levels for finding in (*level.supports, *level.refutes)]
    findings = sorted(evidence, key=lambda finding: not finding.trusted)
    number = {id(finding): n for n, finding in enumerate(findings, start=1)}
    untold = _unchecked(weighed)

    def numbered(found: Sequence[Finding], *, trusted: bool) -> tuple[int, ...]:
        return tuple(sorted(number[id(f)] for f in found if f.trusted == trusted))

    def check(item: Weighed, unchecked: tuple[str, ...] = ()) -> ClaimCheck:
        return ClaimCheck(
            claim=item.claim.claim,
            excerpt=item.claim.excerpt,
            query=item.claim.query,
            supports=numbered(item.supports, trusted=True),
            refutes=numbered(item.refutes, trusted=True),
            set_aside=numbered([*item.supports, *item.refutes], trusted=False),
            note=item.note if any(f.trusted for f in (*item.supports, *item.refutes)) else None,
            unchecked=unchecked,
            problems=item.problems,
            caveat=item.caveat,
            pages=item.pages,
            kept=item.kept,
            archived=check(item.archived) if item.archived is not None else None,
        )

    return findings, [check(item, untold[fold(item.claim.excerpt)]) for item in weighed]


def carried(claim: ClaimToCheck, before: RunResult, found: Sequence[Source]) -> Weighed | None:
    """The claim's evidence as *before* weighed it, moved onto the same pages as numbered in
    *found*; None when the claim must be judged again.

    Pages that read exactly as they did then (the same versions, in the same order) hold the
    same evidence: judging them again would add only the model's variance, and cost GPU time.
    In a cite-check, where one fact cited to two pages is two claims, the claim is the one
    judged on these pages, in any order. A claim the model could not judge is judged again.
    Set-aside evidence travels with the supports: it never rules, whatever its stance.
    """
    then = {source.index: source.reading for source in before.sources}
    now = [source.reading for source in found]

    def same(check: ClaimCheck) -> bool:
        readings = [then.get(n) for n in check.pages]
        return Counter(readings) == Counter(now) if before.cited else readings == now

    check = next(
        (
            c
            for c in before.claims
            if fold(c.claim) == fold(claim.claim) and same(c) and not _unjudged(c)
        ),
        None,
    )
    if check is None or not found:
        return None
    index = {source.reading: source.index for source in found}
    moved = {old: index[then[old]] for old in check.pages}
    evidence = before.numbered
    supports = [evidence[n - 1] for n in (*check.supports, *check.set_aside)]
    refutes = [evidence[n - 1] for n in check.refutes]
    if any(finding.source not in moved for finding in (*supports, *refutes)):
        return None
    return Weighed(
        claim,
        [replace(finding, source=moved[finding.source]) for finding in supports],
        [replace(finding, source=moved[finding.source]) for finding in refutes],
        check.note,
        check.problems,
        pages=tuple(source.index for source in found),
        kept=True,
    )


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
    cited: bool = False,
    skipped: int = 0,
) -> tuple[str, Confidence]:
    """The answer (how many claims got each ruling) and the confidence, from evidence alone:
    high only when quotes from two or more sites settle every claim. *set_aside* claims the
    model listed could not be placed in the text. A cite-check (*cited*) counts its labels,
    and a claim settled by the one page it cites is settled: that page is what was asked.
    It is not high while *skipped* cited sentences went unchecked."""
    if not claims and set_aside:
        listed = f"{set_aside} claim{'s' if set_aside > 1 else ''}"
        answer = (
            f"Nothing was checked: the {listed} the model listed could not be placed in the text."
        )
        return answer, Confidence("low", "no claim could be placed in the text")
    if not claims and cited:
        return NOTHING_CITED, Confidence("low", "no claim is in a sentence that cites a page")
    if not claims:
        return NO_CLAIMS, Confidence("low", "no checkable claims")
    if cited:
        return _cited_summary(claims, sources, skipped)
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


def _cited_summary(
    claims: Sequence[ClaimCheck], sources: Sequence[Source], skipped: int
) -> tuple[str, Confidence]:
    labels = Counter(cited_label(claim, sources) for claim in claims)
    order = (*CITED_LABELS.values(), UNREADABLE, NOT_JUDGED)
    tally = ", ".join(f"{labels[label]} {label}" for label in order if labels[label])
    answer = f"Of {len(claims)} cited claim{'s' if len(claims) > 1 else ''}: {tally}."
    copied = Counter(
        cited_label(c.archived, sources)
        for c in claims
        if c.archived is not None and cited_label(c, sources) == UNREADABLE
    )
    del copied[UNREADABLE], copied[NOT_JUDGED]  # no copy could be read, or judged
    if judged := sum(copied.values()):
        was, cite = ("was", "it cites") if judged == 1 else ("were", "they cite")
        tally = ", ".join(f"{copied[label]} {label}" for label in order if copied[label])
        answer += (
            f" {judged} of {labels[UNREADABLE]} unreadable claims {was} judged on archived copies "
            f"of the pages {cite}: {tally}."
        )
    if moved := sum(1 for source in sources if source.moved_from is not None):
        answer += (
            " 1 dead cited page lives on at a new address."
            if moved == 1
            else f" {moved} dead cited pages live on at new addresses."
        )
    settled = labels[CITED_LABELS[Ruling.SUPPORTED]] + labels[CITED_LABELS[Ruling.REFUTED]]
    reason = f"{settled} of {len(claims)} cited claims settled by the pages they cite"
    unread = sum(1 for source in sources if source.snippet_only and source.copy_of is None)
    if unread:
        reason += f"; {unread} cited page{'s' if unread > 1 else ''} could not be read"
    if labels[NOT_JUDGED]:
        reason += f"; {labels[NOT_JUDGED]} not judged: the model's reply was unusable"
    if skipped:
        reason += f"; {_counted(skipped, 'cited sentence')} not checked"
    level = "high" if settled == len(claims) and not skipped else "medium" if settled else "low"
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
    ends = {  # a passage copied without its sentence's citation markers ends it too
        where[0] + len(ended)
        for piece, where in zip(pieces, ranges, strict=True)
        if where
        for ended in {
            fold(piece).rstrip(_TRAILING),
            _CITATIONS_AT_END.sub("", fold(piece).rstrip(_TRAILING)).rstrip(_TRAILING),
        }
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


@lru_cache(maxsize=4)
def _pieces(text: str) -> tuple[tuple[str, ...], tuple[tuple[int, int] | None, ...], str]:
    """*text* cut into sentences, where each one's folded form sits in the folded whole (None
    for blank pieces), and that whole: one place for annotate and caveat to look things up.
    Kept for the texts last cut, since a check looks up every claim in its text, and an audit
    of a long text hundreds of them."""
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
    return tuple(pieces), tuple(ranges), " ".join(folded)


def _markable(
    text: str, claim: ClaimCheck, starts: set[int], ends: set[int]
) -> tuple[int, int] | None:
    if claim.caveat:
        return None
    span = place(text, claim.excerpt)
    if span is None or text.count(text[span[0] : span[1]]) > 1:
        return None
    return span if span[0] in starts and span[1] in ends else None
