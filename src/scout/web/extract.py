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
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import trafilatura
from lxml.html import HtmlElement
from price_parser import Price
from pypdf import PdfReader
from trafilatura import load_html

from scout.errors import ExtractionError
from scout.textutil import clean, rate_unit
from scout.web.domains import hostname, matches, registrable_domain

log = logging.getLogger(__name__)

# Raised whenever extraction reads pages differently, so cached pages are read again.
EXTRACTOR_VERSION = 5
# Inline code, which trafilatura reads as code blocks: "OLD and NEW" would come out "OLD andNEW".
_INLINE_CODE = "//code[not(ancestor::pre)] | //kbd | //samp | //tt | //var"
# An in-page link's target longer than this is a section of the page, not a note.
_NOTE_CHARS = 1000
# What a link to a note reads: "[3]", "3", "[a]", "[note 1]", "*", a dagger.
_NOTE_MARK = re.compile(
    r"[\[(]?\s*(\d{1,4}|[^\W\d_]{1,3}|(?:note|nb|n)\s?\d{1,3}|[*\N{DAGGER}\N{DOUBLE DAGGER}]{1,3})"
    r"\s*[\])]?",
    re.IGNORECASE,
)
_NOTE_ROLES = frozenset({"doc-noteref", "doc-footnote", "doc-endnote", "doc-biblioentry"})
_NOTE_NAME = re.compile(r"note|fn|foot|ref|cite|bib", re.IGNORECASE)
_PARTS = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "section", "figure", "table", "nav", "header", "footer"}
    | {"main", "article", "aside", "body", "form"}
)
_HEADINGS = ".//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6"
_PROSE_WORD = re.compile(r"\b[a-z]{3,}\b")
_EXPLAINING_WORDS = 6  # a reference has few words of its own: "Archived from the original"
_NAMED = re.compile(r"[^\W\d_]{2}")  # a title or a name, not a catalogue number
_LABEL = re.compile(r"[^\w.-]+")  # what a footnote's label cannot hold
# A link as trafilatura writes it: brackets in its words escaped, an unusual address in <...>.
# No bare "[" in its words, so that a page of them is read in one pass, not one per "[".
_MD_LINK = re.compile(r"\[((?:\\.|[^\[\]\\])*)\]\((<(?:\\.|[^>\\])*>|[^)\s]*)\)")

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


def extract_html(data: bytes, url: str, *, today: date, links: bool = False) -> Extracted:
    """The page's main text and metadata. With *links*, the text keeps its citations: each
    link to another site as a Markdown link, and each in-page note it cites ("[3]") as a
    footnote leading to the web address the note gives."""
    tree = load_html(data)
    if tree is None:
        raise ExtractionError("not parseable as HTML")
    entities = list(_jsonld_entities(tree))
    readable = copy.deepcopy(tree)
    for element in readable.xpath(_INLINE_CODE):
        if element.getparent() is None:
            continue  # inside code already backticked
        if links:  # code stays code, which cites nothing: "[1, 2, 3]" is a list
            _backticked(element)
        element.drop_tag()
    notes: dict[str, str] = {}
    if links:
        # Wikipedia puts a stylesheet in its references, which would make every one too long.
        for element in readable.xpath("//style | //script"):
            element.drop_tree()
        notes = _notes(readable, url)
        for element in readable.xpath("//a[not(normalize-space(@href))]"):
            element.drop_tag()  # trafilatura writes "[words]" for a link to nowhere
    try:
        doc = trafilatura.bare_extraction(
            readable,
            url=url,
            with_metadata=True,
            include_comments=False,
            include_tables=True,
            include_links=links,
            favor_recall=True,
        )
    except Exception as exc:  # a third-party parser meeting untrusted markup
        raise ExtractionError(f"text extraction failed: {exc}") from exc

    text = clean(doc.text or "") if doc is not None else ""
    if links:
        text = _rejoin(_cited_links(text, _site(url)), links=True)
        text += "".join(
            f"\n[^{label}]: {address}"
            for label, address in notes.items()
            if f"[^{label}]" in text  # not a note cited only from what extraction left out
        )
    else:
        text = _rejoin(text)
    published, updated = _page_dates(tree, url, entities)
    title = _first_text(doc.title if doc is not None else None, tree.findtext(".//title"))
    site = _first_text(doc.sitename if doc is not None else None)
    return Extracted(
        text=text,
        title=title,
        site=site if site != title else None,  # some pages name the article as the site
        author=_first_text(doc.author if doc is not None else None),
        published=_plausible(published, today),
        updated=_plausible(updated, today),
        offers=_offers(entities),
    )


