"""What the model is asked to return (pydantic models double as the JSON schemas it sees)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GoalKind = Literal["price", "news", "release", "general"]
Recency = Literal["day", "week", "month", "year", "any"]


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(
        min_length=1,
        description="2 to 4 short web-search queries that together cover the goal. "
        "Use the words people search with; expand abbreviations that could be ambiguous.",
    )
    kind: GoalKind = Field(
        description="price: prices or deals; news: recent events; release: versions or "
        "changelogs; general: anything else."
    )
    recency: Recency = Field(
        description="How recent results must be to answer the goal ('any' if age does not matter)."
    )


class ModelFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(description="One specific fact that helps answer the goal, in plain words.")
    quote: str = Field(
        description="The sentence or table row from the source that states the fact, copied "
        "character for character. Never paraphrase here."
    )
    source: int = Field(ge=1, description="The number of the source the quote was copied from.")
    entity: str | None = Field(
        default=None, description="What the fact is about, e.g. 'RTX 5090' or 'Python 3.13'."
    )
    attribute: str | None = Field(
        default=None, description="Which property, e.g. 'price', 'release date', 'version'."
    )
    value: str | None = Field(
        default=None, description="The value exactly as written in the quote, e.g. '$1,999'."
    )
    doubt: str | None = Field(
        default=None,
        description="Why the page makes this fact doubtful, if it does, e.g. 'the deal list "
        "also contains other cards'. Leave empty otherwise.",
    )


class FollowUp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    done: bool = Field(description="True when the verified facts already answer the goal well.")
    gaps: str = Field(description="What is still missing, unclear or disputed, in one sentence.")
    queries: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Up to 3 new web-search queries that would fill the gaps; none when done.",
    )


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(
        description="A direct answer to the goal in 2 to 6 sentences, citing facts as [n]."
    )


class ClaimToCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(
        description="One checkable fact the text states, restated to stand alone, every number "
        "as the text gives it."
    )
    excerpt: str = Field(
        description="The sentence of the text that states it, copied character for character."
    )
    query: str = Field(
        description="A short web-search query that would find an independent source on the claim."
    )


class ClaimList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[ClaimToCheck] = Field(
        description="The most important checkable claims, most important first; empty if the "
        "text states none."
    )


class ModelEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stance: Literal["supports", "refutes"] = Field(
        description="supports: the quote states the claim; refutes: the quote states something "
        "that cannot be true if the claim is."
    )
    quote: str = Field(
        description="The sentence or table row from the source, copied character for character. "
        "Never paraphrase here."
    )
    source: int = Field(ge=1, description="The number of the source the quote was copied from.")
    says: str = Field(
        description="What the quote states, in plain words, with only the numbers the quote states."
    )


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: list[ModelEvidence] = Field(
        default_factory=list,
        description="Quotes that settle the claim, most direct first; none if no source does.",
    )
    note: str = Field(
        description="One sentence on how the sources bear on the claim, e.g. 'The sources give "
        "324 m, not 330 m.'"
    )


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(
        description="A direct answer to the goal in 1 to 4 sentences, using only the sources. "
        "Say plainly if the sources do not answer it."
    )
    findings: list[ModelFinding] = Field(
        description="The most useful facts, most important first, each backed by a quote."
    )
