"""Turn downloaded bytes into clean text, metadata, and schema.org offers."""

from __future__ import annotations

import copy
import io
import json
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import trafilatura
from lxml.html import HtmlElement
from price_parser import Price
from pypdf import PdfReader
from trafilatura import load_html

from scout.errors import ExtractionError
from scout.textutil import clean, rate_unit

log = logging.getLogger(__name__)

# Raised whenever extraction reads pages differently, so cached pages are read again.
EXTRACTOR_VERSION = 5
# Inline code, which trafilatura reads as code blocks: "OLD and NEW" would come out "OLD andNEW".
_INLINE_CODE = "//code[not(ancestor::pre)] | //kbd | //samp | //tt | //var"

_EARLIEST_PLAUSIBLE_DATE = date(1995, 1, 1)
_MAX_PDF_PAGES = 40
_PRODUCT_TYPES = frozenset({"Product", "ProductGroup", "IndividualProduct", "ProductModel"})
_OFFER_TYPES = frozenset({"Offer", "AggregateOffer"})
_FIELD = re.compile(r"(?:- )?[^\s:-][^:\n]{0,38}:")
_WRAPPED_LINE = 50  # source-wrapped prose runs to 72-80 columns
_REPEATABLE = 80  # shorter lines (labels, "Read more") legitimately repeat
_CURRENCY = re.compile(r"[A-Z]{3}")
# UN/CEFACT codes that schema.org's unitCode uses for the time a price is charged per.
_UNIT_CODES = {"HUR": "hour", "DAY": "day", "WEE": "week", "MON": "month", "ANN": "year"}
_ARTICLE_TYPES = frozenset(
    {
        "Article",
        "NewsArticle",
        "BlogPosting",
        "TechArticle",
        "Report",
        "ScholarlyArticle",
        "WebPage",
    }
)
# Meta tags (property, name or itemprop) that state when a page was published or changed.
_PUBLISHED_META = frozenset(
    {
        "article:published_time",
        "og:published_time",
        "datepublished",
        "date",
        "dc.date",
        "dcterms.created",
        "pubdate",
        "publishdate",
    }
)
_UPDATED_META = frozenset(
    {"article:modified_time", "og:updated_time", "datemodified", "dcterms.modified"}
)
_URL_DATE = re.compile(r"/((?:19|20)\d{2})/(0?[1-9]|1[0-2])/(?:(0?[1-9]|[12]\d|3[01])/)?")


@dataclass(frozen=True, slots=True)
class Offer:
    """A price the page publishes as schema.org data, as opposed to one guessed from prose."""

    product: str | None
    price: Decimal
    currency: str | None = None
    high_price: Decimal | None = None  # upper bound of an AggregateOffer range
    availability: str | None = None  # schema.org term, e.g. "InStock"
    url: str | None = None
    unit: str | None = None  # what a rate is charged per ("hour"); None for a price tag

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "price": str(self.price),
            "currency": self.currency,
            "high_price": str(self.high_price) if self.high_price is not None else None,
            "availability": self.availability,
            "url": self.url,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Offer:
        return cls(
            product=data.get("product"),
            price=Decimal(data["price"]),
            currency=data.get("currency"),
            high_price=Decimal(data["high_price"]) if data.get("high_price") else None,
            availability=data.get("availability"),
            url=data.get("url"),
            unit=data.get("unit"),
        )


@dataclass(frozen=True, slots=True)
class Extracted:
    text: str
    title: str | None = None
    site: str | None = None
    author: str | None = None
    published: date | None = None
    updated: date | None = None
    offers: tuple[Offer, ...] = ()