def _rejoin(text: str, *, links: bool = False) -> str:
    """Put back together what extraction splits: a field and its value ("- Created:" then
    "- 08-Jul-2024") and prose hard-wrapped in the page's source. Repeated paragraphs go.

    With *links*, a line is read as its words, links included, and a bullet left alone on
    its line takes the next: trafilatura keeps the source's line breaks around links."""
    lines: list[str] = []
    shown: list[str] = []  # each line's words
    seen: set[str] = set()
    for line in text.split("\n"):
        if len(line) >= _REPEATABLE and line in seen:
            continue
        seen.add(line)
        words = _MD_LINK.sub(r"\1", line) if links else line
        previous = shown[-1] if shown else ""
        wrapped = (
            len(lines[-1] if lines else "") >= _WRAPPED_LINE
            and not previous.startswith("- ")
            and previous[-1:] not in ".!?:;"
            and words[:1].islower()
        )
        if _FIELD.fullmatch(previous) and words and not _FIELD.fullmatch(words):
            line, words = line.removeprefix("- "), words.removeprefix("- ")
        elif not wrapped and not (links and previous == "-" and words):
            lines.append(line)
            shown.append(words)
            continue
        lines[-1] = f"{lines[-1]} {line}"
        shown[-1] = f"{previous} {words}"
    return "\n".join(lines)


def _backticked(element: HtmlElement) -> None:
    text = element.text_content()
    for child in list(element):
        element.remove(child)
    element.text = f"`{text}`" if text.strip() else text


def _notes(tree: HtmlElement, url: str) -> dict[str, str]:
    """Replace each link to a note on the page ("[3]" leading to its reference list) with a
    footnote "[^3]" when the note gives a web address, or a bare "[3]" when it gives none,
    and remove the notes. Returns the address of each footnote.

    Done before trafilatura reads the page: it resolves "#cite_note-3" against the site
    rather than the page, and would keep the reference list as text to check.
    """
    page, own = urldefrag(url)[0], _site(url)
    ids = _anchors(tree)
    found: dict[str, str] = {}
    taken: set[HtmlElement] = set()
    for link in list(tree.iter("a")):
        target = _in_page(ids, page, link.get("href") or "")
        if target is None or any(parent in taken for parent in link.iterancestors()):
            continue
        fragment, note = target[0], _note_of(target[1])
        mark = _NOTE_MARK.fullmatch(link.text_content().strip())
        if mark is None or note is None or _holds(note, link):
            continue
        if not (_noted(link) or _note_like(note) or _NOTE_NAME.search(fragment)):
            continue  # "Section 4", "2024" or "A" leading to a part of the page
        number = mark[1] if mark[1].isdigit() else None
        if _explains(note):  # an aside, which stays in the text: its links cite for it
            link.tail = (f"[{number}]" if number else "") + (link.tail or "")
            link.drop_tree()
            continue
        taken.add(note)
        address = _note_address(ids, note, page, own)
        if address is None:
            marker = f"[{number}]" if number else ""
        else:
            # Two lists of notes may both have a [1]: the second takes a label still free.
            label = number if number and found.get(number, address) == address else None
            label = _free(found, label or _LABEL.sub("-", fragment), address)
            found[label] = address
            marker = f"[^{label}]"
        link.tail = marker + (link.tail or "")
        link.drop_tree()
    for note in taken:
        if note.getparent() is not None:
            note.drop_tree()
    return found


def _free(found: dict[str, str], label: str, address: str) -> str:
    """*label*, or a variant of it, that names no other address."""
    variant, n = label, 1
    while found.get(variant, address) != address:
        n += 1
        variant = f"{label}-{n}"
    return variant


def _note_of(target: HtmlElement) -> HtmlElement | None:
    """The note a fragment leads to. Substack puts the id on the note's own number (a link
    back to the text): the note is what holds it. Headings, sections, figures and tables are
    parts of the page, never notes."""
    note: HtmlElement | None = target
    while note is not None and (
        note.tag == "a" or _NOTE_MARK.fullmatch(note.text_content().strip())
    ):
        note = note.getparent()
    if note is None or note.tag in _PARTS or note.xpath(_HEADINGS) or not _short(note):
        return None
    return note


def _noted(link: HtmlElement) -> bool:
    """*link* is marked as a footnote's: raised, or named so by its markup."""
    if link.xpath("ancestor-or-self::sup | .//sup"):
        return True
    rel, role = link.get("rel") or "", link.get("role") or ""
    return "footnote" in rel or role in _NOTE_ROLES or _named_note(link)


