from dataclasses import replace
from datetime import date
from decimal import Decimal
from urllib.parse import unquote

from scout.research import verify as verifying
from scout.research.results import Finding, Flag, Source, Verdict
from scout.research.schema import ModelFinding
from scout.research.verify import assess, locate, numbers, quoted_in, span_of, verify
from scout.textutil import fold
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
    assert result.anchor is None


def test_claim_numbers_must_appear_in_the_quote():
    (result,) = verify(
        [finding("The FE costs $1,799", "sells for $1,999.00 today", source=2, value="$1,799")],
        [SHOP],
    )
    assert result.verdict is Verdict.UNVERIFIED
    assert "1799" in result.note
    # The quote is on the page: whoever judges it can be shown where.
    assert result.anchor == "text=sells%20for%20%241%2C999.00%20today."


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


def test_numbers_keep_lone_digits_only_when_asked():
    assert numbers("GPT-4 has 7 rings and 1,999 stars") == {"1999"}
    assert numbers("GPT-4 has 7 rings and 1,999 stars", min_digits=1) == {"4", "7", "1999"}


def test_locate_finds_where_a_quote_is():
    text = "deal: it was sold for 1,999.00 dollars today"
    assert locate(text, "sold for 1,999.00 dollars") == (13, 38)
    assert locate(text, "sold for 1,999.00 dolars today") == (13, 44)  # near, widened to words
    assert locate(text, "sold for 1,899.00 dolars today") is None


PEP = Source(
    index=1,
    url="https://peps.example/pep-0719/",
    title="PEP 719 - Python 3.13 Release Schedule",
    site="peps.example",
    status="ok",
    query="q",
    published=date(2023, 5, 26),
    text="Expected: 3.13.0 final: Monday, October 7. Saturn has 7 rings.",
)


def test_in_a_fact_check_the_quote_alone_states_the_claims_numbers():
    # The schedule was published in 2023, but its quote never says 3.13 shipped in 2023.
    dated = finding(
        "Python 3.13 was released on October 7, 2023.", "3.13.0 final: Monday, October 7."
    )
    rings = finding("Saturn has 5 rings.", "Saturn has 7 rings.")
    strict = verify([dated, rings], [PEP], strict=True)
    assert [(f.verdict, f.note) for f in strict] == [
        (Verdict.UNVERIFIED, "the quote does not contain 2023"),
        (Verdict.UNVERIFIED, "the quote does not contain 5"),
    ]
    # Research is unchanged: a page's title and dates count, and lone digits are list numbering.
    assert [f.verdict for f in verify([dated, rings], [PEP])] == [Verdict.VERIFIED] * 2


def test_in_a_fact_check_a_near_quote_may_not_change_a_lone_digit():
    page = replace(PEP, text="Python 3.13.0 final shipped on October 7 after a long beta.")
    near = finding(
        "3.13.0 shipped in October", "Python 3.13.0 final shipped on October 1 after a long beta."
    )
    (strict,) = verify([near], [page], strict=True)
    assert (strict.verdict, strict.note) == (Verdict.UNVERIFIED, "quote not found in the source")
    assert verify([near], [page])[0].verdict is Verdict.VERIFIED


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
    assert result.anchor is None  # Scout wrote the line: the page has no such text


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
    assert finding.anchor is None  # a snippet is not the page


def test_a_quote_is_placed_on_its_page_in_the_pages_own_characters():
    said = "Python\N{RIGHT SINGLE QUOTATION MARK}s JIT \N{EM DASH} off by default."
    page = replace(DOCS, text=f"Notes. {said} More notes.")
    typed, shouted = verify(
        [
            finding("The JIT is off by default", "Python's JIT - off by default"),
            finding("The JIT is off by default", "PYTHON'S JIT - OFF BY DEFAULT."),
        ],
        [page],
    )
    assert typed.verdict is Verdict.VERIFIED
    assert typed.anchor == "text=Python%E2%80%99s%20JIT%20%E2%80%94%20off%20by%20default."
    assert unquote(typed.anchor) == f"text={said}"
    assert shouted.anchor == typed.anchor


