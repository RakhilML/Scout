"""Values in findings: parse prices, turn published offers into findings, flag impossible values."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from statistics import median

from price_parser import Price

from scout.research.results import Finding, Flag, Source, Verdict
from scout.textutil import MAGNITUDE_PATTERN, MAGNITUDES, fold, rate_unit
from scout.web.extract import Offer

# Currency symbols as price-parser reports them, mapped to ISO codes ("$" is taken as USD).
_CURRENCY_CODES = {
    "$": "USD",
    "us$": "USD",
    "\N{EURO SIGN}": "EUR",
    "\N{POUND SIGN}": "GBP",
    "\N{INDIAN RUPEE SIGN}": "INR",
    "rs": "INR",
    "rs.": "INR",
    "\N{YEN SIGN}": "JPY",
    "c$": "CAD",
    "a$": "AUD",
}
_PRICE_ATTRIBUTE = re.compile(r"price|cost|msrp|deal", re.IGNORECASE)
# Words naming something bought *for* a product. Checked against what a finding is about (its
# entity), never against the whole claim: "a 5090 with a triple-fan cooler" is still a 5090.
_ACCESSORY = re.compile(
    r"\b(adapter|backplate|bracket|cable|cooler|holder|mount|riser|skin|sticker|stand|"
    r"water ?block)s?\b",
    re.IGNORECASE,
)
# A price below half, or above 2.5 times, the median of the *other* prices in the same currency
# is flagged. Half catches "$800 for an RTX 5090" when the others say about $2,000.
_OUTLIER_LOW, _OUTLIER_HIGH = Decimal("0.5"), Decimal("2.5")
_MIN_OTHERS = 2  # with fewer comparable prices there is no telling which one is wrong
# "$84.7K": price-parser reads 84.7, the suffix right after it makes it 84,700.
_MAGNITUDE = re.compile(rf"\s*({MAGNITUDE_PATTERN})\b", re.IGNORECASE)
_OFFERS_PER_SOURCE = 3  # a listing page can publish dozens; the cheapest few say enough


def parse_price(text: str) -> tuple[Decimal | None, str | None]:
    """Amount and ISO currency of a price such as "$1,999", "Rs. 1,66,269" or "$84.7K"."""
    price = Price.fromstring(text)
    amount = price.amount
    if amount is not None and price.amount_text:
        end = text.find(price.amount_text) + len(price.amount_text)
        scale = _MAGNITUDE.match(text, end)
        if scale is not None:
            amount *= MAGNITUDES[scale.group(1).lower()]
    symbol = (price.currency or "").strip()
    if not symbol:
        return amount, None
    return amount, _CURRENCY_CODES.get(symbol.lower(), symbol.upper())


def attach_amounts(findings: Sequence[Finding]) -> list[Finding]:
    """Give price findings a numeric amount, a currency and, for rates, a unit."""
    result = []
    for finding in findings:
        if finding.amount is None and finding.value and _is_price(finding):
            amount, currency = parse_price(finding.value)
            if amount is not None:
                unit = rate_unit(finding.value) or rate_unit(finding.claim)
                finding = replace(finding, amount=amount, currency=currency, unit=unit)
        result.append(finding)
    return result


def offer_findings(sources: Sequence[Source]) -> list[Finding]:
    """Published schema.org offers become findings directly: exact prices, no model involved.
    Per source, only the cheapest offer for each product, and the few cheapest of those."""
    findings = []
    for source in sources:
        for offer in _cheapest_offers(source.offers):
            price = f"{offer.price}-{offer.high_price}" if offer.high_price else f"{offer.price}"
            per = f"per {offer.unit}" if offer.unit else None
            amount_text = " ".join(part for part in (price, offer.currency, per) if part)
            product = offer.product or source.title
            claim = f"{product}: {amount_text}"
            if offer.availability:
                claim += f" ({offer.availability})"
            findings.append(
                Finding(
                    claim=claim,
                    quote=f"schema.org Offer: {product}: price {amount_text}",
                    source=source.index,
                    verdict=Verdict.VERIFIED,
                    entity=product,
                    attribute="price",
                    value=amount_text,
                    amount=offer.price,
                    currency=offer.currency,
                    unit=offer.unit,
                    origin="structured-data",
                    availability=offer.availability,
                )
            )
    return findings


def flag_values(findings: Sequence[Finding], goal: str) -> list[Finding]:
    """Flag accessory prices, then prices far from the median of the other prices."""
    goal_words = fold(goal)
    marked = [
        replace(finding, flag=Flag.ACCESSORY, note="an accessory's price, not the product's")
        if finding.amount is not None and _names_accessory(finding.entity, goal_words)
        else finding
        for finding in findings
    ]

    # Only verified values set the baseline: an invented price must not move the median. Prices
    # are only compared with prices in the same currency. Rates are left alone: spot, reserved
    # and on-demand rentals of one GPU differ by several times, and all of them are real.
    groups: dict[str | None, list[tuple[int, Decimal]]] = defaultdict(list)
    for position, finding in enumerate(marked):
        if finding.trusted and finding.amount is not None and finding.unit is None:
            groups[finding.currency].append((position, finding.amount))

    result = list(marked)
    for currency, members in groups.items():
        if len(members) <= _MIN_OTHERS:
            continue
        for position, amount in members:
            typical = median(other for p, other in members if p != position)
            if not typical * _OUTLIER_LOW <= amount <= typical * _OUTLIER_HIGH:
                note = f"far from the typical price of {typical} {currency or ''}".rstrip()
                result[position] = replace(marked[position], flag=Flag.OUTLIER, note=note)
    return result


def _cheapest_offers(offers: Sequence[Offer]) -> list[Offer]:
    cheapest: dict[str, Offer] = {}
    for offer in offers:
        product = fold(offer.product or "")
        if product not in cheapest or offer.price < cheapest[product].price:
            cheapest[product] = offer
    return sorted(cheapest.values(), key=lambda offer: offer.price)[:_OFFERS_PER_SOURCE]


def _is_price(finding: Finding) -> bool:
    return bool(finding.attribute and _PRICE_ATTRIBUTE.search(finding.attribute))


def _names_accessory(entity: str | None, goal_words: str) -> bool:
    match = _ACCESSORY.search(entity or "")
    return match is not None and fold(match.group(0)) not in goal_words
