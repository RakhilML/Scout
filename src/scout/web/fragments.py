"""Links that open a page at a quote: text fragments ("#:~:text="), which browsers scroll to and
highlight. The browser keeps the fragment to itself: the site never learns what was quoted.

A directive is chosen from the page text Scout read, so its terms are the page's own characters
(its curly quotes, its dashes, its case), whatever the model's copy of the quote looked like.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote

WHOLE = 8  # words: a quote on one line this short is its own start term
MIN_TERM, MAX_TERM = 4, 10  # words in a start or end term, grown until the page holds it once
MAX_TERM_CHARS = 120  # longer, a term is no link but a paste (text without spaces)
_WORD = re.compile(r"\S+")
_CELL = re.compile(r"[|:-]*\|[|:-]*")  # a table's cell bar, or its "|---|---|" row
_BULLETS = ("-", "*", "\N{BULLET}")
_GAP = r"(?:[\s|]|(?<=[\s|])[-:*\N{BULLET}]+(?=[\s|]))+"  # what a prefix spans: blocks, marks
# A label that extraction joined to its value on one line ("- Released: October 7, 2024"):
# on the page they are two blocks.
_FIELD = re.compile(r"(?:[-*\N{BULLET}] )?[^\s:|-][^:\n|]{0,38}:(?=[^\S\n]+\S)")

Term = list[re.Match[str]]


def directive(text: str, start: int, end: int) -> str | None:
    """The text directive ("text=[PREFIX-,]START[,END]") that finds text[start:end], a span of
    a page's *text*; None when it holds no word, or a term would be too long to link.

    Browsers match each term within one block of the page, so no term crosses a line or a
    table cell, and they take a term's first match (an end term's, after the start term's):
    each term grows until that is the span's own. When no start term is (a table row's
    "2nd floor", said in the text above too), the words before it, a prefix, tell it apart.
    """
    blocks = _blocks(text, start, end)
    if not blocks:
        return None
    head, tail = blocks[0], blocks[-1]
    whole = len(blocks) == 1 and len(head) <= WHOLE
    first = head if whole else _grown(text, (head[:size] for size in _sizes(len(head))), after=0)
    found = _span(text, first, 0)
    prefix = [] if found[0] == first[0].start() else _prefix(text, first)
    terms = [first]
    rest = [] if whole else [word for word in tail if word.start() >= first[-1].end()]
    if rest:
        after = first[-1].end() if prefix else found[1]
        terms.append(_grown(text, (rest[-size:] for size in _sizes(len(rest))), after=after))
    if any(len(" ".join(word[0] for word in term)) > MAX_TERM_CHARS for term in terms):
        return None
    spelled = ",".join(map(_encoded, terms))
    return f"text={_encoded(prefix)}-,{spelled}" if prefix else f"text={spelled}"


def link(url: str, anchor: str | None) -> str:
    """*url*, opening at the quote that *anchor* (a directive) finds; *url* itself when there is
    no anchor or it is not a web page. A directive already in *url* is replaced."""
    if anchor is None or not url.startswith(("https://", "http://")):
        return url
    page, _, fragment = url.partition("#")
    return f"{page}#{fragment.split(':~:', 1)[0]}:~:{anchor}"


def _blocks(text: str, start: int, end: int) -> list[Term]:
    """The words of text[start:end], by the block of the page each is in: a line, a table
    cell, or a field's label. A list's bullet is extraction's mark, not the page's text."""
    blocks: list[Term] = [[]]
    field = _FIELD.match(text, text.rfind("\n", 0, start) + 1)
    label = field.end() if field else -1  # where the label of the line being read ends
    for word in _WORD.finditer(text, start, end):
        if _CELL.fullmatch(word[0]):
            blocks.append([])
            continue
        line = text.rfind("\n", 0, word.start()) + 1
        if not text[line : word.start()].strip():
            blocks.append([])
            field = _FIELD.match(text, line)
            label = field.end() if field else -1
            if word[0] in _BULLETS:
                continue
        blocks[-1].append(word)
        if word.end() == label:
            blocks.append([])
    return [block for block in blocks if block]


def _sizes(words: int) -> range:
    longest = min(MAX_TERM, words)
    return range(min(MIN_TERM, longest), longest + 1)


def _grown(text: str, terms: Iterable[Term], *, after: int) -> Term:
    """The first of *terms* whose first match from *after* is where it was taken from, or the
    last: an earlier passage that reads the same shows the same words."""
    term: Term = []
    for term in terms:
        if _span(text, term, after) == (term[0].start(), term[-1].end()):
            break
    return term


def _span(text: str, term: Term, after: int) -> tuple[int, int]:
    """Where *term* first occurs in *text* from *after*, as a browser matches it: whole words,
    in any case, on one line."""
    words = r"[^\S\n]+".join(re.escape(word[0]) for word in term)
    found = re.compile(rf"(?<!\w){words}(?!\w)", re.IGNORECASE).search(text, after)
    return found.span() if found else (-1, -1)


def _prefix(text: str, term: Term) -> Term:
    """The fewest words of the page just before *term* (across lines and cells, as a browser
    reads a prefix) that, with it, occur first where *term* is; none if no such words do."""
    words = [word for word in _WORD.finditer(text, 0, term[0].start()) if not _mark(text, word)]
    start = r"[^\S\n]+".join(re.escape(word[0]) for word in term)
    for size in range(1, min(MAX_TERM, len(words)) + 1):
        before = words[-size:]
        pattern = _GAP.join(re.escape(word[0]) for word in before) + _GAP + start
        found = re.compile(rf"(?<!\w){pattern}(?!\w)", re.IGNORECASE).search(text)
        if found is not None and found.start() == before[0].start():
            return before
    return []


def _mark(text: str, word: re.Match[str]) -> bool:
    """*word* is extraction's mark, not the page's: a cell bar, or a list's bullet."""
    if _CELL.fullmatch(word[0]):
        return True
    line = text.rfind("\n", 0, word.start()) + 1
    return word[0] in _BULLETS and not text[line : word.start()].strip()


def _encoded(term: Term) -> str:
    """A term as a directive spells it: "-", "," and "&" are its syntax."""
    return quote(" ".join(word[0] for word in term), safe="").replace("-", "%2D")
