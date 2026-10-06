"""Alert rules: plain words in, deterministic predicates out. No model decides whether to alert.

Grammar (case-insensitive):

    new                              a fact appeared that was not there before
    changed                          a value changed (by 1% or more) or a fact disappeared
    price below 1800 [USD]           a verified price under the limit (also: under, < )
    price above 2500 [USD]           a verified price over the limit (also: over, > )
    drop 5%                          a price fell by at least 5%   (also: falls, drops by)
    rise 10%                         a price rose by at least 10%  (also: rises, up)
    mentions "free-threaded"         a new fact contains the text
    in stock                         a published offer is in stock (also: back in stock)

The first five and "mentions" are events: each change is seen by exactly one run, so each alerts
once. Price limits and "in stock" are conditions: one alerts when it starts to hold (or when the
rule is added while it holds), stays quiet while it keeps holding, and can alert again only after
it stopped holding. A price moving around under the limit is one alert, not one per price.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from scout.errors import ConfigError
from scout.monitor.diff import Change, Delta, Fact
from scout.textutil import fold

# A value change smaller than this does not count as "changed" (prices jitter by cents).
MATERIAL_PERCENT = Decimal(1)
# schema.org availability terms under which an offer can be bought now.
AVAILABLE = frozenset({"InStock", "LimitedAvailability", "OnlineOnly", "InStoreOnly"})

RuleKind = Literal["new", "changed", "below", "above", "drop", "rise", "mentions", "in_stock"]
CONDITIONS: frozenset[RuleKind] = frozenset({"below", "above", "in_stock"})

_NUMBER = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
_CURRENCY = r"(?:\s*([A-Za-z]{3}))?"


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_PATTERNS: tuple[tuple[RuleKind, re.Pattern[str]], ...] = (
    ("new", _words(r"^new(?: findings?| facts?)?$")),
    ("changed", _words(r"^(?:changed|any change|changes?)$")),
    ("below", _words(rf"^(?:price )?(?:below|under|<)\s*\$?{_NUMBER}{_CURRENCY}$")),
    ("above", _words(rf"^(?:price )?(?:above|over|>)\s*\$?{_NUMBER}{_CURRENCY}$")),
    ("drop", _words(rf"^(?:price )?(?:drops?|falls?)(?: by)?\s*{_NUMBER}\s*%$")),
    ("rise", _words(rf"^(?:price )?(?:rises?|up)(?: by)?\s*{_NUMBER}\s*%$")),
    ("mentions", _words(r"^mentions\s+[\"']?(.+?)[\"']?$")),
    ("in_stock", _words(r"^(?:back )?in stock$")),
)


@dataclass(frozen=True, slots=True)
class Rule:
    kind: RuleKind
    text: str  # as the user wrote it
    amount: Decimal | None = None  # below / above
    currency: str | None = None
    percent: Decimal | None = None  # drop / rise
    phrase: str | None = None  # mentions

    @property
    def is_condition(self) -> bool:
        return self.kind in CONDITIONS

    @property
    def key(self) -> str:
        """The rule's identity: its words, whatever the spacing and case."""
        return " ".join(self.text.lower().split())


def parse_rule(text: str) -> Rule:
    normalized = " ".join(text.split())  # the case is kept: a phrase is shown as written
    for kind, pattern in _PATTERNS:
        match = pattern.match(normalized)
        if match is None:
            continue
        if kind in ("below", "above"):
            currency = match[2].upper() if match[2] else ("USD" if "$" in normalized else None)
            return Rule(kind, text, amount=_decimal(match[1], text), currency=currency)
        if kind in ("drop", "rise"):
            return Rule(kind, text, percent=_decimal(match[1], text))
        if kind == "mentions":
            return Rule(kind, text, phrase=match[1])
        return Rule(kind, text)
    raise ConfigError(
        f"cannot understand the alert rule {text!r}. Try: new, changed, price below 1800 USD, "
        'price above 2500, drop 5%, rise 10%, mentions "text", in stock'
    )