def extract_html(data: bytes, url: str, *, today: date) -> Extracted:
    tree = load_html(data)
    if tree is None:
        raise ExtractionError("not parseable as HTML")
    entities = list(_jsonld_entities(tree))
    readable = copy.deepcopy(tree)
    for element in readable.xpath(_INLINE_CODE):
        element.drop_tag()
    try:
        doc = trafilatura.bare_extraction(
            readable,
            url=url,
            with_metadata=True,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception as exc:  # a third-party parser meeting untrusted markup
        raise ExtractionError(f"text extraction failed: {exc}") from exc

    published, updated = _page_dates(tree, url, entities)
    title = _first_text(doc.title if doc is not None else None, tree.findtext(".//title"))
    site = _first_text(doc.sitename if doc is not None else None)
    return Extracted(
        text=_rejoin(clean(doc.text or "")) if doc is not None else "",
        title=title,
        site=site if site != title else None,  # some pages name the article as the site
        author=_first_text(doc.author if doc is not None else None),
        published=_plausible(published, today),
        updated=_plausible(updated, today),
        offers=_offers(entities),
    )


def _rejoin(text: str) -> str:
    """Put back together what extraction splits: a field and its value ("- Created:" then
    "- 08-Jul-2024") and prose hard-wrapped in the page's source. Repeated paragraphs go."""
    lines: list[str] = []
    seen: set[str] = set()
    for line in text.split("\n"):
        if len(line) >= _REPEATABLE and line in seen:
            continue
        seen.add(line)
        previous = lines[-1] if lines else ""
        if _FIELD.fullmatch(previous) and line and not _FIELD.fullmatch(line):
            lines[-1] = f"{previous} {line.removeprefix('- ')}"
        elif (
            len(previous) >= _WRAPPED_LINE
            and not previous.startswith("- ")
            and previous[-1] not in ".!?:;"
            and line[:1].islower()
        ):
            lines[-1] = f"{previous} {line}"
        else:
            lines.append(line)
    return "\n".join(lines)


def extract_pdf(data: bytes, *, today: date) -> Extracted:
    try:
        reader = PdfReader(io.BytesIO(data))
        # Many PDFs are "encrypted" with an empty user password and open fine.
        readable = not reader.is_encrypted or bool(reader.decrypt(""))
        pages = (
            [page.extract_text() or "" for page in reader.pages[:_MAX_PDF_PAGES]]
            if readable
            else []
        )
        info = reader.metadata if readable else None
    except Exception as exc:  # malformed PDFs raise anything from KeyError to struct.error
        raise ExtractionError(f"unreadable PDF: {exc}") from exc
    if not readable:
        raise ExtractionError("PDF is password-protected")

    title = _first_text(info.title) if info is not None else None
    return Extracted(
        text=clean("\n\n".join(pages)),
        title=title,
        published=_plausible(_pdf_creation_date(info), today),
    )


def extract_plain_text(data: bytes, encoding: str) -> Extracted:
    return Extracted(text=clean(data.decode(encoding, errors="replace")))


def _jsonld_entities(tree: HtmlElement) -> Iterator[dict[str, Any]]:
    for raw in tree.xpath('//script[contains(@type, "ld+json")]/text()'):
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            log.debug("skipping a malformed JSON-LD block")
            continue
        yield from _top_level(data)


def _top_level(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _top_level(item)
    elif isinstance(node, dict):
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from _top_level(graph)
        else:
            yield node


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every JSON object in *node*, parents before children."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key != "@context":
                yield from _walk(value)


def _types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {_schema_term(value) for value in values if isinstance(value, str)}


def _offers(entities: Iterable[dict[str, Any]]) -> tuple[Offer, ...]:
    found: list[Offer] = []
    done: set[int] = set()  # offer nodes already read as part of a product or an aggregate
    for node in _walk(list(entities)):
        types = _types(node)
        if types & _PRODUCT_TYPES:
            product = _first_text(node.get("name"))
            for offer_node in _as_list(node.get("offers")):
                found += _offer_tree(offer_node, product=product, unit=None, done=done)
        elif types & _OFFER_TYPES:
            offered = node.get("itemOffered")
            product = _first_text(offered.get("name")) if isinstance(offered, dict) else None
            found += _offer_tree(node, product=product, unit=None, done=done)
    return tuple(found)


def _offer_tree(node: Any, *, product: str | None, unit: str | None, done: set[int]) -> list[Offer]:
    """An offer and the offers nested in it: an AggregateOffer's inherit its product and unit."""
    if not isinstance(node, dict) or id(node) in done:
        return []
    done.add(id(node))
    unit = _price_unit(node) or unit
    offer = _parse_offer(node, product=product, unit=unit)
    nested = [
        found
        for child in _as_list(node.get("offers"))
        for found in _offer_tree(child, product=product, unit=unit, done=done)
    ]
    return ([offer] if offer is not None else []) + nested


def _parse_offer(node: dict[str, Any], *, product: str | None, unit: str | None) -> Offer | None:
    spec = _price_specification(node)
    price = _decimal(node.get("price", node.get("lowPrice"))) or _decimal(spec.get("price"))
    if price is None:
        return None
    currency = _first_text(node.get("priceCurrency"), spec.get("priceCurrency"))
    availability = node.get("availability")
    return Offer(
        product=product,
        price=price,
        currency=currency.upper() if currency and _CURRENCY.fullmatch(currency.upper()) else None,
        high_price=_decimal(node.get("highPrice")),
        availability=_schema_term(availability) if isinstance(availability, str) else None,
        url=_first_text(node.get("url")),
        unit=unit,
    )


def _price_unit(node: dict[str, Any]) -> str | None:
    """What an offer's price is charged per, from its UnitPriceSpecification ("HUR": an hour)."""
    spec = _price_specification(node)
    code = _first_text(spec.get("unitCode"))
    if code and code.upper() in _UNIT_CODES:
        return _UNIT_CODES[code.upper()]
    text = _first_text(spec.get("unitText"))
    return rate_unit(text) if text else None


def _price_specification(node: dict[str, Any]) -> dict[str, Any]:
    spec = node.get("priceSpecification")
    if isinstance(spec, list):
        spec = spec[0] if spec else None
    return spec if isinstance(spec, dict) else {}


def _page_dates(
    tree: HtmlElement, url: str, entities: Iterable[dict[str, Any]]
) -> tuple[date | None, date | None]:
    """(published, updated) as the page states them: JSON-LD, meta tags, <time>, or the URL.

    Nothing is guessed from other numbers in the markup: date guessers read cache-busting
    timestamps as publication dates (python.org pages came out as 2015-03-13).
    """
    published, updated = _article_dates(entities)
    if published is None and updated is None:
        published, updated = _meta_dates(tree)
    if published is None:
        published = _time_element_date(tree)
    if published is None and updated is None:
        published = _url_date(url)
    return published, updated


def _article_dates(entities: Iterable[dict[str, Any]]) -> tuple[date | None, date | None]:
    for node in entities:
        if _types(node) & _ARTICLE_TYPES:
            published = _parse_date(node.get("datePublished"))
            updated = _parse_date(node.get("dateModified"))
            if published or updated:
                return published, updated
    return None, None


def _meta_dates(tree: HtmlElement) -> tuple[date | None, date | None]:
    published = updated = None
    for meta in tree.iter("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        value = _parse_date(meta.get("content"))
        if value is None:
            continue
        if key in _PUBLISHED_META and published is None:
            published = value
        elif key in _UPDATED_META and updated is None:
            updated = value
    return published, updated


def _time_element_date(tree: HtmlElement) -> date | None:
    for element in tree.xpath(
        '//time[@itemprop="datePublished"] | //time[@pubdate] | //article//time[@datetime]'
    ):
        found = _parse_date(element.get("datetime") or element.get("content"))
        if found is not None:
            return found
    return None


def _url_date(url: str) -> date | None:
    """A /YYYY/MM/ or /YYYY/MM/DD/ path segment, as blogs and news sites use."""
    match = _URL_DATE.search(urlsplit(url).path)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3] or 1))
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    """A positive, finite amount from a JSON number or a price string; None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        amount: Decimal | None = Decimal(str(value))
    elif isinstance(value, str):
        amount = Price.fromstring(value).amount
    else:
        return None
    if amount is None or not amount.is_finite() or amount <= 0:
        return None
    return amount


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _plausible(day: date | None, today: date) -> date | None:
    """Drop dates that cannot be a web page's publication date (a common extraction error)."""
    if day is None or day < _EARLIEST_PLAUSIBLE_DATE or day > today + timedelta(days=1):
        return None
    return day


def _pdf_creation_date(info: Any) -> date | None:
    if info is None:
        return None
    try:
        created = info.creation_date
    except (ValueError, TypeError):  # malformed D:YYYYMMDD strings are common
        return None
    return created.date() if created is not None else None


def _schema_term(value: str) -> str:
    return value.rsplit("/", 1)[-1] if value.startswith(("http://", "https://")) else value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return clean(value)
    return None
