"""Text normalization shared by extraction, prompting, verification and search."""

from __future__ import annotations

import re
import unicodedata

# Characters that look like a space or a hyphen but break search and exact matching.
_SPACE_LIKE = (
    "\N{NO-BREAK SPACE}\N{FIGURE SPACE}\N{THIN SPACE}\N{HAIR SPACE}"
    "\N{NARROW NO-BREAK SPACE}\N{MEDIUM MATHEMATICAL SPACE}\N{IDEOGRAPHIC SPACE}"
)
_HYPHEN_LIKE = "\N{HYPHEN}\N{NON-BREAKING HYPHEN}"
_INVISIBLE = (
    "\N{SOFT HYPHEN}\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}"
    "\N{ZERO WIDTH JOINER}\N{WORD JOINER}\N{ZERO WIDTH NO-BREAK SPACE}"
)
# Only for matching: typographic dashes and quotes become their ASCII counterparts.
_DASHES = "\N{FIGURE DASH}\N{EN DASH}\N{EM DASH}\N{HORIZONTAL BAR}\N{MINUS SIGN}"
_SINGLE_QUOTES = (
    "\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
    "\N{SINGLE LOW-9 QUOTATION MARK}\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}\N{PRIME}"
)
_DOUBLE_QUOTES = (
    "\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}"
    "\N{DOUBLE LOW-9 QUOTATION MARK}\N{DOUBLE HIGH-REVERSED-9 QUOTATION MARK}\N{DOUBLE PRIME}"
)

# Control characters, which a terminal would run (an escape sequence can clear the screen);
# tab and line breaks stay.
_CONTROL = "".join(
    map(chr, (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0x7F, 0xA0)))
)

_CLEAN_TABLE = str.maketrans(
    {
        **dict.fromkeys(_SPACE_LIKE, " "),
        **dict.fromkeys(_HYPHEN_LIKE, "-"),
        **dict.fromkeys(_INVISIBLE),
        **dict.fromkeys(_CONTROL),
    }
)
_FOLD_TABLE = str.maketrans(
    {
        **dict.fromkeys(_DASHES, "-"),
        **dict.fromkeys(_SINGLE_QUOTES, "'"),
        **dict.fromkeys(_DOUBLE_QUOTES, '"'),
    }
)

_RUN_OF_SPACES = re.compile(r"[ \t\f\v]+")
_RUN_OF_BLANK_LINES = re.compile(r"\n{3,}")
_ANY_WHITESPACE = re.compile(r"\s+")

# Above this share of junk characters, "text" is really binary or mis-decoded bytes.
JUNK_THRESHOLD = 0.05

# Words and suffixes that scale a number: "78k stars", "$1.2M", "120 billion parameters".
MAGNITUDES = {
    "k": 10**3,
    "thousand": 10**3,
    "m": 10**6,
    "million": 10**6,
    "b": 10**9,
    "bn": 10**9,
    "billion": 10**9,
}
MAGNITUDE_PATTERN = r"(?:thousand|million|billion|bn|k|m|b)"

# What a rate is charged per. Rates are not price tags: $0.53/hr to rent a GPU is not its price.
_UNIT = r"(?P<unit>hour|hr|h|day|week|wk|month|mo|year|yr|user|seat|gb|tb)"
_UNIT_NAMES = {"hr": "hour", "h": "hour", "wk": "week", "mo": "month", "yr": "year"}
# "/hr", "per month", "a year"; after "/" or "per" also with what is counted: "/GPU-hr".
_RATE = re.compile(rf"(?:(?:/|\bper\s+)(?:[a-z]+[-/ ])?|\ba\s+){_UNIT}\b", re.IGNORECASE)
_BARE_UNIT = re.compile(rf"(?:per\s+)?{_UNIT}s?", re.IGNORECASE)


def clean(text: str) -> str:
    """Normalize text for storage and display without changing what it says."""
    text = unicodedata.normalize("NFC", text).translate(_CLEAN_TABLE)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _RUN_OF_SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _RUN_OF_BLANK_LINES.sub("\n\n", text).strip()


def fold(text: str) -> str:
    """Normalize aggressively for comparison: width, case, dashes, quotes and whitespace."""
    text = unicodedata.normalize("NFKC", text).translate(_CLEAN_TABLE).translate(_FOLD_TABLE)
    return _ANY_WHITESPACE.sub(" ", text).casefold().strip()


def junk_ratio(text: str) -> float:
    """Share of characters that only appear when binary data is decoded as text."""
    if not text:
        return 0.0
    return sum(1 for ch in text if _is_junk(ch)) / len(text)


def looks_like_junk(text: str) -> bool:
    return junk_ratio(text) > JUNK_THRESHOLD


def _is_junk(ch: str) -> bool:
    if ch in "\n\r\t":
        return False
    # Replacement character, control characters (incl. C1), private-use and surrogates.
    return ch == "\N{REPLACEMENT CHARACTER}" or unicodedata.category(ch) in {"Cc", "Co", "Cs"}


def rate_unit(text: str) -> str | None:
    """The time or quantity a price is charged per, if any ("hour", "month", "gb"). Named by
    "$0.53/hr", "$0.35/GPU-hr", "$0.53 per GPU-hour", "$120 a year", or a unit alone ("hours")."""
    match = _RATE.search(text) or _BARE_UNIT.fullmatch(text.strip())
    if match is None:
        return None
    unit = match["unit"].lower()
    return _UNIT_NAMES.get(unit, unit)


def shorten(text: str, limit: int) -> str:
    """Cut *text* to at most *limit* characters, preferring a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if not text[limit - 1].isspace():  # the cut splits a word: back off to the previous space
        space = cut.rfind(" ")
        if space > limit // 2:
            cut = cut[:space]
    return cut.rstrip() + "\N{HORIZONTAL ELLIPSIS}"
