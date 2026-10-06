"""Rank text against a goal with BM25: pick relevant search hits and pack page chunks into a budget.

Instead of cutting every page at its first N characters, pages are split into paragraph-sized
chunks and the chunks that best match the goal fill the model's real context budget.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from scout.textutil import fold

# A word in any script ([^\W_] is a letter or digit), with the inner punctuation of versions and
# names kept: "3.13", "c++", "c#", "gpt-oss".
_TOKEN = re.compile(r"[^\W_](?:(?:[^\W_]|[.+#-])*(?:[^\W_]|[+#]))?")
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "now",
        "of",
        "on",
        "or",
        "right",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    ]
)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_LEAD_BONUS = 0.5  # nudges a page's opening chunk ahead of equally relevant ones
CHUNK_CHARS = 900


def tokenize(text: str) -> list[str]:
    """Lower-cased search terms in any script; stopwords and stray Latin letters (the s of
    "5090's") dropped. A single character of another script can be a word (a Chinese one)."""
    folded = fold(text)
    # The regex's \w has no combining marks (Devanagari vowel signs): match on a copy where they
    # read as letters, then cut the words out of the real text.
    shadow = folded if folded.isascii() else "".join(map(_mark_as_letter, folded))
    words = (folded[match.start() : match.end()] for match in _TOKEN.finditer(shadow))
    return [
        word
        for word in words
        if word not in _STOPWORDS and (len(word) > 1 or word.isdigit() or not word.isascii())
    ]


def _mark_as_letter(char: str) -> str:
    return "a" if unicodedata.category(char).startswith("M") else char


class BM25:
    def __init__(
        self, documents: Sequence[Sequence[str]], *, k1: float = 1.5, b: float = 0.75
    ) -> None:
        self._counts = [Counter(document) for document in documents]
        self._lengths = [len(document) for document in documents]
        self._average = (sum(self._lengths) / len(documents)) if documents else 0.0
        self._k1, self._b = k1, b
        frequency = Counter(term for counts in self._counts for term in counts)
        total = len(documents)
        self.idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in frequency.items()
        }

    def score(self, terms: Sequence[str], index: int) -> float:
        counts, length = self._counts[index], self._lengths[index]
        norm = self._k1 * (1 - self._b + self._b * length / (self._average or 1.0))
        return sum(
            self.idf[term] * counts[term] * (self._k1 + 1) / (counts[term] + norm)
            for term in set(terms)
            if counts[term]
        )


def split_chunks(text: str, *, target: int = CHUNK_CHARS) -> list[str]:
    """Paragraph-aligned pieces of about *target* characters; long paragraphs split by sentence."""
    chunks: list[str] = []
    current = ""
    for paragraph in (part.strip() for part in text.split("\n\n")):
        for piece in _pieces(paragraph, target) if paragraph else []:
            if current and len(current) + 2 + len(piece) > target:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def _pieces(paragraph: str, target: int) -> list[str]:
    if len(paragraph) <= target:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_BREAK.split(paragraph):
        if len(sentence) > target:  # tables and lists often have no sentence breaks
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(sentence[i : i + target] for i in range(0, len(sentence), target))
        elif current and len(current) + 1 + len(sentence) > target:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        pieces.append(current)
    return pieces


@dataclass(frozen=True, slots=True)
class _Chunk:
    source: int
    position: int
    text: str


def pack(
    texts: Sequence[tuple[int, str]], query: str, *, budget: int, per_source: int
) -> dict[int, list[str]]:
    """Choose the chunks of ``(source, text)`` pairs most relevant to *query*.

    Sources first get their best chunk each, most relevant source first, while *budget*
    (characters) allows; the rest goes to the best remaining chunks, at most *per_source*
    characters per source. Chunks keep their original order within a source. Sources absent
    from the result did not fit.
    """
    chunks = [
        _Chunk(source, position, piece)
        for source, text in texts
        for position, piece in enumerate(split_chunks(text))
    ]
    if not chunks:
        return {}
    ranker = BM25([tokenize(chunk.text) for chunk in chunks])
    terms = tokenize(query)
    scores = [
        ranker.score(terms, i) + (_LEAD_BONUS if chunk.position == 0 else 0.0)
        for i, chunk in enumerate(chunks)
    ]
    by_score = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)

    chosen: set[int] = set()
    used_total = 0
    used_by_source: Counter[int] = Counter()

    def take(i: int) -> None:
        nonlocal used_total
        size = len(chunks[i].text)
        source = chunks[i].source
        if used_total + size <= budget and used_by_source[source] + size <= per_source:
            chosen.add(i)
            used_total += size
            used_by_source[source] += size

    best_per_source: dict[int, int] = {}
    for i in by_score:
        best_per_source.setdefault(chunks[i].source, i)
    for i in sorted(best_per_source.values(), key=lambda i: scores[i], reverse=True):
        take(i)
    for i in by_score:
        if i not in chosen:
            take(i)

    packed: dict[int, list[str]] = {}
    for i in sorted(chosen, key=lambda i: (chunks[i].source, chunks[i].position)):
        packed.setdefault(chunks[i].source, []).append(chunks[i].text)
    return packed


def coverage(query: str, texts: Sequence[str]) -> list[float]:
    """For each text, the IDF-weighted share of the query's terms it contains (0 to 1).

    Rare query terms weigh most, so a hit about "LMS" scores low for a goal about "LLMs" even
    though both mention "open source".
    """
    terms = set(tokenize(query))
    if not terms or not texts:
        return [0.0] * len(texts)
    documents = [set(tokenize(text)) for text in texts]
    ranker = BM25([list(document) for document in documents])
    weights = {term: ranker.idf.get(term, math.log(1 + (len(texts) + 0.5) / 0.5)) for term in terms}
    total = sum(weights.values())
    return [sum(weights[t] for t in terms if t in document) / total for document in documents]
