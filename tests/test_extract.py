import time
from datetime import date
from decimal import Decimal

import pytest

from scout.errors import ExtractionError
from scout.research.citations import cited
from scout.research.factcheck import cited_by
from scout.web.extract import extract_html, extract_pdf
from tests.helpers import make_pdf

TODAY = date(2026, 9, 25)
PARAGRAPH = "<p>" + "Retailers cut prices on flagship graphics cards this week. " * 12 + "</p>"


def page(head: str = "", body: str = PARAGRAPH) -> bytes:
    html = f"<html><head><title>GPU prices</title>{head}</head>"
    return (html + f"<body><article>{body}</article></body></html>").encode()


def ld(json_text: str) -> str:
    return f'<script type="application/ld+json">{json_text}</script>'


def test_html_text_title_and_article_dates():
    head = ld(
        '{"@type": "NewsArticle", "datePublished": "2026-03-18T09:00:00Z",'
        ' "dateModified": "2026-03-19"}'
    )
    result = extract_html(page(head), "https://news.example.com/a", today=TODAY)
    assert "flagship graphics cards" in result.text
    assert result.title == "GPU prices"
    assert (result.published, result.updated) == (date(2026, 3, 18), date(2026, 3, 19))


def test_fields_and_source_wrapped_prose_are_rejoined():
    body = (
        "<dl><dt>Created:</dt><dd>08-Jul-2024</dd><dt>Resolution:</dt><dd>10-Apr-2025</dd></dl>"
        "<p>Template strings are a generalization of f-strings, using a t in place of\n"
        "the f prefix. They evaluate to a new type.</p>" + PARAGRAPH
    )
    text = extract_html(page(body=body), "https://peps.example/750", today=TODAY).text
    assert "Created: 08-Jul-2024" in text
    assert "Resolution: 10-Apr-2025" in text
    assert "using a t in place of the f prefix." in text


def test_implausible_dates_are_dropped():
    head = ld('{"@type": "Article", "datePublished": "2031-01-01"}')
    assert extract_html(page(head), "https://example.com/a", today=TODAY).published is None


def test_product_offer_is_read_from_jsonld():
    head = ld(
        '{"@context": "https://schema.org", "@type": "Product", "name": "RTX 5090",'
        ' "offers": {"@type": "Offer", "price": "1,999.00", "priceCurrency": "usd",'
        ' "availability": "https://schema.org/InStock", "url": "https://shop.example/5090"}}'
    )
    (offer,) = extract_html(page(head), "https://shop.example/5090", today=TODAY).offers
    assert offer.product == "RTX 5090"
    assert offer.price == Decimal("1999.00")
    assert offer.currency == "USD"
    assert offer.availability == "InStock"


def test_aggregate_offer_inside_graph_and_itemlist():
    head = ld(
        '{"@graph": [{"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item":'
        ' {"@type": "Product", "name": "RTX 5090 FE", "offers": {"@type": "AggregateOffer",'
        ' "lowPrice": 1899, "highPrice": 2499.5, "priceCurrency": "USD"}}}]}]}'
    )
    (offer,) = extract_html(page(head), "https://deals.example/5090", today=TODAY).offers
    assert (offer.product, offer.price, offer.high_price) == (
        "RTX 5090 FE",
        Decimal("1899"),
        Decimal("2499.5"),
    )


def test_standalone_offer_uses_item_offered_name_and_is_not_double_counted():
    head = ld(
        '[{"@type": "Offer", "price": 74264.36, "priceCurrency": "USD",'
        '  "itemOffered": {"name": "Bitcoin"}},'
        ' {"@type": "Product", "name": "Card",'
        '  "offers": [{"@type": "Offer", "price": 10, "priceCurrency": "EUR"}]}]'
    )
    offers = extract_html(page(head), "https://x.example", today=TODAY).offers
    assert [(o.product, o.price) for o in offers] == [
        ("Bitcoin", Decimal("74264.36")),
        ("Card", Decimal("10")),
    ]


