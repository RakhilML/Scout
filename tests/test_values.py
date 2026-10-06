from dataclasses import replace
from decimal import Decimal

import pytest

from scout.research.results import Finding, Flag, Source, Verdict
from scout.research.values import attach_amounts, flag_values, offer_findings, parse_price
from scout.textutil import rate_unit
from scout.web.extract import Offer


@pytest.mark.parametrize(
    ("text", "amount", "currency"),
    [
        ("$1,999.99", Decimal("1999.99"), "USD"),
        ("Rs. 1,66,269.38", Decimal("166269.38"), "INR"),
        ("\N{INDIAN RUPEE SIGN}38,641", Decimal("38641"), "INR"),
        ("22,90 \N{EURO SIGN}", Decimal("22.90"), "EUR"),
        ("1999 USD", Decimal("1999"), "USD"),
        ("about 2000", Decimal("2000"), None),
    ],
)
def test_parse_price(text, amount, currency):
    assert parse_price(text) == (amount, currency)


def price(value, entity="RTX 5090", verdict=Verdict.VERIFIED):
    return Finding(
        claim=f"{entity} costs {value}",
        quote="q",
        source=1,
        verdict=verdict,
        entity=entity,
        attribute="price",
        value=value,
    )


def test_only_price_findings_get_amounts():
    version = Finding(
        claim="c",
        quote="q",
        source=1,
        verdict=Verdict.VERIFIED,
        attribute="version",
        value="3.13.0",
    )
    priced, other = attach_amounts([price("$1,999"), version])
    assert (priced.amount, priced.currency) == (Decimal("1999"), "USD")
    assert other.amount is None


def test_accessory_prices_are_flagged_by_what_they_are_about():
    findings = attach_amounts([price("$29", entity="RTX 5090 support bracket"), price("$1,999")])
    bracket, card = flag_values(findings, "cheapest RTX 5090")
    assert bracket.flag is Flag.ACCESSORY
    assert card.flag is None


def test_accessories_that_are_the_goal_are_not_flagged():
    findings = attach_amounts([price("$29", entity="GPU support bracket")])
    (bracket,) = flag_values(findings, "cheapest GPU support bracket")
    assert bracket.flag is None


def test_outliers_need_enough_peers():
    card_prices = ["$1,999", "$2,099", "$2,199", "$800"]
    flagged = flag_values(attach_amounts([price(v) for v in card_prices]), "RTX 5090 price")
    assert [f.flag for f in flagged] == [None, None, None, Flag.OUTLIER]
    assert "typical price" in flagged[-1].note

    two = flag_values(attach_amounts([price("$1,999"), price("$800")]), "RTX 5090 price")
    assert [f.flag for f in two] == [None, None]


def test_outliers_are_judged_per_currency():
    mixed = [price("$1,999"), price("$2,050"), price("\N{INDIAN RUPEE SIGN}1,66,269")]
    assert all(f.flag is None for f in flag_values(attach_amounts(mixed), "RTX 5090 price"))


def test_published_offers_become_findings():
    source = Source(
        index=4,
        url="https://deals.example/5090",
        title="Deals",
        site="deals.example",
        status="ok",
        query="q",
        offers=(
            Offer(
                product="RTX 5090 FE",
                price=Decimal("1899"),
                currency="USD",
                high_price=Decimal("2499"),
                availability="InStock",
            ),
        ),
    )
    (finding,) = offer_findings([source])
    assert finding.claim == "RTX 5090 FE: 1899-2499 USD (InStock)"
    assert (finding.amount, finding.currency, finding.source) == (Decimal("1899"), "USD", 4)
    assert finding.origin == "structured-data"
    assert finding.trusted


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        ("$0.53/hr", "hour"),
        ("$9.99 per month", "month"),
        ("$120 a year", "year"),
        ("$0.10 per GB", "gb"),
        # How GPU rental trackers write it (getdeploying.com, gpuperhour.com):
        ("$0.35 /GPU/hr", "hour"),
        ("$0.53/GPU-hr", "hour"),
        ("$0.53 per GPU-hour", "hour"),
        ("$0.35 per GPU per hour", "hour"),
        ("hours", "hour"),
        ("$1,999", None),
        ("$1,999/HDMI bundle", None),
        ("$1,999, a new month low", None),
    ],
)
def test_rate_unit(text, unit):
    assert rate_unit(text) == unit


def test_an_offer_without_a_product_name_is_an_offer_of_its_page():
    offer = Offer(product=None, price=Decimal("1899"), currency="USD")
    page = Source(1, "https://shop.example/5090", "RTX 5090 FE", "shop.example", "ok", "q")
    (finding,) = offer_findings([replace(page, offers=(offer,))])
    assert finding.entity == "RTX 5090 FE"
    assert finding.quote == "schema.org Offer: RTX 5090 FE: price 1899 USD"


def test_published_rental_offers_are_rates():
    offer = Offer(product="RTX 5090", price=Decimal("0.53"), currency="USD", unit="hour")
    source = Source(1, "https://rent.example", "Rent", "rent.example", "ok", "q", offers=(offer,))
    (finding,) = offer_findings([source])
    assert (finding.claim, finding.unit) == ("RTX 5090: 0.53 USD per hour", "hour")


def test_rental_rates_are_not_compared_with_sale_prices():
    findings = attach_amounts(
        [price("$1,999"), price("$2,099"), price("$2,199"), price("$0.53/hr"), price("$0.61/hr")]
    )
    flagged = flag_values(findings, "RTX 5090 price")
    assert [f.unit for f in flagged] == [None, None, None, "hour", "hour"]
    assert all(f.flag is None for f in flagged)


def test_rates_are_never_outliers():
    # Seen on getdeploying.com: spot and reserved rentals at a fraction of the on-demand rate.
    rates = [price("$0.53/hr"), price("$0.61/hr"), price("$0.58/hr"), price("$0.21/hr")]
    assert all(f.flag is None for f in flag_values(attach_amounts(rates), "RTX 5090 price"))


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("$83K", Decimal("83000")),
        ("$84.7k", Decimal("84700")),
        ("$1.2M", Decimal("1200000")),
        ("$1,999 MSRP", Decimal("1999")),
        ("$15 per 1M tokens", Decimal("15")),
        ("$599 for the 4K model", Decimal("599")),
        ("$1,999 (2 m cable)", Decimal("1999")),
    ],
)
def test_parse_price_understands_magnitudes(text, amount):
    assert parse_price(text) == (amount, "USD")


def test_only_the_cheapest_published_offers_become_findings():
    offers = tuple(
        Offer(product=f"RTX 4090 {variant}", price=Decimal(price), currency="USD")
        for variant, price in [
            ("A", "2500"),
            ("A", "2400"),
            ("B", "2100"),
            ("C", "3100"),
            ("D", "2900"),
        ]
    )
    source = Source(
        index=1,
        url="https://dojo.example",
        title="t",
        site="s",
        status="ok",
        query="q",
        offers=offers,
    )
    assert [f.claim for f in offer_findings([source])] == [
        "RTX 4090 B: 2100 USD",
        "RTX 4090 A: 2400 USD",
        "RTX 4090 D: 2900 USD",
    ]
