"""The pages a text cites, and where it cites them.

AI answers, docs and papers cite in many styles: Markdown links, footnotes, bare addresses, a
numbered list of sources whose numbers the text uses as [n]. A cite-check needs one: each
citation becomes a marker [n] in the text, and each n leads to a web address.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import NamedTuple

from scout.textutil import clean
from scout.web.domains import canonical_url, hostname, registrable_domain

NONE_CITED = (
    "the text cites no web page: give its sources as Markdown links, bare addresses, "
    "or a numbered list of sources whose numbers the text uses as [n]"
)
NONE_LINKED = (
    "the page cites no page of another site (links to its own site are not citations): "
    "scout factcheck URL checks its claims against independent pages"
)
_BULLET = r"[ \t]*(?:[-*+\N{BULLET}][ \t]+)?"
_DEFINED = re.compile(rf"{_BULLET}\[(?P<label>[^\]\n]+)\]:(?P<rest>.*)")
_BRACKETED = re.compile(rf"{_BULLET}\[(?P<label>\d+)\](?P<rest>(?![(\[:]).*)")
_LISTED = re.compile(rf"{_BULLET}(?P<label>\d+)[.)](?P<rest>[ \t].*)")
_HEADING = re.compile(
    r"[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*|__)?"
    r"(?:sources|references|citations|notes|links|footnotes|works cited|bibliography)"
    r"[ \t]*:?[ \t]*(?:\*\*|__)?:?[ \t]*",
    re.IGNORECASE,
)
_FENCE = re.compile(r"[ \t]*(```|~~~)")
_CONTINUED = re.compile(r"[ \t]+\S")
# What "[label]: target" may link to without a web address: "/faq.html", "#notes", "faq.html".
_LINK_TARGET = re.compile(
    r"(?:<[^>\s]*>|[a-z][a-z0-9+.-]*:\S+|\S*[/#]\S*|[\w-]+(?:\.[\w-]+)+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?",
    re.IGNORECASE,
)
_URL = r"https?://[^\s<>\[\]\"'`]+"
_CODE = r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`"
_NUMBERS = re.compile(r"\[\^?(\d+)\]")
_RUNS = re.compile(
    rf"(?P<code>{_CODE})"
    r"|\[(?P<run>\d{1,3}(?:[ \t]*[,\-\N{EN DASH}][ \t]*\d{1,3})+)\](?!\()"
)
_IMAGE_LINK = re.compile(r"\[!\[([^\[\]\n]*)\]\([^()\n]*\)\]\(")
_CITATION = re.compile(
    rf"(?P<code>{_CODE})"
    r"|(?P<image>!)?\[(?P<anchor>[^\[\]\n]*)\]"
    r"\((?P<target><[^>\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?[ \t]*\)"
    r"|\[\^(?P<note>[^\[\]\s]+)\]"
    r"|\[(?P<text>[^\[\]\n]+)\](?:\[(?P<label>[^\[\]\n]*)\])?"
    r"|<(?P<auto>https?://[^>\s]+)>"
    rf"|(?P<bare>{_URL})",
    re.IGNORECASE,
)
_CLOSERS = "\"')\N{RIGHT SINGLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}"
# Where a link whose text is a number stands for a marker: attached to the word or punctuation
# before it ("2024.[1](url)"), after a sentence's end, or after another marker.
_MARKER_PLACE = re.compile(
    r"(?:[^\s(\[\"'\N{LEFT SINGLE QUOTATION MARK}\N{LEFT DOUBLE QUOTATION MARK}]"
    rf"|[.!?][{_CLOSERS}]*[ \t]+"
    r"|\[\d+\](?:\([^()\s]*\))?[ \t]+)\Z"
)
_MARK = re.compile(r"\[\d+\]")
_ONLY_MARKS = re.compile(r"\(\s*(\[\d+\](?:[\s,;]*\[\d+\])*)\s*\)")
# Markers after a sentence's end belong to it, but not after a title, which ends none.
_MARKS_AFTER_END = re.compile(
    r"(?<!\b(?:Dr|Mr|Ms|St|No|vs))(?<!\bMrs)(?<![.!?])"  # from a run's first mark: one pass
    rf"(?P<end>[.!?]++[{_CLOSERS}]*+)"
    r"[ \t]*(?P<marks>\[\d+\](?:[ \t]*\[\d+\])*)"
)
_INITIALS = re.compile(r"\b[A-Za-z]\.[A-Za-z]\Z")


class Cited(NamedTuple):
    text: str  # every citation a marker [n], placed before the end of its sentence
    pages: dict[int, str]  # the web address of each n
    warnings: tuple[str, ...] = ()


def cited(text: str, *, own: Collection[str] = ()) -> Cited:
    """*text* with each citation as a marker [n] and its lists of sources removed, and the web
    address each n cites.

    A number the text gives (its [3], a footnote [^3], a listed source "3.") is kept, and
    "[1, 2]" or "[1-3]" are the markers they stand for. Any other citation takes the lowest
    number still free, in order of appearance, and an address cited before keeps its number.
    A marker whose number names no address stays, citing no page. Markers move before the
    punctuation that ends their sentence, so that the text cuts into sentences each holding
    its own citations. Code, and addresses on the sites *own* (a checked page's own site, which
    is no evidence for it), are words of the text.
    """
    body, defined, warnings = _sources_removed(_RUNS.sub(_spelled_out, text))
    body = _IMAGE_LINK.sub(r"[\1](", body)

    def citable(url: str) -> bool:
        return _web(url) and registrable_domain(hostname(url)) not in own

    pages = {
        int(label.lstrip("^")): url
        for label, url in defined.items()
        if label.lstrip("^").isdigit() and url and citable(url)
    }
    used = {int(n) for n in _NUMBERS.findall(re.sub(_CODE, " ", body))} | set(pages)
    by_address: dict[str, int] = {}
    for n, url in sorted(pages.items()):
        by_address.setdefault(canonical_url(url), n)

    def number(url: str, given: str = "") -> int:
        key = canonical_url(url)
        if given.isdigit():
            n = int(given)
            if n not in pages:
                pages[n] = url
                used.add(n)
            if canonical_url(pages[n]) == key:
                by_address.setdefault(key, n)
                return n
        if key not in by_address:
            by_address[key] = min(set(range(1, len(used) + 2)) - used)
            used.add(by_address[key])
            pages[by_address[key]] = url
        return by_address[key]

    def cite(anchor: str, url: str | None, given: str = "") -> str:
        """*anchor* citing *url*, as the number *given* ("[2](url)", "[docs][2]") if it can."""
        if url is None or not citable(url):
            return anchor
        n = number(url, given)
        return f"[{n}]" if given == anchor or _names_page(anchor, url) else f"{anchor} [{n}]"

    def referenced(match: re.Match[str]) -> str:
        """Markers "[3]" or "[3][4]", a reference "[docs][2]" or "[docs]", or "[https://...]"."""
        text, label = match["text"], match["label"]
        if text.isdigit() and (label is None or label.isdigit()):
            return match[0] if label is None or len(text) <= 3 else f"{text} [{label}]"
        if label and label.isdigit() and label not in defined:
            return f"{text} [{label}]"
        if label is None and re.fullmatch(_URL, text, re.IGNORECASE):
            return f"[{number(_trimmed(text))}]" if citable(_trimmed(text)) else match[0]
        key = _label(label or text)
        return cite(text, defined[key], label or "") if key in defined else match[0]

    def marked(match: re.Match[str]) -> str:
        if match["code"] is not None:
            return match[0]
        if match["target"] is not None:
            anchor = match["anchor"]
            if match["image"]:
                return anchor
            numbered = anchor.isdigit() and len(anchor) <= 3 and _marker_place(match)
            return cite(anchor, match["target"].strip("<>"), anchor if numbered else "")
        if match["note"] is not None:
            url = defined.get(f"^{_label(match['note'])}")
            return f"[{number(url, match['note'])}]" if url and citable(url) else match[0]
        if match["text"] is not None:
            return referenced(match)
        if match["auto"]:
            return f"[{number(match['auto'])}]" if citable(match["auto"]) else match["auto"]
        url = _trimmed(match["bare"])
        return f"[{number(url)}]{match['bare'][len(url) :]}" if citable(url) else match[0]

    body = _ONLY_MARKS.sub(lambda m: "".join(_MARK.findall(m[1])), _CITATION.sub(marked, body))
    body = _MARKS_AFTER_END.sub(_moved, body)
    return Cited(clean(body), dict(sorted(pages.items())), tuple(warnings))


class Relinked(NamedTuple):
    text: str
    replaced: frozenset[str]  # the keys of the links that replaced an address in it


def relinked(text: str, links: Mapping[str, str]) -> Relinked:
    """*text* with each web address it cites whose canonical_url is a key of *links* replaced by
    that key's value, wherever and however the text spells it (a trailing slash, a utm_ query,
    a soft hyphen that cited() cleaned away): a reference-style citation is one address for
    every sentence that cites it. Addresses in code or images are no citations, and every
    other character stays as it was written. Says which keys it replaced."""
    replaced: set[str] = set()

    def relink(match: re.Match[str]) -> str:
        if match["code"] is not None or match["image"]:
            return match[0]
        bracketed = match["text"]
        if match["target"] is not None:
            group, url = "target", match["target"].strip("<>")
        elif bracketed and match["label"] is None and re.fullmatch(_URL, bracketed, re.IGNORECASE):
            group, url = "text", _trimmed(bracketed)
        elif match["auto"] is not None:
            group, url = "auto", match["auto"]
        elif match["bare"] is not None:
            group, url = "bare", _trimmed(match["bare"])
        else:
            return match[0]
        key = canonical_url(clean(url))
        if key not in links:
            return match[0]
        replaced.add(key)
        at = match.start(group) - match.start() + match[group].index(url)
        return f"{match[0][:at]}{links[key]}{match[0][at + len(url) :]}"

    text = _CITATION.sub(relink, text) if links else text
    return Relinked(text, frozenset(replaced))


def _moved(match: re.Match[str]) -> str:
    """Markers after a sentence's end, moved into it: "fast.[2]" becomes "fast [2].". After
    "U.S." they are moved only when a new sentence follows: "the U.S.[5] team" is one."""
    text, start, end = match.string, match.start(), match.end()
    following = text[end : end + 40].lstrip()[:1]
    if _INITIALS.search(text, max(0, start - 3), start) and not following.isupper():
        return match[0]
    return f" {match['marks']}{match['end']}"


def _marker_place(match: re.Match[str]) -> bool:
    """A link whose text is a number cites by that number where a marker would stand
    ("2024.[1](url)"); elsewhere the number is words of the text ("has [95](url) moons")."""
    return bool(_MARKER_PLACE.search(match.string, max(0, match.start() - 300), match.start()))


def _spelled_out(match: re.Match[str]) -> str:
    """ "[1, 2]" and "[1-3]" as the markers they stand for: "[1][2]", "[1][2][3]"."""
    if match["run"] is None:
        return match[0]
    numbers: list[int] = []
    for part in match["run"].split(","):
        low, _, high = re.sub(r"\s", "", part).replace("\N{EN DASH}", "-").partition("-")
        if not low.isdigit() or not (high.isdigit() or not high):
            return match[0]
        first, last = int(low), int(high or low)
        if not 0 < first <= last <= first + 20:
            return match[0]
        numbers.extend(range(first, last + 1))
    return "".join(f"[{n}]" for n in numbers)


def _sources_removed(text: str) -> tuple[str, dict[str, str | None], list[str]]:
    """*text* without its source definitions and lists of sources (and a heading such as
    "Sources:" right above one), what each label there leads to (a web address, or None for a
    link to anything else), and a warning for each label given two addresses.

    "[1]: url", "[1] url" and footnotes define their label wherever they are. A numbered list
    ("1. url") lists sources only when the text cites its numbers as [n] and it stands under a
    heading such as "Sources", or ends the text with an address on every line: anywhere else
    it is the text's own list, links and all. A label defined both ways takes its definition.
    """
    lines = text.split("\n")
    fenced = _fenced(lines)
    defined: dict[str, str | None] = {}
    warnings: list[str] = []
    dropped: set[int] = set()

    def define(label: str, url: str | None) -> None:
        known = defined.get(label)
        if known is None:
            defined[label] = url
        elif url and canonical_url(url) != canonical_url(known):
            warnings.append(f"the text gives [{label}] two addresses: {known} is read, not {url}")

    i = 0
    while i < len(lines):
        end = i + 1
        found = None if i in fenced else _definition(lines[i])
        if found is not None:
            label, rest = found
            if label.startswith("^"):  # a footnote may go on in indented lines
                while end < len(lines) and end not in fenced and _CONTINUED.match(lines[end]):
                    rest = f"{rest} {lines[end].strip()}"
                    end += 1
            url = _first_url(rest)
            if url or (not label.startswith("^") and _LINK_TARGET.fullmatch(rest.strip())):
                define(label, url)
                dropped.update(range(i, end))
        i = end

    prose = (line for i, line in enumerate(lines) if i not in dropped and i not in fenced)
    marked = {n for line in prose for n in _NUMBERS.findall(re.sub(_CODE, " ", line))}
    for items in _lists(lines, dropped | fenced):
        listed = [_LISTED.fullmatch(lines[i]) for i in items]
        urls = [_first_url(item["rest"]) for item in listed]
        last = items[-1]
        ends = all(not line.strip() or j in dropped for j, line in enumerate(lines) if j > last)
        if (
            not any(item["label"] in marked for item in listed)
            or any(_NUMBERS.search(item["rest"]) for item in listed)
            or not (_heading_above(lines, items[0]) is not None or (ends and all(urls)))
        ):
            continue
        for i, item, url in zip(items, listed, urls, strict=True):
            if url:
                define(item["label"], url)
            dropped.add(i)

    for i in sorted(dropped):
        above = _heading_above(lines, i)
        if above is not None and above not in dropped:
            dropped.add(above)
    return "\n".join(line for i, line in enumerate(lines) if i not in dropped), defined, warnings


def _definition(line: str) -> tuple[str, str] | None:
    """The label and the rest of a line that defines one: "[docs]: url", "[^1]: note",
    "[1] url"."""
    if match := _DEFINED.fullmatch(line):
        return _label(match["label"]), match["rest"]
    if (match := _BRACKETED.fullmatch(line)) and _first_url(match["rest"]):
        return match["label"], match["rest"]
    return None


def _lists(lines: list[str], skip: set[int]) -> list[list[int]]:
    """Each numbered list in *lines*, as the indices of its items: blank lines may stand
    between items, but nothing else."""
    found: list[list[int]] = []
    items: list[int] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if i not in skip and _LISTED.fullmatch(line):
            items.append(i)
        elif items:
            found.append(items)
            items = []
    if items:
        found.append(items)
    return found


def _heading_above(lines: list[str], index: int) -> int | None:
    above = next((i for i in range(index - 1, -1, -1) if lines[i].strip()), None)
    return above if above is not None and _HEADING.fullmatch(lines[above]) else None


def _fenced(lines: list[str]) -> set[int]:
    """The lines of fenced code blocks, fences included."""
    fenced: set[int] = set()
    inside = None
    for i, line in enumerate(lines):
        fence = _FENCE.match(line)
        if inside is not None or fence:
            fenced.add(i)
        if fence and inside is None:
            inside = fence[1]
        elif fence and fence[1] == inside:
            inside = None
    return fenced


def _first_url(text: str) -> str | None:
    found = re.search(_URL, text, re.IGNORECASE)
    return _trimmed(found[0]) if found else None


def _trimmed(url: str) -> str:
    """A bare address without the punctuation that ends its sentence, or a parenthesis that
    holds it."""
    unopened = url.count(")") - url.count("(")
    end = len(url)
    while end and (url[end - 1] in ".,;:!?" or (url[end - 1] == ")" and unopened > 0)):
        unopened -= url[end - 1] == ")"
        end -= 1
    return url[:end]


def _label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _web(url: str) -> bool:
    return url.lower().startswith(("http://", "https://")) and bool(hostname(url))


def _names_page(anchor: str, url: str) -> bool:
    """*anchor* is the address itself or its site ("realpython.com"), not words of the text:
    "Node.js" linking to nodejs.org is a name, kept in the text."""
    name = anchor.strip().lower().split("://", 1)[-1].removeprefix("www.").rstrip("/")
    host = hostname(url)
    whole = url.lower().split("://", 1)[-1].removeprefix("www.").rstrip("/")
    return (
        not name
        or name in (host, registrable_domain(host))
        or (name.startswith(host) and whole.startswith(name))
    )
