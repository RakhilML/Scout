from dataclasses import replace
from datetime import date
from decimal import Decimal

from scout.research.results import Finding, Flag, Source, Verdict
from scout.research.schema import ModelFinding
from scout.research.verify import assess, quoted_in, verify
from scout.web.extract import Offer

TODAY = date(2026, 9, 25)
DOCS = Source(
    index=1,
    url="https://docs.python.org/3.13/whatsnew/3.13.html",
    title="What's New In Python 3.13",
    site="Python documentation",
    status="ok",
    query="q",
    text=(
        "Python 3.13 was released on October 7, 2024.\n\nThe biggest changes include a new "
        "interactive interpreter, experimental support for running in a free-threaded mode "
        "(PEP 703), and a Just-In-Time compiler (PEP 744)."
    ),
)
SHOP = Source(
    index=2,
    url="https://shop.example/rtx-5090",
    title="RTX 5090 Founders Edition",
    site="shop.example",
    status="ok",
    query="q",
    text="The RTX\N{NARROW NO-BREAK SPACE}5090 Founders Edition sells for $1,999.00 today.",
)


def finding(claim, quote, source=1, value=None):
    return ModelFinding(claim=claim, quote=quote, source=source, value=value)


def test_exact_quote_is_verified():
    (result,) = verify(
        [finding("3.13 came out in October 2024", "Python 3.13 was released on October 7, 2024.")],
        [DOCS],
    )
    assert result.verdict is Verdict.VERIFIED
    assert result.note is None


def test_near_exact_quote_survives_typography_and_small_slips():
    quote = (
        "The biggest changes include a new interactive interpreter, experimental support "
        "for running in a free threaded mode (PEP 703)"  # "free threaded", not "free-threaded"
    )
    (result,) = verify([finding("3.13 adds a free-threaded mode (PEP 703)", quote)], [DOCS])
    assert result.verdict is Verdict.VERIFIED


def test_a_near_quote_with_a_changed_number_is_rejected():
    # 96% similar to the page, but the page says $1,999: a near match never excuses a number.
    quote = "The RTX 5090 Founders Edition sells for $1,849.00 today."
    (result,) = verify([finding("The FE sells for $1,849", quote, 2, "$1,849")], [SHOP])
    assert result.verdict is Verdict.UNVERIFIED
    assert result.note == "quote not found in the source"


def test_near_quotes_keep_numbers_cut_at_the_edge_of_the_match():
    page = replace(SHOP, text="Deal: RTX 5090 FE sells for $1,999.00 today only at shop")
    (result,) = verify(
        [finding("It sells for $1,999", "RTX 5090 FE sells for $1,999.00 today only", 2)], [page]
    )
    assert result.verdict is Verdict.VERIFIED
    assert quoted_in("sold for 1,999.00 dollars", "sold for 1,999.00 dollar")
    assert not quoted_in("sold for 1,999.00 dollars", "sold for 1,899.00 dollar")


def test_the_model_can_withhold_trust_but_not_grant_it():
    doubted = ModelFinding(
        claim="The FE sells for $1,999",
        quote="sells for $1,999.00 today",
        source=2,
        doubt="the same list also prices an RTX 3050",
    )
    invented = ModelFinding(claim="It costs $99", quote="Only $99 today!", source=2, doubt=" ")
    first, second = verify([doubted, invented], [SHOP])
    assert (first.verdict, first.flag, first.trusted) == (Verdict.VERIFIED, Flag.DOUBTED, False)
    assert first.note == "doubtful: the same list also prices an RTX 3050"
    assert (second.verdict, second.flag) == (Verdict.UNVERIFIED, None)


def test_quote_from_another_source_is_reattributed():
    (result,) = verify(
        [finding("The FE sells for $1,999", "sells for $1,999.00 today", source=1, value="$1,999")],
        [DOCS, SHOP],
    )
    assert (result.verdict, result.source) == (Verdict.VERIFIED, 2)
    assert result.note == "quote is from source 2"


def test_invented_quote_is_rejected():
    (result,) = verify(
        [finding("3.13 removed the GIL entirely", "Python 3.13 removes the GIL for everyone.")],
        [DOCS],
    )
    assert result.verdict is Verdict.UNVERIFIED
    assert result.note == "quote not found in the source"


def test_claim_numbers_must_appear_in_the_quote():
    (result,) = verify(
        [finding("The FE costs $1,799", "sells for $1,999.00 today", source=2, value="$1,799")],
        [SHOP],
    )
    assert result.verdict is Verdict.UNVERIFIED
    assert "1799" in result.note


def test_number_formats_are_normalized_and_title_numbers_count():
    ok = verify(
        [
            finding(
                "The 5090 FE is $1999",
                "The RTX 5090 Founders Edition sells for $1,999.00",
                source=2,
            ),
            finding(
                "PEP 744 adds a JIT to 3.13", "and a Just-In-Time compiler (PEP 744)", source=1
            ),
        ],
        [DOCS, SHOP],
    )
    assert [r.verdict for r in ok] == [Verdict.VERIFIED, Verdict.VERIFIED]


