"""Shared test helpers (fixtures live in conftest.py)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from scout.llm.base import Completion, CompletionRequest
from scout.llm.structured import StructuredMode
from scout.research.results import (
    CHECK_KIND,
    ClaimCheck,
    Confidence,
    Finding,
    Flag,
    Plan,
    RunResult,
    Source,
    Verdict,
)
from scout.web.fetch import Document, FetchStatus
from scout.web.search import SearchHit

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class ScriptedBackend:
    """A fake model. Replies come from per-purpose queues ({"plan": [...], "extract": [...]}) or,
    for a plain list, in call order. Dicts are sent as JSON; exceptions are raised."""

    model = "scripted"

    def __init__(self, replies: dict[str, list[Any]] | Sequence[Any]) -> None:
        self._by_purpose = (
            {k: list(v) for k, v in replies.items()} if isinstance(replies, dict) else None
        )
        self._in_order = list(replies) if not isinstance(replies, dict) else []
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        queue = (
            self._by_purpose[request.purpose] if self._by_purpose is not None else self._in_order
        )
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, Completion):
            return reply
        return Completion(text=reply if isinstance(reply, str) else json.dumps(reply))

    def close(self) -> None:
        """Nothing to release."""

    def add(self, purpose: str, *replies: Any) -> None:
        """Queue more replies for requests of *purpose*."""
        if self._by_purpose is None:
            raise TypeError("this backend replies in call order; build it with a dict")
        self._by_purpose.setdefault(purpose, []).extend(replies)

    def purposes(self) -> list[str]:
        return [request.purpose for request in self.requests]


class FakeSearch:
    """Search backend answering from a {query: [(url, title, snippet), ...]} table."""

    name = "fake"

    def __init__(self, table: dict[str, list[tuple[str, str, str]]]) -> None:
        self._table = table
        self.calls: list[dict[str, Any]] = []

    def search(self, query, *, max_results, region, recency, news):
        self.calls.append({"query": query, "recency": recency, "news": news, "region": region})
        rows = self._table.get(query, [])[:max_results]
        return [
            SearchHit(url=url, title=title, snippet=snippet, rank=rank, query=query)
            for rank, (url, title, snippet) in enumerate(rows, start=1)
        ]


class FakeFetcher:
    """Serves prepared Documents (text pages get a content hash, as real ones do); URLs it does
    not know come back BLOCKED. Like the real Fetcher, it hands what it read to a *cache*."""

    def __init__(self, pages: dict[str, str | Document], *, cache: Any = None) -> None:
        self.pages = pages  # edit between runs to change the web
        self._cache = cache
        self.fetched: list[str] = []

    def fetch_many(self, urls, *, workers=4, deadline=45.0):
        self.fetched.extend(urls)
        documents = [self._document(url) for url in urls]
        if self._cache is not None:
            for doc in documents:
                self._cache.put_page(doc)
        return documents

    def close(self) -> None:
        """Nothing to release."""

    def _document(self, url: str) -> Document:
        page = self.pages.get(url)
        if isinstance(page, Document):
            return page
        if page is None:
            return Document(url=url, status=FetchStatus.BLOCKED, fetched_at=NOW, error="HTTP 403")
        return Document(
            url=url,
            status=FetchStatus.OK,
            fetched_at=NOW,
            text=page,
            title=url,
            content_hash=hashlib.sha256(page.encode()).hexdigest(),
        )


class FakeResearcher:
    """Stands in for App.researcher(): returns a prepared result, or raises a prepared error."""

    structured_mode = StructuredMode.PROMPT

    def __init__(self, outcome: RunResult | Exception) -> None:
        self.outcome = outcome
        self.checked: list[str] = []
        self.check_options: dict[str, Any] = {}

    def run(self, goal: str, **options: Any) -> RunResult:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def check(self, subject: str, **options: Any) -> RunResult:
        self.checked.append(subject)
        self.check_options = options
        return self.run(subject)


class Clock:
    """A controllable clock for code that takes a ``clock`` callable."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_pdf(text: str, *, title: str | None = None) -> bytes:
    """A minimal, valid one-page PDF whose content stream draws *text* (ASCII only)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if title is not None:
        objects.append(b"<< /Title (" + title.encode() + b") /CreationDate (D:20260301120000Z) >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"<< /Size {len(objects) + 1} /Root 1 0 R"
    if title is not None:
        trailer += f" /Info {len(objects)} 0 R"
    out += (
        b"trailer\n" + trailer.encode() + b" >>\nstartxref\n" + str(xref_at).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


# A small but complete run: one trusted finding, one outlier, one unverified quote.
SAMPLE_RESULT = RunResult(
    goal="cheapest RTX 5090 | today",
    started_at=NOW,
    finished_at=NOW + timedelta(seconds=42),
    model="openai/gpt-oss-20b",
    plan=Plan(queries=("rtx 5090 price",), kind="price", recency="month", planner="model"),
    sources=(
        Source(
            index=1,
            url="https://shop.example/5090",
            title="RTX 5090 | Shop",
            site="shop.example",
            status="ok",
            query="rtx 5090 price",
            published=date(2024, 5, 20),
            updated=date(2026, 7, 27),
        ),
        Source(
            index=2,
            url="https://blocked.example/x",
            title="Tracker",
            site="blocked.example",
            status="blocked",
            query="rtx 5090 price",
            snippet_only=True,
        ),
    ),
    answer="About $1,999.",
    findings=(
        Finding(
            claim="Shop sells it for $1,999",
            quote="Now $1,999 at Shop.",
            source=1,
            verdict=Verdict.VERIFIED,
            amount=Decimal("1999"),
            currency="USD",
        ),
        Finding(
            claim="Shop Z sells it for $800",
            quote="Only $800!",
            source=2,
            verdict=Verdict.VERIFIED,
            flag=Flag.OUTLIER,
            note="far from the typical price of 1999 USD",
        ),
        Finding(
            claim="It ships free",
            quote="Free shipping",
            source=1,
            verdict=Verdict.UNVERIFIED,
            note="quote not found in the source",
        ),
    ),
    confidence=Confidence("medium", "1 of 3 findings trusted across 1 site(s)"),
    warnings=("1 of 2 pages could not be read (1 blocked); used their search snippets",),
)

# A fact-check of two claims: one refuted (its mislabelled support set aside), one supported.
# Each finding states what its page says, never the claim under test.
RELEASED_2023 = "Python 3.13 was released on October 7, 2023."
HAS_JIT = "Python 3.13 added an experimental JIT compiler."
CHECKED_TEXT = f"{RELEASED_2023} It added an experimental JIT compiler."
CHECK_RESULT = RunResult(
    goal=f"fact-check: {CHECKED_TEXT}",
    started_at=NOW,
    finished_at=NOW + timedelta(seconds=30),
    model="openai/gpt-oss-20b",
    plan=Plan(
        queries=("python 3.13 release date", "python 3.13 jit"),
        kind=CHECK_KIND,
        recency=None,
        planner="model",
    ),
    sources=(
        Source(
            index=1,
            url="https://www.python.org/downloads/release/python-3130/",
            title="Python Release Python 3.13.0",
            site="python.org",
            status="ok",
            query="python 3.13 release date",
            published=date(2024, 10, 7),
        ),
        Source(
            index=2,
            url="https://docs.python.org/3/whatsnew/3.13.html",
            title="What's New In Python 3.13",
            site="docs.python.org",
            status="ok",
            query="python 3.13 jit",
        ),
        Source(
            index=3,
            url="https://realpython.com/python313-new-features/",
            title="Python 3.13: Cool New Features",
            site="realpython.com",
            status="ok",
            query="python 3.13 jit",
        ),
    ),
    answer="Of 2 claims: 1 supported, 1 refuted.",
    findings=(
        Finding(
            claim="Python 3.13.0 was released on October 7, 2024.",
            quote="Python 3.13.0 was released on October 7, 2024.",
            source=1,
            verdict=Verdict.VERIFIED,
        ),
        Finding(
            claim="Python 3.13 adds an experimental just-in-time compiler.",
            quote="Python 3.13 adds an experimental just-in-time (JIT) compiler.",
            source=2,
            verdict=Verdict.VERIFIED,
        ),
        Finding(
            claim="Python 3.13 ships an experimental JIT compiler.",
            quote="Python 3.13 ships an experimental JIT compiler.",
            source=3,
            verdict=Verdict.VERIFIED,
        ),
        Finding(
            claim="Python 3.13 was released on October 7, 2024.",
            quote="Python 3.13 was released on October 7, 2024.",
            source=2,
            verdict=Verdict.UNVERIFIED,
            note="the quote does not contain 2023",
        ),
    ),
    confidence=Confidence("medium", "2 of 2 claims settled, 1 by two or more sites"),
    claims=(
        ClaimCheck(
            claim=RELEASED_2023,
            excerpt=RELEASED_2023,
            query="python 3.13 release date",
            refutes=(1,),
            set_aside=(4,),
            note="The sources give October 7, 2024.",
        ),
        ClaimCheck(
            claim=HAS_JIT,
            excerpt="It added an experimental JIT compiler.",
            query="python 3.13 jit",
            supports=(2, 3),
            note="Two sources confirm it.",
        ),
    ),
    checked_text=CHECKED_TEXT,
)
