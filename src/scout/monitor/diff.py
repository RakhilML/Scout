"""What changed between two runs of a watch, judged by the pages rather than by the model.

A fact is a trusted finding remembered for a watch. A value (a price at a shop) is identified by
what it is about, the property and the site, so a new price is a change rather than a new fact.
A statement without such a handle is matched by its wording and numbers against earlier facts
from the same site.

A model reports different things on different runs even when a page has not changed, so no
change is taken from its output alone. Each one is checked against the page text:

* NEW: the fact's evidence is new. If its quote was already on the page when the watch last read
  it, the model merely noticed it this time: NOTICED, which alerts nobody.
* CHANGED: a remembered value differs, and its old evidence has left the re-read page. If the old
  evidence is still there, the page shows both values and the other one is a fact of its own.
* SAME: the fact was reported again, or its evidence is still on the re-read page.
* GONE: the page was read again and the fact's evidence is no longer on it. A page that could not
  be fetched proves nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz

from scout.research.results import Finding, RunResult, Source
from scout.research.verify import evidence, numbers, quoted_in
from scout.textutil import fold
from scout.web import fragments
from scout.web.domains import hostname
from scout.web.extract import Offer

SAME_STATEMENT = 85  # rapidfuzz token_set_ratio at which two wordings state the same fact


class Change(StrEnum):
    NEW = "new"
    NOTICED = "noticed"  # first reported now, but it was already on the page
    CHANGED = "changed"
    SAME = "same"
    GONE = "gone"


@dataclass(frozen=True, slots=True)
class Fact:
    key: str
    claim: str
    quote: str
    url: str
    seen: datetime  # first seen, for facts from the ledger; this run's time for new ones
    entity: str | None = None
    value: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    unit: str | None = None
    structured: bool = False  # read from the page's schema.org data, not from its text
    availability: str | None = None  # of a structured offer, a schema.org term ("InStock")
    noticed: bool = False  # a claim watch's evidence no page proves new: shown, never ruled on
    missed: bool = False  # a claim watch's evidence its page lacked once: one more and it leaves
    anchor: str | None = None  # where its quote is on its page, as last read (Finding.anchor)

    @property
    def site(self) -> str:
        return hostname(self.url)

    @property
    def link(self) -> str:
        """Its page, opening at its quote."""
        return fragments.link(self.url, self.anchor)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "claim": self.claim,
            "quote": self.quote,
            "url": self.url,
            "seen": self.seen.isoformat(),
            "entity": self.entity,
            "value": self.value,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "unit": self.unit,
            "structured": self.structured,
            "availability": self.availability,
            "noticed": self.noticed,
            "missed": self.missed,
            "anchor": self.anchor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fact:
        return cls(
            key=data["key"],
            claim=data["claim"],
            quote=data["quote"],
            url=data["url"],
            seen=datetime.fromisoformat(data["seen"]),
            entity=data.get("entity"),
            value=data.get("value"),
            amount=Decimal(data["amount"]) if data.get("amount") is not None else None,
            currency=data.get("currency"),
            unit=data.get("unit"),
            structured=data.get("structured", False),
            availability=data.get("availability"),
            noticed=data.get("noticed", False),
            missed=data.get("missed", False),
            anchor=data.get("anchor"),
        )


@dataclass(frozen=True, slots=True)
class Delta:
    change: Change
    fact: Fact  # the fact as it is now (as it was, for GONE)
    previous: Fact | None = None  # the remembered fact, for CHANGED, SAME and a NOTICED ruling

    @property
    def percent(self) -> Decimal | None:
        """Relative change of a value between the two runs, in percent (same currency and unit)."""
        old, new = self.previous, self.fact
        if old is None or old.amount is None or new.amount is None or old.amount == 0:
            return None
        if (old.currency, old.unit) != (new.currency, new.unit):
            return None
        return (new.amount - old.amount) / old.amount * 100


def facts_from(result: RunResult) -> list[Fact]:
    """A run's trusted findings as facts; when two share a key, the first (most important) wins."""
    facts: dict[str, Fact] = {}
    for finding in result.findings:
        source = result.source(finding.source)
        if finding.trusted and source is not None:
            fact = _fact(finding, source, result.started_at)
            facts.setdefault(fact.key, fact)
    return list(facts.values())


def diff(
    previous: Sequence[Fact],
    result: RunResult,
    *,
    earlier: Mapping[str, Source] | None = None,
) -> list[Delta]:
    """Compare a run, as it just happened (with its pages' text), with the remembered facts.

    *earlier* holds the pages as the watch last read them, by URL. Without them nothing can be
    shown to have been there before, so every fact not remembered is NEW.
    """
    comparison = _Comparison(
        previous,
        now={s.url: _Page.of(s) for s in result.sources if _readable(s)},
        before={url: _Page.of(s) for url, s in (earlier or {}).items() if _readable(s)},
    )
    reported = [comparison.classify(fact) for fact in facts_from(result)]
    return reported + comparison.unreported()