def test_rental_offers_carry_their_unit_to_the_offers_nested_in_them():
    # As gpuperhour.com publishes GPU rentals: an hourly AggregateOffer holding the offers.
    rentals = ld(
        '{"@type": "Product", "name": "NVIDIA RTX 5090", "offers": {"@type": "AggregateOffer",'
        ' "lowPrice": "0.77", "highPrice": "2.40", "priceCurrency": "USD",'
        ' "priceSpecification": {"@type": "UnitPriceSpecification", "unitCode": "HUR",'
        ' "unitText": "per GPU per hour"},'
        ' "offers": [{"@type": "Offer", "price": "0.77", "priceCurrency": "USD"},'
        ' {"@type": "Offer", "price": "1.11", "priceCurrency": "USD"}]}}'
    )
    subscription = ld(
        '{"@type": "Offer", "price": "9", "priceCurrency": "USD",'
        ' "priceSpecification": {"@type": "UnitPriceSpecification", "unitText": "month"}}'
    )
    head = rentals + subscription
    offers = extract_html(page(head), "https://rent.example/5090", today=TODAY).offers
    assert [(o.product, o.price, o.unit) for o in offers] == [
        ("NVIDIA RTX 5090", Decimal("0.77"), "hour"),
        ("NVIDIA RTX 5090", Decimal("0.77"), "hour"),
        ("NVIDIA RTX 5090", Decimal("1.11"), "hour"),
        (None, Decimal("9"), "month"),
    ]


def test_malformed_jsonld_and_zero_prices_are_ignored():
    head = ld("{not json") + ld('{"@type": "Offer", "price": 0, "priceCurrency": "USD"}')
    assert extract_html(page(head), "https://x.example", today=TODAY).offers == ()


def test_pdf_text_title_and_date():
    result = extract_pdf(
        make_pdf("Attention is all you need", title="Transformer paper"), today=TODAY
    )
    assert "Attention is all you need" in result.text
    assert result.title == "Transformer paper"
    assert result.published == date(2026, 3, 1)


def test_broken_pdf_raises_extraction_error():
    with pytest.raises(ExtractionError):
        extract_pdf(b"%PDF-1.4 this is not really a pdf", today=TODAY)


def test_meta_tags_give_published_and_updated_dates():
    head = (
        '<meta property="article:published_time" content="2025-11-02T08:00:00+05:30">'
        '<meta property="article:modified_time" content="2026-01-15">'
    )
    result = extract_html(page(head), "https://news.example/a", today=TODAY)
    assert (result.published, result.updated) == (date(2025, 11, 2), date(2026, 1, 15))


def test_time_element_and_url_dates():
    body = PARAGRAPH + '<time datetime="2026-02-03T10:00:00Z">Feb 3</time>'
    assert extract_html(page(body=body), "https://x.example/a", today=TODAY).published == date(
        2026, 2, 3
    )
    blog = extract_html(
        page(), "https://blog.python.org/2024/10/python-3130-final-released/", today=TODAY
    )
    assert blog.published == date(2024, 10, 1)


def test_no_date_is_guessed_from_stray_numbers():
    body = PARAGRAPH + '<img src="/static/logo.png?v=1426204800"><p>Build 20150313</p>'
    assert (
        extract_html(page(body=body), "https://docs.example/whatsnew", today=TODAY).published
        is None
    )


def test_inline_code_keeps_the_spaces_around_it():
    notes = (
        "<ul><li><p><code>OLD</code> and <code>NEW</code> support for <code>RETURNING</code> "
        "clauses in <code>INSERT</code>, <kbd>UPDATE</kbd>, and MERGE commands.</p></li></ul>"
        "<pre><code>SELECT 1;\n  SELECT 2;</code></pre>"
    )
    text = extract_html(page(body=notes + PARAGRAPH), "https://docs.example/18", today=TODAY).text
    assert "OLD and NEW support for RETURNING clauses in INSERT, UPDATE, and MERGE" in text
    assert "SELECT 1;\nSELECT 2;" in text  # a code block keeps its lines


RELEASE = "https://www.python.org/downloads/release/python-3130/"
REALPY = "https://realpython.com/python313-new-features/"
FILLER = "Python 3.13 brought many changes that this review goes through one by one. " * 6
RELEASED = (
    "Python 3.13 was released on October 7, 2024, a little later than planned, according to "
    "the release page"
)
OWN_LINKS = (
    "My older post tried it out. See every Python post, write to me or share it. "
    "Its docs call free threading a [sic] feature."
)
HAS_JIT = "It has an experimental JIT compiler"
BENCH = "https://bench.example/python-313"
BLOG = "https://blog.example.com/2024/10/python-313"
BLOG_POST = (
    "<html><head><title>Python 3.13, reviewed</title></head><body>"
    '<nav><a href="/">Home</a> <a href="/archive">Archive</a></nav><article>'
    f'<p>{RELEASED.removesuffix(" the release page")}\n<a href="{RELEASE}">the release page</a>. '
    f'{HAS_JIT} (<a href="{REALPY}">realpython.com</a>). '
    f'It is 5% faster.<a href="{BENCH}">[3]</a></p>'
    '<p>My <a href="/blog/older-post">older post</a> tried it out. See every '
    '<a href="https://www.example.com/tag/python">Python post</a>, '
    '<a href="mailto:me@example.com">write to me</a> or '
    '<a href="javascript:void(0)">share it</a>. Its docs call free threading '
    '<a href="https://docs.example.com/ft">a [sic] feature</a>.</p>'
    '<ul><li>\n<a href="/2024/09/python-313-rc">The release candidate</a> came first.</li></ul>'
    f"<p>{FILLER}</p></article></body></html>"
).encode()


