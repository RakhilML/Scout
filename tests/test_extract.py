from datetime import date
from decimal import Decimal

import pytest

from scout.errors import ExtractionError
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