def _decimal(raw: str, text: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise ConfigError(f"no usable number in the alert rule {text!r}") from exc


@dataclass(frozen=True, slots=True)
class Trigger:
    rule: Rule
    delta: Delta
    reason: str

    @property
    def key(self) -> str:
        """The rule and the fact: a condition stays raised under this key while it holds."""
        return alert_key(self.rule, self.delta.fact)


def alert_key(rule: Rule, fact: Fact) -> str:
    return f"{rule.key}|{fact.key}"


def triggers(rules: Sequence[Rule], deltas: Sequence[Delta], *, baseline: bool) -> list[Trigger]:
    """Events these deltas are, and conditions that hold now. On a watch's first run
    (*baseline*) everything is new, so "new" and "changed" stay quiet."""
    fired = []
    for rule in rules:
        for delta in deltas:
            if rule.is_condition:
                reason = _condition(rule, delta)
            else:
                reason = _EVENTS[rule.kind](rule, delta, baseline)
            if reason is not None:
                fired.append(Trigger(rule, delta, reason))
    return fired


def cleared(rules: Sequence[Rule], deltas: Sequence[Delta]) -> list[str]:
    """Keys of the conditions that were checked and no longer hold: they may alert again."""
    return [
        alert_key(rule, delta.fact)
        for rule in rules
        if rule.is_condition
        for delta in deltas
        if _condition(rule, delta) is None
    ]


# Conditions: why one holds, or None.


def _condition(rule: Rule, delta: Delta) -> str | None:
    fact = delta.fact
    if delta.change is Change.GONE:
        return None
    if rule.kind == "in_stock":
        available = fact.structured and fact.availability in AVAILABLE
        return f"in stock: {_label(fact)}" if available else None
    if fact.amount is None or rule.amount is None or fact.unit is not None:
        return None  # a rate ($ per hour) never meets a price limit
    if rule.currency is not None and fact.currency not in (None, rule.currency):
        return None
    holds = fact.amount < rule.amount if rule.kind == "below" else fact.amount > rule.amount
    return f"{rule.text}: {_label(fact)}" if holds else None


# Events: the alert's reason, or None.


def _new(rule: Rule, delta: Delta, baseline: bool) -> str | None:
    if baseline or delta.change is not Change.NEW:
        return None
    return f"new: {delta.fact.claim}"


def _changed(rule: Rule, delta: Delta, baseline: bool) -> str | None:
    fact = delta.fact
    if baseline:
        return None
    if delta.change is Change.GONE:
        return f"no longer on {fact.site}: {fact.claim}"
    percent = delta.percent
    if delta.change is not Change.CHANGED or (
        percent is not None and abs(percent) < MATERIAL_PERCENT
    ):
        return None
    before = delta.previous.value if delta.previous is not None else None
    return f"changed: {fact.entity or fact.claim}: {before} \N{RIGHTWARDS ARROW} {fact.value}"


def _move(rule: Rule, delta: Delta, baseline: bool) -> str | None:
    percent = delta.percent
    if delta.change is not Change.CHANGED or percent is None or rule.percent is None:
        return None
    moved = -percent if rule.kind == "drop" else percent
    if moved < rule.percent:
        return None
    before = delta.previous.value if delta.previous is not None else None
    return f"{rule.kind} {abs(percent):.1f}%: {_label(delta.fact)} (was {before})"


def _mentions(rule: Rule, delta: Delta, baseline: bool) -> str | None:
    fact = delta.fact
    if delta.change is not Change.NEW or not rule.phrase:
        return None
    if fold(rule.phrase) not in fold(f"{fact.claim} {fact.quote}"):
        return None
    return f"mentions {rule.phrase!r}: {fact.claim}"


def _label(fact: Fact) -> str:
    return f"{fact.entity}: {fact.value}" if fact.entity and fact.value else fact.claim


_EVENTS: dict[str, Callable[[Rule, Delta, bool], str | None]] = {
    "new": _new,
    "changed": _changed,
    "drop": _move,
    "rise": _move,
    "mentions": _mentions,
}