def _note_like(element: HtmlElement) -> bool:
    """*element* is marked as a note: an item of a numbered list, or named so by its markup."""
    parent = element.getparent()
    numbered = element.tag == "li" and parent is not None and parent.tag == "ol"
    return numbered or element.get("role") in _NOTE_ROLES or _named_note(element)


def _named_note(element: HtmlElement) -> bool:
    return bool(_NOTE_NAME.search(f"{element.get('id') or ''} {element.get('class') or ''}"))


def _explains(note: HtmlElement) -> bool:
    """*note* says something of its own ("Another one I often hear is ..."), beyond the
    authors, title, dates and links of a reference."""
    linked = sum(len(_PROSE_WORD.findall(link.text_content())) for link in note.iter("a"))
    return len(_PROSE_WORD.findall(note.text_content())) - linked >= _EXPLAINING_WORDS


def _note_address(
    ids: dict[str, HtmlElement], note: HtmlElement, page: str, own: str, hops: int = 2
) -> str | None:
    """The web address a note cites: its first link to another site whose words name
    something (a title, "the original"), or else a DOI. A link to another note on the page
    (Wikipedia's short citations, to their entry in the bibliography) is followed once.
    Links to the page's own site and bare identifiers (catalogue numbers) cite nothing."""
    doi = None
    further: list[HtmlElement] = []
    for link in note.iter("a"):
        href = link.get("href") or ""
        target = _in_page(ids, page, href)
        if target is not None:
            if hops > 1 and not _holds(target[1], link):
                further.append(target[1])
            continue
        address = _resolved(page, href)
        if urlsplit(address).scheme not in ("http", "https") or _site(address) == own:
            continue
        if _NAMED.search(link.text_content()):
            return address
        if doi is None and matches(hostname(address), "doi.org"):
            doi = address
    followed = (_note_address(ids, element, page, own, hops - 1) for element in further)
    return next((address for address in followed if address), doi)


def _anchors(tree: HtmlElement) -> dict[str, HtmlElement]:
    """What each fragment of the page's address leads to (an element by its id, or what holds
    an <a name>) when it is short enough to be a note. Each is measured once, however many
    links lead to it."""
    found: dict[str, HtmlElement] = {}
    for element in tree.xpath("//a[@name]"):
        if (parent := element.getparent()) is not None:
            found.setdefault(element.get("name"), parent)
    for element in tree.xpath("//*[@id]"):
        found.setdefault(element.get("id"), element)
    return {fragment: element for fragment, element in found.items() if _short(element)}


def _in_page(ids: dict[str, HtmlElement], page: str, href: str) -> tuple[str, HtmlElement] | None:
    """The fragment and element a link leads to when it leads to this page."""
    address, fragment = urldefrag(_resolved(page, href))
    if address != page or not fragment:
        return None
    target = ids.get(unquote(fragment))
    return (unquote(fragment), target) if target is not None else None


def _short(element: HtmlElement) -> bool:
    size = 0
    for piece in element.itertext():
        size += len(piece)
        if size > _NOTE_CHARS:
            return False
    return True


def _holds(element: HtmlElement, link: HtmlElement) -> bool:
    """*link* is in *element*: a link to where it stands ("#top") leads to no note."""
    return element is link or element in link.iterancestors()


def _resolved(page: str, href: str) -> str:
    """*href* as a browser reads it on *page*."""
    return urljoin(page, re.sub(r"[\t\n\r]", "", href).strip().replace(" ", "%20"))


def _cited_links(text: str, own: str) -> str:
    """*text* with its links to other sites kept for citations to read, and every other link
    (to the page's own site, e-mail, scripts) as its words: a fact-check never takes evidence
    from the checked page's site. A link's words lose their brackets, so "[2]" may cite as
    a marker and no bracket ends a link early."""

    def link(match: re.Match[str]) -> str:
        words = " ".join(re.sub(r"\\([\[\]])", r"\1", match[1]).split())
        target = match[2]
        address = re.sub(r"\\([\\<>])", r"\1", target[1:-1]) if target[:1] == "<" else target
        if urlsplit(address).scheme not in ("http", "https") or _site(address) == own:
            return words
        return f"[{words.replace('[', '').replace(']', '')}]({target})"

    return _MD_LINK.sub(link, text)


def _site(url: str) -> str:
    host = hostname(url)
    return registrable_domain(host) if host else ""


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
