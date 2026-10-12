"""No quote, no claim: check every finding against the full text of its source.

A finding is verified when its quote occurs in a source and every number the claim states also
appears in that quote, the source's title or the source's dates. A quote may differ slightly from
the page (extraction and tokenization differ between the page and what the model saw), but never
in a number. A quote found in a different source than the one cited is re-attributed rather than
rejected. The price lines Scout adds from a page's schema.org data count as part of that source.
Where on its page a quote was found is kept, so that links can open the page at it.

A fact-check is stricter: the claim is under test, so the quote alone must state its numbers,
single digits included.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from rapidfuzz import fuzz

from scout.research.prompts import offer_line
from scout.research.results import Confidence, Finding, Flag, Source, Verdict
from scout.research.schema import ModelFinding
from scout.textutil import MAGNITUDE_PATTERN, MAGNITUDES, clean, fold
from scout.web.domains import hostname
from scout.web.fragments import directive

QUOTE_MATCH_THRESHOLD = 90  # rapidfuzz partial_ratio on folded text, 0-100
_MAX_QUOTE_CHARS = 400  # long quotes are checked by their opening, which is plenty to anchor them
MIN_QUOTE_CHARS = 12
_UNSPACED = 30  # characters: a "word" this long is text written without spaces
# The word boundary guards only the magnitude: extracted tables glue cells together
# ("Amazon$3299current", "$2073mid-range"), and those numbers must still be read.
_NUMBER = re.compile(rf"(\d[\d,]*(?:\.\d+)?)(?:\s*({MAGNITUDE_PATTERN})\b)?", re.IGNORECASE)
_VERSION = re.compile(r"\d+(?:\.\d+){2,}")  # "3.13.0", "10.0.0.1", "2024.10.07"
_CURRENCY = re.compile("[$\N{EURO SIGN}\N{POUND SIGN}\N{YEN SIGN}\N{INDIAN RUPEE SIGN}]\\s?$")
_STALE_NEWS_DAYS = 30
# A fact-check compares single digits, and pages write small numbers as words ("two moons").
_NUMBER_WORDS = ("two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
_NUMBER_WORD = re.compile(rf"\b({'|'.join(_NUMBER_WORDS)}|eleven|twelve)\b", re.IGNORECASE)


def verify(
    findings: Sequence[ModelFinding], sources: Sequence[Source], *, strict: bool = False
) -> list[Finding]:
    """*strict* is for a claim under test (a fact-check): the page's title and dates cannot vouch
    for it, and every digit counts."""
    by_index = {source.index: source for source in sources}
    folded = {source.index: fold(evidence(source)) for source in sources}
    words: dict[int, Words] = {}  # each page's, mapped once a call when a quote is on it
    return [_verify_one(finding, by_index, folded, strict, words) for finding in findings]


def evidence(source: Source) -> str:
    """Everything the model was shown for a source: its text and its published prices."""
    if not source.offers:
        return source.text
    return source.text + "\n" + "\n".join(offer_line(offer) for offer in source.offers)


def _verify_one(
    found: ModelFinding,
    sources: dict[int, Source],
    folded: dict[int, str],
    strict: bool,
    words: dict[int, Words],
) -> Finding:
    # The model may withhold trust from a fact (the page makes it doubtful), never grant it.
    doubt = f"doubtful: {clean(found.doubt)}" if found.doubt and found.doubt.strip() else None

    def result(
        source: int, verdict: Verdict, note: str | None = None, anchor: str | None = None
    ) -> Finding:
        return Finding(
            claim=clean(found.claim),
            quote=clean(found.quote),
            source=source,
            verdict=verdict,
            entity=clean(found.entity) if found.entity else None,
            attribute=clean(found.attribute) if found.attribute else None,
            value=clean(found.value) if found.value else None,
            flag=Flag.DOUBTED if doubt else None,
            note="; ".join(part for part in (note, doubt) if part) or None,
            anchor=anchor,
        )

    quote = fold(found.quote)[:_MAX_QUOTE_CHARS]
    if len(quote) < MIN_QUOTE_CHARS:
        return result(found.source, Verdict.UNVERIFIED, "quote too short to check")

    digits = 1 if strict else 2
    cited_first = sorted(sources, key=lambda index: index != found.source)
    for index in cited_first:
        if locate(folded[index], quote, min_digits=digits) is None:
            continue
        source = sources[index]
        if index not in words and not source.snippet_only:
            words[index] = Words.of(source.text)
        anchor = _anchor(source, quote, digits, words.get(index))
        dates = " ".join(d.isoformat() for d in (source.published, source.updated) if d)
        stated = quote if strict else f"{quote} {fold(source.title)} {dates}"
        missing = numbers(fold(f"{found.claim} {found.value or ''}"), min_digits=digits) - numbers(
            stated, min_digits=digits
        )
        if missing:
            listed = ", ".join(sorted(missing))
            return result(index, Verdict.UNVERIFIED, f"the quote does not contain {listed}", anchor)
        notes = [f"quote is from source {index}"] if index != found.source else []
        if source.snippet_only:
            notes.append("quoted from the search result: the page itself could not be read")
        return result(index, Verdict.VERIFIED, "; ".join(notes) or None, anchor)
    return result(found.source, Verdict.UNVERIFIED, "quote not found in the source")


def _anchor(source: Source, quote: str, min_digits: int, words: Words | None) -> str | None:
    """Where on its page the (folded) *quote* is, for links that open the page at it: never in
    a search snippet, which is not the page, nor in the price lines Scout wrote."""
    if source.snippet_only or words is None:
        return None
    span = span_of(source.text, quote, min_digits=min_digits, words=words)
    return directive(source.text, *span) if span else None


def quoted_in(text: str, quote: str) -> bool:
    """Whether *quote* occurs in *text* (both folded): exactly, or nearly with every number intact.

    A near match absorbs extraction noise (spacing, punctuation, a dropped word), never a changed
    number: "sells for $1,849" is not a near match for "sells for $1,999".
    """
    return locate(text, quote) is not None


def locate(text: str, quote: str, *, min_digits: int = 2) -> tuple[int, int] | None:
    """Where *quote* occurs in *text* (both folded), as quoted_in() finds it: (start, end)."""
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    match = fuzz.partial_ratio_alignment(quote, text, score_cutoff=QUOTE_MATCH_THRESHOLD)
    if match is None:
        return None
    start, end = _whole_words(text, match.dest_start, match.dest_end)
    if numbers(quote, min_digits=min_digits) <= numbers(text[start:end], min_digits=min_digits):
        return start, end
    return None


def span_of(
    text: str, quote: str, *, min_digits: int = 2, words: Words | None = None
) -> tuple[int, int] | None:
    """Where the folded *quote* is in *text* as written: the whole words of *text* that
    locate() matches in its words folded (*words*, mapped already). A near match that missed
    the quote's first or last word by one takes it back. In a word so long it is text without
    spaces (Chinese, Japanese), the match is cut to the character."""
    words = words or Words.of(text)
    found = locate(words.joined, quote, min_digits=min_digits)
    if found is None:
        return None
    first = bisect_right(words.starts, found[0]) - 1
    last = bisect_left(words.starts, found[1]) - 1
    said = quote.split()
    if first > 0 and words.folded[first] != said[0] == words.folded[first - 1]:
        first -= 1
    if last + 1 < len(words.folded) and words.folded[last] != said[-1] == words.folded[last + 1]:
        last += 1
    begin, end = words.spans[first][0], words.spans[last][1]
    if words.unspaced(first):
        begin += max(0, found[0] - words.starts[first])
    if words.unspaced(last):
        end = words.spans[last][0] + min(len(words.folded[last]), found[1] - words.starts[last])
    return begin, end


class Words(NamedTuple):
    """A page's words folded, so a folded quote can be placed on them and mapped back to the
    page as written: folding changes lengths ("\N{LATIN SMALL LETTER SHARP S}" is "ss")."""

    joined: str  # the folded words, joined by single spaces
    folded: tuple[str, ...]
    starts: tuple[int, ...]  # where each starts in joined
    spans: tuple[tuple[int, int], ...]  # where each is in the page's text

    @classmethod
    def of(cls, text: str) -> Words:
        folded: list[str] = []
        starts: list[int] = []
        spans: list[tuple[int, int]] = []
        at = 0
        for match in re.finditer(r"\S+", text):
            word = fold(match[0])
            if not word:  # an invisible character alone
                continue
            folded.append(word)
            starts.append(at)
            spans.append(match.span())
            at += len(word) + 1
        return cls(" ".join(folded), tuple(folded), tuple(starts), tuple(spans))

    def unspaced(self, i: int) -> bool:
        """Word *i* is text without spaces, folded to as many characters as it is written."""
        begin, end = self.spans[i]
        return end - begin == len(self.folded[i]) > _UNSPACED


def _whole_words(text: str, start: int, end: int) -> tuple[int, int]:
    """The span start:end widened to whitespace, so a number cut by the match is read whole."""
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def numbers(text: str, *, min_digits: int = 2) -> set[str]:
    """Numbers of *min_digits* or more digits, normalized so that different spellings of one
    value agree: '1,999.00' -> '1999', '1,66,269' -> '166269', '78k' and '78,000' -> '78000'.

    Research skips lone digits, which in its findings are mostly list numbering; a fact-check
    compares every digit, since "7 rings" and "5 rings" are different claims.
    """
    return set(numbers_as_written(text, min_digits=min_digits))


def _version(written: str) -> str:
    """A version (or dotted date) as one value: 3.13.0 is 3.13, while 10.0.0.1 stays itself."""
    groups = written.split(".")
    while len(groups) > 2 and not groups[-1].strip("0"):
        groups.pop()
    if len(groups) > 2:
        return ".".join(groups)
    return format(Decimal(".".join(groups)).normalize(), "f")


def numbers_as_written(text: str, *, min_digits: int = 2) -> dict[str, str]:
    """numbers(), each with how *text* first writes it. "m" and "b" are million and billion
    only after a currency sign: "330 m" is a height, "$330m" an amount. A version is one
    value: "3.13.0" is not 3.13 and 0."""
    found: dict[str, str] = {}
    for match in _VERSION.finditer(text):
        value = _version(match[0])
        if sum(ch.isdigit() for ch in value) >= min_digits:
            found.setdefault(value, match[0])
    text = _VERSION.sub(lambda match: " " * len(match[0]), text)
    for match in _NUMBER.finditer(text):
        digits, magnitude = match.groups()
        before = max(0, match.start() - 2)
        if (
            magnitude
            and magnitude.lower() in ("m", "b")
            and not _CURRENCY.search(text, before, match.start())
        ):
            magnitude = None
        try:
            value = Decimal(digits.replace(",", "").rstrip("."))
        except InvalidOperation:
            continue
        if magnitude:
            value *= MAGNITUDES[magnitude.lower()]
        normalized = format(value.normalize(), "f")
        if sum(ch.isdigit() for ch in normalized) >= min_digits:
            written = match.group(0) if magnitude else digits
            found.setdefault(normalized, written.strip().rstrip(",."))
    if min_digits == 1:
        for match in _NUMBER_WORD.finditer(text):
            word = match.group(1).lower()
            value = {"eleven": 11, "twelve": 12}.get(word) or _NUMBER_WORDS.index(word) + 2
            found.setdefault(str(value), match.group(1))
    return found


def assess(
    findings: Sequence[Finding], sources: Sequence[Source], *, kind: str, today: date
) -> Confidence:
    """Confidence from evidence, not from the model's opinion of itself."""
    if not findings:
        return Confidence("low", "no findings")
    trusted = [finding for finding in findings if finding.trusted]
    by_index = {source.index: source for source in sources}
    sites = {hostname(by_index[f.source].url) for f in trusted if f.source in by_index}
    share = len(trusted) / len(findings)
    reason = f"{len(trusted)} of {len(findings)} findings trusted across {len(sites)} site(s)"

    if len(trusted) >= 3 and len(sites) >= 2 and share >= 0.7:
        level = "high"
    elif trusted:
        level = "medium"
    else:
        level = "low"

    if kind == "news" and trusted:
        dates = [by_index[f.source].freshest_date for f in trusted if f.source in by_index]
        newest = max((d for d in dates if d is not None), default=None)
        if newest is None or (today - newest).days > _STALE_NEWS_DAYS:
            level = {"high": "medium", "medium": "low"}.get(level, level)
            age = "undated" if newest is None else f"{(today - newest).days} days old"
            reason += f"; newest verified source is {age}"
    return Confidence(level, reason)