def test_a_page_read_with_its_links_keeps_links_to_other_sites_and_words_for_its_own():
    text = extract_html(BLOG_POST, BLOG, today=TODAY, links=True).text
    found = cited(text)
    assert found.pages == {1: RELEASE, 2: REALPY, 3: BENCH}
    assert f"{RELEASED} [1]. {HAS_JIT} [2]. It is 5% faster [3]." in found.text.split("\n")
    assert OWN_LINKS in found.text.split("\n")
    assert "- The release candidate came first." in found.text.split("\n")

    plain = extract_html(BLOG_POST, BLOG, today=TODAY).text
    assert plain == (
        f"{RELEASED}. {HAS_JIT} (realpython.com). It is 5% faster.[3]\n"
        f"{OWN_LINKS}\n- The release candidate came first.\n{FILLER.strip()}"
    )


WIKIPEDIA = "https://en.wikipedia.org/wiki/Python_Software_Foundation"
STYLE = "<style>.mw-parser-output cite.citation{font-style:inherit}" + " " * 2000 + "</style>"
ARTICLE = (
    "<html><head><title>Python Software Foundation</title></head><body>"
    '<div id="content"><h1>Python Software Foundation</h1>'
    "<p>The Python Software Foundation is a nonprofit organization devoted to the Python "
    'programming language.<sup id="cite_ref-2"><a href="#cite_note-2">[2]</a></sup> '
    'It was launched on March 6, 2001.<sup id="cite_ref-3"><a href="#cite_note-3">[3]</a></sup> '
    "Legal scholars studied how it holds the Python trademark."
    '<sup id="cite_ref-4"><a href="#cite_note-4">[4]</a></sup> '
    'It runs PyCon US every year.<sup id="cite_ref-5"><a href="#cite_note-5">[5]</a></sup> '
    'Its supporting members pay $99 a year.<sup id="cite_ref-a"><a href="#cite_note-a">[a]</a>'
    f"</sup></p><p>{FILLER}</p><h2>Notes</h2><ol>"
    '<li id="cite_note-a"><a href="#cite_ref-a">^</a> The fee was set in 2024, as '
    '<a href="https://pyfound.blogspot.com/2024/fees">the PSF blog</a> announced.</li></ol>'
    '<h2>References</h2><ol class="references">'
    f'<li id="cite_note-2"><a href="#cite_ref-2">^</a> {STYLE}'
    '<a href="/wiki/Stephan_Deibel">Deibel, Stephan</a> (2008). '
    '<a href="https://search.worldcat.org/oclc/52858">52858</a> '
    '<a href="https://www.python.org/psf/summary/">"Executive Summary"</a>. '
    "Retrieved 2016-10-05.</li>"
    '<li id="cite_note-3"><a href="#cite_ref-3">^</a> '
    'doi:<a href="https://doi.org/10.1000/182">10.1000/182</a>. Retrieved 2020-01-01.</li>'
    '<li id="cite_note-4"><a href="#cite_ref-4">^</a> '
    '<a href="#CITEREFLee2012">Lee 2012</a>, p. 5.</li>'
    '<li id="cite_note-5"><a href="#cite_ref-5">^</a> Smith, John (2019). The PyCon Book. '
    'ISBN <a href="/wiki/Special:BookSources/9780000000000">978-0-00-000000-0</a>. '
    'OCLC <a href="https://search.worldcat.org/oclc/42">42</a>. Retrieved 2021-02-02.</li>'
    "</ol><h2>Sources</h2><ul><li>"
    '<cite id="CITEREFLee2012">Lee, Jyh-An (2012). '
    '<a href="https://books.google.com/books?id=IGmgp8pMTI8C">Nonprofit Organizations and the '
    "Intellectual Commons</a>. Edward Elgar.</cite></li></ul></div>"
    '<footer>Text is available under a license.<sup><a href="#cite_note-6">[6]</a></sup>'
    '<ol><li id="cite_note-6"><a href="https://creativecommons.org/licenses/by-sa/4.0/">'
    "CC BY-SA</a></li></ol></footer></body></html>"
).encode()


