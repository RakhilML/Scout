"""What a research run produces, and how it is (de)serialized for reports and the store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from scout.web.extract import Offer

CHECK_KIND = "check"  # the plan kind of a fact-check: its findings are evidence on claims


class Verdict(StrEnum):
    VERIFIED = "verified"  # the quote was found in the cited source and supports the claim
    UNVERIFIED = "unverified"  # the quote is missing or does not contain what the claim says


class Flag(StrEnum):
    OUTLIER = "outlier"  # a value far from what other sources report
    ACCESSORY = "accessory"  # a price that belongs to an accessory, not the product itself
    DOUBTED = "doubted"  # the model saw on the page why the fact may be wrong
    STALE = "stale"  # from a page dated long before the period the goal asks about


class Ruling(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    DISPUTED = "disputed"  # verified quotes on both sides
    UNCLEAR = "unclear"  # no verified quote settles it


def ruling_of(supported: bool, refuted: bool) -> Ruling:
    """A claim's ruling from the sides its trusted evidence is on."""
    if supported and refuted:
        return Ruling.DISPUTED
    if supported:
        return Ruling.SUPPORTED
    return Ruling.REFUTED if refuted else Ruling.UNCLEAR


@dataclass(frozen=True, slots=True)
class Source:
    index: int  # the number the model sees ("Source 3")
    url: str
    title: str
    site: str
    status: str  # a FetchStatus value
    query: str
    published: date | None = None
    updated: date | None = None
    snippet_only: bool = False  # the page was unreadable; only the search snippet was used
    # What the model read. A page's full text is not saved with the run (its snapshot, found by
    # content_hash, holds it); a snippet is short and is saved.
    text: str = field(default="", repr=False)
    content_hash: str | None = None
    offers: tuple[Offer, ...] = ()
    error: str | None = None
    copy_of: int | None = None  # an archived copy, read in place of cited page [n], unreadable
    moved_from: int | None = None  # the live page that cited page [n], dead, moved to

    @property
    def freshest_date(self) -> date | None:
        dates = [d for d in (self.published, self.updated) if d is not None]
        return max(dates) if dates else None

    @property
    def reading(self) -> tuple[str, str | None, str | None]:
        """Which page was read, and which version of it (a snippet is known by its text)."""
        return (self.url, self.content_hash, self.text if self.snippet_only else None)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "index": self.index,
            "url": self.url,
            "title": self.title,
            "site": self.site,
            "status": self.status,
            "query": self.query,
            "published": self.published.isoformat() if self.published else None,
            "updated": self.updated.isoformat() if self.updated else None,
            "snippet_only": self.snippet_only,
            "content_hash": self.content_hash,
            "offers": [offer.to_dict() for offer in self.offers],
            "error": self.error,
            "copy_of": self.copy_of,
            "moved_from": self.moved_from,
        }
        if self.snippet_only:
            data["snippet"] = self.text
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        return cls(
            index=data["index"],
            url=data["url"],
            title=data["title"],
            site=data["site"],
            status=data["status"],
            query=data["query"],
            published=date.fromisoformat(data["published"]) if data.get("published") else None,
            updated=date.fromisoformat(data["updated"]) if data.get("updated") else None,
            snippet_only=data.get("snippet_only", False),
            text=data.get("snippet", ""),
            content_hash=data.get("content_hash"),
            offers=tuple(Offer.from_dict(item) for item in data.get("offers", [])),
            error=data.get("error"),
            copy_of=data.get("copy_of"),
            moved_from=data.get("moved_from"),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    claim: str
    quote: str
    source: int  # Source.index
    verdict: Verdict
    entity: str | None = None
    attribute: str | None = None
    value: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    unit: str | None = None  # a rate's unit ("hour", "month"): $0.53 per hour is not a price tag
    origin: str = "model"  # "model" or "structured-data" (schema.org offers read directly)
    availability: str | None = None  # a structured offer's schema.org availability ("InStock")
    flag: Flag | None = None
    note: str | None = None
    anchor: str | None = None  # where the quote is on its page: a text directive, for links

    @property
    def trusted(self) -> bool:
        """Verified and not flagged: safe to alert on."""
        return self.verdict is Verdict.VERIFIED and self.flag is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "quote": self.quote,
            "source": self.source,
            "verdict": self.verdict.value,
            "entity": self.entity,
            "attribute": self.attribute,
            "value": self.value,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "unit": self.unit,
            "origin": self.origin,
            "availability": self.availability,
            "flag": self.flag.value if self.flag else None,
            "note": self.note,
            "anchor": self.anchor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            claim=data["claim"],
            quote=data["quote"],
            source=data["source"],
            verdict=Verdict(data["verdict"]),
            entity=data.get("entity"),
            attribute=data.get("attribute"),
            value=data.get("value"),
            amount=Decimal(data["amount"]) if data.get("amount") is not None else None,
            currency=data.get("currency"),
            unit=data.get("unit"),
            origin=data.get("origin", "model"),
            availability=data.get("availability"),
            flag=Flag(data["flag"]) if data.get("flag") else None,
            note=data.get("note"),
            anchor=data.get("anchor"),
        )


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    claim: str  # restated to stand alone
    excerpt: str  # where the checked text makes it, as written there
    query: str
    # Finding numbers (as reports number them): trusted evidence for and against the claim, and
    # the evidence Scout could not verify.
    supports: tuple[int, ...] = ()
    refutes: tuple[int, ...] = ()
    set_aside: tuple[int, ...] = ()
    note: str | None = None  # the model's reading of the sources: shown, never ruled on
    unchecked: tuple[str, ...] = ()  # numbers its passage states that no checked claim carries
    problems: tuple[str, ...] = ()  # why it may have no evidence: nothing found, not judged
    caveat: str | None = None  # why its sentence is not marked in the text
    pages: tuple[int, ...] = ()  # the sources it was judged on (Source.index), in reading order
    kept: bool = False  # carried over from the last run: its pages read the same, so not judged
    # The claim judged on archived copies of its cited pages that are gone: what they said then,
    # shown beside its label and never ruling.
    archived: ClaimCheck | None = None

    @property
    def ruling(self) -> Ruling:
        return ruling_of(bool(self.supports), bool(self.refutes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "excerpt": self.excerpt,
            "query": self.query,
            "ruling": self.ruling.value,
            "supports": list(self.supports),
            "refutes": list(self.refutes),
            "set_aside": list(self.set_aside),
            "note": self.note,
            "unchecked": list(self.unchecked),
            "problems": list(self.problems),
            "caveat": self.caveat,
            "pages": list(self.pages),
            "kept": self.kept,
            "archived": self.archived.to_dict() if self.archived is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClaimCheck:
        return cls(
            claim=data["claim"],
            excerpt=data["excerpt"],
            query=data["query"],
            supports=tuple(data.get("supports", [])),
            refutes=tuple(data.get("refutes", [])),
            set_aside=tuple(data.get("set_aside", [])),
            note=data.get("note"),
            unchecked=tuple(data.get("unchecked", [])),
            problems=tuple(data.get("problems", [])),
            caveat=data.get("caveat"),
            pages=tuple(data.get("pages", [])),
            kept=data.get("kept", False),
            archived=ClaimCheck.from_dict(data["archived"]) if data.get("archived") else None,
        )


@dataclass(frozen=True, slots=True)
class Plan:
    queries: tuple[str, ...]
    kind: str
    recency: str | None
    planner: str  # "model" or "heuristic"


@dataclass(frozen=True, slots=True)
class Confidence:
    level: str  # "high" | "medium" | "low"
    reason: str


@dataclass(frozen=True, slots=True)
class RunResult:
    goal: str
    started_at: datetime
    finished_at: datetime
    model: str
    plan: Plan
    sources: tuple[Source, ...]
    answer: str
    findings: tuple[Finding, ...]
    confidence: Confidence
    warnings: tuple[str, ...] = ()
    carried_over: bool = False  # no page changed since the previous run; its analysis was kept
    rounds: int = 1  # search rounds: more than one in deep research
    claims: tuple[ClaimCheck, ...] = ()  # a fact-check's claims, ruled on from the findings
    checked_text: str = ""  # a fact-check's text as checked (cut to fit, a page's title first)
    cited: bool = False  # a cite-check: each claim judged only on the pages its sentence cites
    audit: bool = False  # a cite-check of every cited sentence, not of the first few claims
    skipped: tuple[str, ...] = ()  # an audit's cited sentences in which no claim was checked
    unread: tuple[str, ...] = ()  # an audit's cited sentences after it stopped at its limit
    cites: dict[int, str] = field(default_factory=dict)  # a cite-check's [n]: the address it cites

    @property
    def trusted(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.trusted)

    @property
    def numbered(self) -> tuple[Finding, ...]:
        """Findings in the order reports number them (from 1): the trusted ones, then the rest."""
        return self.trusted + tuple(finding for finding in self.findings if not finding.trusted)

    def source(self, index: int) -> Source | None:
        return next((s for s in self.sources if s.index == index), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "model": self.model,
            "plan": {
                "queries": list(self.plan.queries),
                "kind": self.plan.kind,
                "recency": self.plan.recency,
                "planner": self.plan.planner,
            },
            "answer": self.answer,
            "confidence": {"level": self.confidence.level, "reason": self.confidence.reason},
            "findings": [finding.to_dict() for finding in self.findings],
            "sources": [source.to_dict() for source in self.sources],
            "warnings": list(self.warnings),
            "carried_over": self.carried_over,
            "rounds": self.rounds,
            "claims": [claim.to_dict() for claim in self.claims],
            "checked_text": self.checked_text,
            "cited": self.cited,
            "audit": self.audit,
            "skipped": list(self.skipped),
            "unread": list(self.unread),
            "cites": {str(n): url for n, url in self.cites.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunResult:
        plan = data["plan"]
        return cls(
            goal=data["goal"],
            started_at=datetime.fromisoformat(data["started_at"]),
            finished_at=datetime.fromisoformat(data["finished_at"]),
            model=data["model"],
            plan=Plan(
                queries=tuple(plan["queries"]),
                kind=plan["kind"],
                recency=plan.get("recency"),
                planner=plan["planner"],
            ),
            sources=tuple(Source.from_dict(item) for item in data["sources"]),
            answer=data["answer"],
            findings=tuple(Finding.from_dict(item) for item in data["findings"]),
            confidence=Confidence(**data["confidence"]),
            warnings=tuple(data.get("warnings", [])),
            carried_over=data.get("carried_over", False),
            rounds=data.get("rounds", 1),
            claims=tuple(ClaimCheck.from_dict(item) for item in data.get("claims", [])),
            checked_text=data.get("checked_text", ""),
            cited=data.get("cited", False),
            audit=data.get("audit", False),
            skipped=tuple(data.get("skipped", [])),
            unread=tuple(data.get("unread", [])),
            cites={int(n): url for n, url in data.get("cites", {}).items()},
        )