def test_too_short_quote_is_not_checkable():
    (result,) = verify([finding("It is fast", "fast")], [DOCS])
    assert result.verdict is Verdict.UNVERIFIED


def trusted(source=1):
    return Finding(claim="c", quote="q", source=source, verdict=Verdict.VERIFIED)


def test_confidence_comes_from_evidence():
    other = Source(
        index=3, url="https://blog.example/a", title="t", site="s", status="ok", query="q"
    )
    sources = [DOCS, SHOP, other]
    high = assess([trusted(1), trusted(2), trusted(3)], sources, kind="general", today=TODAY)
    assert high.level == "high"
    assert high.reason == "3 of 3 findings trusted across 3 site(s)"

    one_site = assess([trusted(1), trusted(1), trusted(1)], sources, kind="general", today=TODAY)
    assert one_site.level == "medium"

    failed = Finding(claim="c", quote="q", source=1, verdict=Verdict.UNVERIFIED)
    assert assess([failed], sources, kind="general", today=TODAY).level == "low"
    assert assess([], sources, kind="general", today=TODAY).level == "low"

    flagged = Finding(claim="c", quote="q", source=1, verdict=Verdict.VERIFIED, flag=Flag.OUTLIER)
    assert assess([flagged], sources, kind="general", today=TODAY).level == "low"


def test_stale_news_is_downgraded():
    old = Source(
        index=1,
        url="https://a.example",
        title="t",
        site="s",
        status="ok",
        query="q",
        published=date(2025, 1, 1),
    )
    fresh = Source(
        index=2,
        url="https://b.example",
        title="t",
        site="s",
        status="ok",
        query="q",
        updated=date(2026, 9, 20),
    )
    findings = [trusted(1), trusted(1), trusted(1)]
    stale = assess(findings, [old, fresh], kind="news", today=TODAY)
    assert stale.level == "low"
    assert "days old" in stale.reason

    current = assess([trusted(2), trusted(2)], [old, fresh], kind="news", today=TODAY)
    assert current.level == "medium"


# Cases found by running Scout against the live web with a real model as the extractor.


def test_magnitude_spellings_agree():
    stars = Source(
        1,
        "https://a.example",
        "Frameworks",
        "a.example",
        "ok",
        "q",
        text="FastAPI has 78k+ GitHub stars.",
    )
    (result,) = verify(
        [finding("FastAPI has over 78,000 stars", "FastAPI has 78k+ GitHub stars.")], [stars]
    )
    assert result.verdict is Verdict.VERIFIED


def test_claims_may_cite_the_sources_own_date():
    page = Source(
        1,
        "https://a.example",
        "Showdown",
        "a.example",
        "ok",
        "q",
        updated=date(2026, 9, 16),
        text="FastAPI handles 3-5x more requests per second.",
    )
    (result,) = verify(
        [
            finding(
                "As of 16 September 2026, FastAPI handles 3-5x more requests",
                "FastAPI handles 3-5x more requests per second.",
            )
        ],
        [page],
    )
    assert result.verdict is Verdict.VERIFIED


def test_quotes_of_published_price_lines_count_as_the_source():
    shop = Source(
        1,
        "https://gpudojo.example/4090",
        "RTX 4090 prices",
        "gpudojo.example",
        "ok",
        "q",
        text="Used and new prices.",
        offers=(
            Offer(
                product="RTX 4090", price=Decimal("2073.49"), currency="USD", availability="InStock"
            ),
        ),
    )
    (result,) = verify(
        [
            finding(
                "GPUDojo's lowest RTX 4090 offer is $2,073.49", "- RTX 4090: 2073.49 USD, InStock"
            )
        ],
        [shop],
    )
    assert result.verdict is Verdict.VERIFIED


def test_numbers_glued_to_table_cells_are_still_read():
    # Real quotes from a price-history table whose cells trafilatura joined without spaces.
    table = Source(
        1,
        "https://gpudojo.example",
        "RTX 4090 history",
        "gpudojo.example",
        "ok",
        "q",
        text="Amazon$3299current Newegg$4499at high eBay$2073mid-range",
    )
    results = verify(
        [
            finding("Amazon lists it at $3,299", "Amazon$3299current"),
            finding("eBay sits near $2,073", "eBay$2073mid-range"),  # not 2073 million
        ],
        [table],
    )
    assert [r.verdict for r in results] == [Verdict.VERIFIED, Verdict.VERIFIED]


def test_a_quote_from_a_search_snippet_says_so():
    snippet = Source(
        1,
        "https://a.example/py",
        "Python",
        "a.example",
        "blocked",
        "q",
        snippet_only=True,
        text="Python 3.14 was released on October 7, 2025.",
    )
    claim = ModelFinding(
        claim="Python 3.14 is out", quote="Python 3.14 was released on October 7, 2025.", source=1
    )
    (finding,) = verify([claim], [snippet])
    assert finding.verdict is Verdict.VERIFIED
    assert finding.note == "quoted from the search result: the page itself could not be read"