def test_in_page_notes_cite_their_first_titled_web_address_by_their_own_number():
    found = cited(extract_html(ARTICLE, WIKIPEDIA, today=TODAY, links=True).text)
    assert found.pages == {
        1: "https://pyfound.blogspot.com/2024/fees",  # [a] takes the lowest number free
        2: "https://www.python.org/psf/summary/",  # not the author's page, not a catalogue's
        3: "https://doi.org/10.1000/182",
        4: "https://books.google.com/books?id=IGmgp8pMTI8C",  # its short note's full entry
    }  # and no [6]: the footer citing it is not the article's text
    assert "It was launched on March 6, 2001 [3]." in found.text
    assert "It runs PyCon US every year [5]." in found.text  # a book: no address to read
    assert "Its supporting members pay $99 a year [1]." in found.text
    assert ("Retrieved" in found.text, "^" in found.text) == (False, False)


def test_links_to_this_page_that_are_not_notes_are_words():
    article = (
        '<html><body id="top"><article id="content"><h2 id="history">History</h2><p>'
        '<a href="#content">Skip to content</a>. Read the <a href="#history">History</a>. '
        'Python 3.13 has a <a>JIT compiler</a>.<a href="#missing">[7]</a> It is fast.'
        f'<a href="{BLOG}#fn9">[9]</a> <a href="#top">Back to top</a>.</p>'
        f"<p>{FILLER}</p></article></body></html>"
    ).encode()
    text = extract_html(article, BLOG, today=TODAY, links=True).text
    assert text.startswith(
        "History\nSkip to content. Read the History. Python 3.13 has a JIT compiler.[7] It is "
        "fast.[9] Back to top."
    )
    assert "[^" not in text
    assert cited(text).pages == {}


def test_two_lists_of_notes_numbered_alike_keep_their_own_addresses():
    first, second = "https://a.example/one", "https://b.example/two"
    posts = (
        "<html><body><article><p>The first post says Python 3.13 is out."
        '<sup><a href="#fn1-first">1</a></sup></p><ol><li id="fn1-first">'
        f'<a href="{first}">Its release notes</a></li></ol>'
        "<p>The second post says it is fast.<sup>"
        '<a href="#fn1-second">1</a></sup></p><ol><li id="fn1-second">'
        f'<a href="{second}">A benchmark</a></li></ol><p>{FILLER}</p></article></body></html>'
    ).encode()
    found = cited(extract_html(posts, BLOG, today=TODAY, links=True).text)
    assert found.pages == {1: first, 2: second}
    assert "The second post says it is fast [2]." in found.text


def linked(body: str, url: str = BLOG) -> str:
    html = f"<html><body><article>{body}<p>{FILLER}</p></article></body></html>"
    return extract_html(html.encode(), url, today=TODAY, links=True).text


def test_a_flood_of_brackets_is_read_in_no_time():
    flood = "[" * 30_000 + "[![" * 5_000 + "." * 30_000
    started = time.perf_counter()
    text = linked(f"<p>{flood} https://a.example/x{')' * 30_000}</p>")
    cited(text + flood)
    assert time.perf_counter() - started < 3


def test_links_to_parts_of_the_page_are_no_notes_and_take_nothing_from_it():
    text = linked(
        '<p>Jump to: <a href="#v2024">2024</a> | <a href="#v2023">2023</a></p>'
        '<h2 id="v2024">2024</h2><p>Version 3.0 dropped Python 3.8 support this year.</p>'
        '<section id="v2023"><h2>2023</h2><p>Version 2.0 added Windows support.</p></section>'
        '<p>As <a href="#s4">Section 4</a> shows in <a href="#s4">4</a> and <a href="#f1">1</a>, '
        "our method cuts error by 40%.</p>"
        '<section id="s4"><p>The error fell from 10% to 6%.</p></section>'
        '<figure id="f1"><figcaption>Error by month. Photo: '
        '<a href="https://www.flickr.com/photos/someone/1">someone</a></figcaption></figure>'
        '<p>Glossary: <a href="#A">A</a> | <a href="#B">B</a></p>'
        '<div id="A"><p>Algol was defined in 1958 by a committee.</p></div>'
        '<div id="B"><p>BASIC was designed at Dartmouth in 1964.</p></div>'
    )
    for kept in (
        "Version 3.0 dropped Python 3.8 support this year.",
        "Version 2.0 added Windows support.",
        "The error fell from 10% to 6%.",
        "Algol was defined in 1958 by a committee.",
        "BASIC was designed at Dartmouth in 1964.",
    ):
        assert kept in text
    found = cited(text)
    assert "[^" not in text
    assert cited_by(found.text, "our method cuts error by 40%") == ()  # no photo credit