def test_a_near_or_partial_quote_is_placed_on_the_whole_words_it_matched():
    dropped = (  # "a new interactive interpreter" on the page
        "The biggest changes include a interactive interpreter, experimental support for "
        "running in a free threaded mode (PEP 703)"
    )
    (near,) = verify([finding("3.13 adds a free-threaded mode (PEP 703)", dropped)], [DOCS])
    start, end = (unquote(term) for term in near.anchor.removeprefix("text=").split(","))
    assert start.startswith("The biggest changes")  # the first word, though the match missed it
    assert end == "free-threaded mode (PEP 703),"

    (cut,) = verify([finding("It sells for $1,999", "ells for $1,999.00 tod", source=2)], [SHOP])
    assert cut.anchor == "text=sells%20for%20%241%2C999.00%20today."


def test_quotes_are_placed_where_locate_finds_them_and_each_page_is_read_for_it_once(
    monkeypatch,
):
    lines = [
        f"Item {n}\N{EN DASH}its \N{LEFT DOUBLE QUOTATION MARK}weight"
        f"\N{RIGHT DOUBLE QUOTATION MARK} is {n * 7} kg."
        for n in range(3000)
    ]
    text = "\n".join(lines)
    for n in (5, 1234, 2999):
        quote = fold(f'Item {n}-its "weight" is {n * 7} KG')
        start, end = locate(fold(text), quote)
        placed = span_of(text, quote)
        assert text[slice(*placed)] == lines[n]
        assert fold(text)[start:end] in fold(text[slice(*placed)])

    mapped, of = [], verifying.Words.of
    monkeypatch.setattr(verifying.Words, "of", lambda text: mapped.append(text) or of(text))
    page = replace(DOCS, text=text)
    found = [finding(f"Item {n} weighs {n * 7} kg", lines[n]) for n in (10, 20)]
    assert all(result.anchor for result in verify(found, [page]))
    assert len(mapped) == 1  # and none kept once the call is done


def test_m_and_b_are_millions_only_after_a_currency_sign_and_small_numbers_may_be_words():
    assert numbers("The tower is 330 m tall; the deal was $330m.") == {"330", "330000000"}
    assert numbers("Mars has two moons.", min_digits=1) == {"2"}


def test_a_version_is_one_number():
    released = "Python 3.13.0 was released on October 7, 2023."
    assert numbers(released, min_digits=1) == {"3.13", "7", "2023"}  # 3.13.0 is 3.13
    assert numbers("Upgrade from 1.2.3 to v2.0.0, not 10.0.0.1.", min_digits=1) == {
        "1.2.3",
        "2",
        "10.0.0.1",
    }
    assert numbers("Upgrade to v2.0.0.") == set()  # a lone digit, as research reads numbers
    assert numbers("Mars has two moons.") == set()  # research compares numbers of 2+ digits


def test_a_quote_in_text_without_spaces_is_placed_to_the_character():
    version, year, released = (
        "\N{CJK UNIFIED IDEOGRAPH-7248}\N{CJK UNIFIED IDEOGRAPH-672C}",
        "\N{CJK UNIFIED IDEOGRAPH-5E74}",
        "\N{CJK UNIFIED IDEOGRAPH-53D1}\N{CJK UNIFIED IDEOGRAPH-5E03}",
    )
    sentence = f"3.13{version}2024{year}10{released}"
    filler = "\N{CJK UNIFIED IDEOGRAPH-4E00}" * 200
    page = replace(DOCS, text=f"{filler}{sentence}{filler}")
    (found,) = verify([finding("3.13 came out in 2024", sentence)], [page])
    assert unquote(found.anchor) == f"text={sentence}"  # not the whole paragraph