@dataclass(frozen=True, slots=True)
class _Page:
    text: str  # folded: the page's text and its published price lines
    offers: tuple[Offer, ...]
    title: str  # what an offer without a product name is an offer of

    @classmethod
    def of(cls, source: Source) -> _Page:
        return cls(fold(evidence(source)), source.offers, source.title)

    def states(self, fact: Fact) -> bool:
        """The page carries the fact itself: its quote, or its offer at the same price."""
        if fact.structured:
            return any(
                self._of(offer, fact) and offer.price == fact.amount for offer in self.offers
            )
        return quoted_in(self.text, fold(fact.quote))

    def mentions(self, fact: Fact) -> bool:
        """The page still carries what the fact is about (an offer's product, at any price)."""
        if fact.structured:
            return any(self._of(offer, fact) for offer in self.offers)
        return self.states(fact)

    def _of(self, offer: Offer, fact: Fact) -> bool:
        return fold(offer.product or self.title) == fold(fact.entity or "")


class _Comparison:
    def __init__(
        self, previous: Sequence[Fact], *, now: dict[str, _Page], before: dict[str, _Page]
    ) -> None:
        self._previous = previous
        self._known = {fact.key: fact for fact in previous}
        self._now = now
        self._before = before
        self._matched: set[str] = set()

    def classify(self, fact: Fact) -> Delta:
        old = self._remembered(fact)
        if old is None:
            return self._appeared(fact)
        if not _changed(old, fact):
            return self._same(old, fact)
        sibling = self._known.get(_sibling_key(old.key, fact))
        if sibling is not None and not _changed(sibling, fact):
            return self._same(sibling, fact)
        page = self._now.get(old.url)
        if page is not None and not page.states(old):
            self._matched.add(old.key)
            return Delta(Change.CHANGED, _as(fact, old), old)
        # The old value is still on its page (or its page was not read): this is another value.
        return self._appeared(replace(fact, key=_sibling_key(old.key, fact)))

    def unreported(self) -> list[Delta]:
        """Remembered facts the model did not report: still on their re-read page, or gone."""
        deltas = []
        for old in self._previous:
            page = self._now.get(old.url)
            if old.key in self._matched or page is None:
                continue
            if page.states(old):
                deltas.append(Delta(Change.SAME, old, old))
            elif not page.mentions(old):
                deltas.append(Delta(Change.GONE, old))
        return deltas

    def _remembered(self, fact: Fact) -> Fact | None:
        old = self._known.get(fact.key)
        if old is None and fact.key.startswith("text|"):
            old = _same_statement(fact, self._previous, exclude=self._matched)
        return old

    def _same(self, old: Fact, fact: Fact) -> Delta:
        self._matched.add(old.key)
        return Delta(Change.SAME, _as(fact, old), old)

    def _appeared(self, fact: Fact) -> Delta:
        page = self._before.get(fact.url)
        noticed = page is not None and page.states(fact)
        return Delta(Change.NOTICED if noticed else Change.NEW, fact)


def _fact(finding: Finding, source: Source, seen: datetime) -> Fact:
    return Fact(
        key=_key(finding, source.url),
        claim=finding.claim,
        quote=finding.quote,
        url=source.url,
        seen=seen,
        entity=finding.entity,
        value=finding.value,
        amount=finding.amount,
        currency=finding.currency,
        unit=finding.unit,
        structured=finding.origin == "structured-data",
        availability=finding.availability,
        anchor=finding.anchor,
    )


def _key(finding: Finding, url: str) -> str:
    site = hostname(url)
    if finding.entity and finding.attribute and (finding.value or finding.amount is not None):
        return f"value|{fold(finding.entity)}|{fold(finding.attribute)}|{site}"
    wording = hashlib.blake2b(fold(finding.quote).encode(), digest_size=6).hexdigest()
    return f"text|{site}|{wording}"


def _sibling_key(key: str, fact: Fact) -> str:
    """The key of a second value for the same thing on the same site (the page shows both)."""
    return f"{key}|{fold(fact.value or str(fact.amount))}"


def _as(fact: Fact, old: Fact) -> Fact:
    """The fact as reported now, under the identity it is remembered by."""
    return replace(fact, key=old.key, seen=old.seen)


def _same_statement(fact: Fact, previous: Sequence[Fact], *, exclude: set[str]) -> Fact | None:
    """The remembered statement this one rewords: same site, same numbers, similar words."""
    wording = fold(f"{fact.claim} {fact.quote}")
    figures = numbers(wording)
    best, best_score = None, 0.0
    for old in previous:
        if not old.key.startswith("text|") or old.site != fact.site or old.key in exclude:
            continue
        old_wording = fold(f"{old.claim} {old.quote}")
        if numbers(old_wording) != figures:
            continue
        score = fuzz.token_set_ratio(wording, old_wording)
        if score > best_score:
            best, best_score = old, score
    return best if best_score >= SAME_STATEMENT else None


def _changed(old: Fact, new: Fact) -> bool:
    if old.amount is not None and new.amount is not None:
        return (old.amount, old.currency, old.unit) != (new.amount, new.currency, new.unit)
    return fold(old.value or "") != fold(new.value or "")


def _readable(source: Source) -> bool:
    return not source.snippet_only and bool(source.text or source.offers)