@pytest.mark.parametrize(
    "note",
    [
        '<p><a name="fn1"></a>See <a href="{source}">the bridge history</a>.</p>',
        '<div class="footnote"><a id="fn1" href="#ref1" class="footnote-number">1</a>'
        '<div class="footnote-content"><p>See <a href="{source}">the bridge history</a>.'
        "</p></div></div>",  # Substack: the id is on the note's own number
    ],
    ids=["named anchor", "substack"],
)
def test_a_note_cites_its_address_wherever_its_id_is(note):
    source = "https://bridges.example/history"
    text = linked(
        '<p>The bridge opened in 1932.<a id="ref1" class="footnote-anchor" href="#fn1">1</a></p>'
        + note.format(source=source)
    )
    found = cited(text)
    assert found.pages == {1: source}
    assert "The bridge opened in 1932 [1]." in found.text
    assert "the bridge history" not in found.text


def test_a_note_that_says_something_of_its_own_stays_in_the_text():
    tweet = "https://twitter.com/someone/status/1"
    text = linked(
        '<p>Planes can be designed before they fly.<sup><a href="#fn:1">1</a></sup></p>'
        '<div class="footnotes"><ol><li id="fn:1">Another one I commonly hear is that, unlike '
        "traditional engineers, programmers do things that have never been done before, as "
        f'<a href="{tweet}">someone said</a>. <a href="#fnref:1">\N{LEFTWARDS ARROW WITH HOOK}</a>'
        "</li></ol></div>"
    )
    found = cited(text)
    assert found.pages == {2: tweet}  # cited by the note's sentence, not the body's
    assert "Planes can be designed before they fly [1]." in found.text
    assert "programmers do things that have never been done before" in found.text


def test_notes_numbered_alike_never_take_one_anothers_label():
    a, b, c = "https://a.example/one", "https://b.example/jit", "https://c.example/rust"
    text = linked(
        '<p>Python 3.13 is out.<sup><a href="#fn1">1</a></sup> '
        'It has a JIT.<sup><a href="#fn2">2</a></sup></p>'
        f'<ol><li id="fn1"><a href="{a}">Release notes</a></li>'
        f'<li id="fn2"><a href="{b}">JIT notes</a></li></ol>'
        '<p>Rust 1.80 is out.<sup><a href="#2">1</a></sup></p>'
        f'<ol><li id="2"><a href="{c}">Rust release notes</a></li></ol>'
    )
    found = cited(text)
    assert found.pages == {1: a, 2: b, 3: c}
    assert "It has a JIT [2]." in found.text
    assert "Rust 1.80 is out [3]." in found.text


def test_links_in_a_list_item_keep_the_spaces_around_them():
    text = linked(
        "<ul><li>A computer language, initially developed by the "
        '<a href="https://www.defense.gov/">US Department of Defense</a>, is called '
        '<a href="https://en.wikipedia.org/wiki/Ada">Ada</a>, says '
        '<a href="https://history.example/ada">the history page</a>.</li></ul>'
    )
    assert (
        "- A computer language, initially developed by the US Department of Defense [1], is "
        "called Ada [2], says the history page [3]."
    ) in cited(text).text


def test_inline_code_read_with_links_is_code_and_cites_nothing():
    docs = "https://docs.python.org/3/tutorial/datastructures.html"
    found = cited(
        linked(
            "<p>Lists are written as <code>[1, <var>2</var>, 3]</code>, are mutable, and hold "
            f'<a href="{docs}">anything</a>.</p>'
        )
    )
    assert "Lists are written as `[1, 2, 3]`, are mutable, and hold anything [1]." in found.text
    assert cited_by(found.text, "Lists are written as") == (1,)
