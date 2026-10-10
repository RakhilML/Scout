"""Pages that are gone though they answer: soft 404s.

Link checkers call a link alive when it answers HTTP 200, or 301 and then 200. Yet the commonest
rot answers just so: an old article redirected to its site's home page, a "Page not found"
template served with 200, an expired domain showing a domain-sale page. Read as the cited page,
such an answer blames the sentence citing it ("not found in [2]") for a dead link, and is never
looked up in the archive. A page found gone here becomes NOT_FOUND, with the reason as its error,
so that everything reading dead pages (factcheck.gone) sees it.

Where a redirect lands rules on its own: a home page, an error page, a domain-sale site. Wording
("Page not found") never does: it only makes a page a suspect, as does a redirect to an unrelated
path, and a suspect is gone only when the site answers a made-up address beside it the same way
(the "random address" test of Bar-Yossef et al.). A site that tells that address apart (a real
404), or a probe that fails, leaves the page as read. No model is asked.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scout.textutil import fold, shingles, shorten
from scout.web.archive import in_archive
from scout.web.domains import canonical_url, hostname, matches_any, registrable_domain
from scout.web.fetch import TRANSIENT_STATUSES, Document, Fetcher, FetchStatus

_SIMILAR = 0.8  # share of three-word runs two pages have in common to be one template
_HEAD = 300  # where an error template says so: its first characters

# A short link may lead to a home page on purpose.
_REDIRECTORS = (
    "bit.ly",
    "buff.ly",
    "doi.org",
    "goo.gl",
    "hdl.handle.net",
    "is.gd",
    "j.mp",
    "lnkd.in",
    "ow.ly",
    "purl.org",
    "rebrand.ly",
    "t.co",
    "tinyurl.com",
)
_PARKING = (
    "afternic.com",
    "atom.com",
    "bodis.com",
    "buydomains.com",
    "dan.com",
    "domainmarket.com",
    "hugedomains.com",
    "parkingcrew.net",
    "sedo.com",
    "sedoparking.com",
    "undeveloped.com",
)
_ERROR_NAMES = frozenset(
    {
        "error",
        "error-404",
        "error-page",
        "error404",
        "errorpage",
        "not-found",
        "not_found",
        "notfound",
        "page-not-found",
        "page_not_found",
        "pagenotfound",
    }
)
# A login, subscription or consent page stands before the page, not in its place.
_WALLS = frozenset(
    {
        "account",
        "accounts",
        "auth",
        "captcha",
        "challenge",
        "consent",
        "gdpr",
        "log-in",
        "login",
        "logon",
        "oauth",
        "paywall",
        "register",
        "session",
        "sign-in",
        "sign-up",
        "signin",
        "signup",
        "sso",
        "subscribe",
    }
)
_NEXT_KEYS = frozenset(
    {"continue", "dest", "destination", "next", "redirect", "redirect_uri", "return", "returnurl"}
)
_CODES = frozenset({"404", "410"})
_ERRORS = frozenset({"error", "errors", "http", "status"})
_STATUS_KEYS = frozenset({"code", "err", "error", "errorcode", "status", "statuscode"})
_CONTENT_KEYS = frozenset(
    {"p", "page_id", "id", "article", "story", "post", "item", "doc", "docid", "q", "s", "v"}
)
_HOME_NAMES = frozenset({"default", "home", "index"})
_NOT_CONTENT = frozenset({"hl", "lang", "ref", "referrer", "source", "via"})
_LANGUAGES = frozenset(
    {
        "ar",
        "cs",
        "da",
        "de",
        "en",
        "es",
        "fi",
        "fr",
        "it",
        "ja",
        "ko",
        "nl",
        "no",
        "pl",
        "pt",
        "ru",
        "sv",
        "tr",
        "uk",
        "zh",
    }
)
_LOCALE = re.compile(r"([a-z]{2})(?:[-_]([a-z]{2}))?")
_NOT_THERE = re.compile(
    r"\b(?:404|410)\b|\bnot found\b|\bno longer (?:available|exists)\b"
    r"|\b(?:does not|doesn't) exist\b|\b(?:could not|couldn't|cannot|can't) be found\b"
    r"|\bdomain\b.{0,40}\bfor sale\b|\bbuy this domain\b|\bparked domain\b"
)


def screened(
    fetcher: Fetcher, documents: Sequence[Document], *, deadline: float, links: bool = False
) -> list[Document]:
    """*documents* as read, each page that is gone though it answered made NOT_FOUND. The
    made-up addresses beside the suspects are read at once, under *deadline*, and as the pages
    were (*links*), so that their text compares."""
    found = list(documents)
    suspects: dict[int, str] = {}  # each suspect, and why it is gone if its site agrees
    for n, document in enumerate(documents):
        if not in_archive(document.url):  # a copy there is the archive's to tell
            found[n] = _moved(document)
            if _suspect(found[n]):
                suspects[n] = ""
    # A made-up address beside the page, then one a folder up: a site that sends any last
    # segment to the article its folder names ("/2015-07841/any-title") has not lost it.
    for depth in (1, 2):
        asked = {n: made for n in suspects if (made := probe(found[n].url, depth)) is not None}
        if not asked:
            break
        urls = list(dict.fromkeys(asked.values()))
        answers = fetcher.fetch_many(urls, deadline=deadline, links=links)
        answered = dict(zip(urls, answers, strict=True))
        for n, url in asked.items():
            why = _same_answer(found[n], answered[url])
            if why is None:
                del suspects[n]
            elif depth == 1:
                suspects[n] = why
    for n, why in suspects.items():
        found[n] = _gone(found[n], why)
    return found


def error_page(document: Document) -> bool:
    """*document* says it is not there, in its title or where its text starts ("Page not
    found", "This domain is for sale"). Never proof alone: an article may be about 404s."""
    title = fold(document.title or "")
    return bool(_NOT_THERE.search(title) or _NOT_THERE.search(fold(document.text[:_HEAD])))


def probe(url: str, depth: int = 1) -> str | None:
    """A made-up address beside *url*: its last *depth* path segments replaced by names no page
    has, keeping its extension and trailing slash; None when it has fewer segments than that
    (beyond the first). The same for every page in one folder, so the site is asked once, and
    a cache serves it again."""
    parts = urlsplit(url)
    path = parts.path
    slash = "/" if path.endswith("/") and path.strip("/") else ""
    segments = path.strip("/").split("/") if path.strip("/") else []
    if depth > 1 and len(segments) < depth:
        return None
    folder = "/".join(segments[: len(segments) - depth]) if segments else ""
    last = segments[-1] if segments else ""
    stem, dot, extension = last.rpartition(".")
    keep = f".{extension}" if dot and stem and extension.isalnum() and len(extension) <= 5 else ""
    folder = f"/{folder}" if folder else ""
    name = hashlib.sha256(f"{hostname(url)}{folder}/".encode()).hexdigest()[:12]
    made = "/".join([f"scout-{name}"] * (depth - 1) + [f"scout-{name}{keep}"])
    return urlunsplit((parts.scheme, parts.netloc, f"{folder}/{made}{slash}", "", ""))


def _moved(document: Document) -> Document:
    """*document*, gone when where it was redirected shows it: a domain-sale page, an error
    page, a home page. A failure at the end may be a site down for a while: no news."""
    final = document.final_url
    if final is None or not _redirected(document) or document.status in TRANSIENT_STATUSES:
        return document
    if matches_any(final, _PARKING) and not matches_any(document.url, _PARKING):
        return _gone(document, f"redirects to a domain-sale page, {final}")
    if _markers(final) - _markers(document.url):
        return _gone(document, f"redirects to an error page, {final}")
    if _wall(final):  # a login or consent page stands before the page, not in its place
        return document
    if _home(final) and not _home(document.url) and not matches_any(document.url, _REDIRECTORS):
        if _same_site(document.url, final):
            return _gone(document, f"redirects to its site's home page, {final}")
        if _deep(document.url):  # a section ("/firefox") may have become a site of its own
            return _gone(document, f"redirects to the home page of {hostname(final)}, {final}")
    return document


def _suspect(document: Document) -> bool:
    """A page read that may be gone: one saying so, or one its site redirected to an unrelated
    path (neither within the page's own path nor above it), unless to a wall the page may stand
    behind. Another site's page is no suspect unless it says so: a short link, or a site that
    moved, may lead anywhere."""
    final = document.final_url
    if not document.ok or matches_any(document.url, _REDIRECTORS):
        return False
    if error_page(document):
        return True
    if final is None or not _redirected(document):
        return False
    if not _same_site(document.url, final):  # a section that went to another site's home
        return _home(final) and not _home(document.url)
    cited, landed = _segments(document.url), _segments(final)
    shared = min(len(cited), len(landed))
    return cited[:shared] != landed[:shared] and not _wall(final)


def _same_answer(page: Document, answer: Document) -> str | None:
    """Why *page*, a suspect, is gone, when *answer* (the site's to a made-up address beside
    it) is the same: a redirect to where the page was sent, or the same text. None when not."""
    if not answer.ok:
        return None
    if _redirected(answer):
        final = page.final_url
        if final is not None and _redirected(page) and _same(final, answer.final_url or ""):
            return f"redirects where the site sends any unknown address, {final}"
        return None
    if not _alike(page.text, answer.text):
        return None
    title = f', "{shorten(page.title, 60)}"' if page.title else ""
    return f"shows what its site shows for any unknown address{title}"


def _gone(document: Document, why: str) -> Document:
    return Document(
        url=document.url,
        status=FetchStatus.NOT_FOUND,
        fetched_at=document.fetched_at,
        final_url=document.final_url,
        error=why,
    )


def _redirected(document: Document) -> bool:
    return document.final_url is not None and not _same(document.url, document.final_url)


def _same(a: str, b: str) -> bool:
    """*a* and *b* are one address, whatever their scheme, www, trailing slash, the case of the
    path, fragment, or a query that only tracks the reader or names a language: a site that
    drops "?ref=x" or "?lang=fr" from its home page has not lost a page."""
    return _address(a) == _address(b)


def _address(url: str) -> tuple[str, str, str]:
    parts = urlsplit(canonical_url(url))
    query = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(key, value) for key, value in query if key.lower() not in _NOT_CONTENT]
    return hostname(url), parts.path.lower(), urlencode(kept)


def _same_site(a: str, b: str) -> bool:
    return registrable_domain(hostname(a)) == registrable_domain(hostname(b))


def _home(url: str) -> bool:
    """*url* is a home page: "/", "/index.html", "/en/", "/en-us/home", with no query that
    names content ("?p=123" does; "?_ga=..." or "?amp=1" does not)."""
    if _content(url):
        return False
    rest = _segments(url)
    if rest and (locale := _LOCALE.fullmatch(rest[0])) and {*locale.groups()} & _LANGUAGES:
        rest = rest[1:]
    return not rest or (len(rest) == 1 and _stem(rest[0]) in _HOME_NAMES)


def _markers(url: str) -> set[str]:
    """What marks *url* as an error page: a path segment so named ("not-found", "error.html"),
    a last segment "404" or "410" at the root or under an error folder (elsewhere a number:
    bill 410), an ASP.NET error query (aspxerrorpath), or a status query of 404 or 410."""
    segments = [_stem(segment) for segment in _segments(url)]
    found = set(segments) & _ERROR_NAMES
    if segments and segments[-1] in _CODES and (len(segments) == 1 or segments[-2] in _ERRORS):
        found.add(segments[-1])
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.lower() == "aspxerrorpath":
            found.add("aspxerrorpath")
        if key.lower() in _STATUS_KEYS and value in _CODES:
            found.add(value)
    return found


def _deep(url: str) -> bool:
    """*url* names a page, not a section: two path segments beside its language, a file, or a
    query naming content. "/firefox" or "/en-US/thunderbird/" is a section."""
    rest = _segments(url)
    if rest and (locale := _LOCALE.fullmatch(rest[0])) and {*locale.groups()} & _LANGUAGES:
        rest = rest[1:]
    return len(rest) >= 2 or bool(rest and "." in rest[-1]) or _content(url)


def _content(url: str) -> bool:
    """*url* has a query naming content, not one that tracks the reader or names a language."""
    query = parse_qsl(urlsplit(canonical_url(url)).query, keep_blank_values=True)
    return any(key.lower() in _CONTENT_KEYS for key, _ in query)


def _wall(url: str) -> bool:
    """*url* is a page that stands before another: a login, a consent page, or any page told
    where to send the reader next ("/?next=/2019/depot")."""
    first = hostname(url).split(".")[0]
    query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    return (
        first in _WALLS
        or any(_stem(segment) in _WALLS for segment in _segments(url))
        or any(key.lower() in _NEXT_KEYS for key, _ in query)
    )


def _segments(url: str) -> list[str]:
    return [segment for segment in urlsplit(url).path.lower().split("/") if segment]


def _stem(segment: str) -> str:
    return segment.partition(".")[0]


def _alike(a: str, b: str) -> bool:
    first, second = shingles(a), shingles(b)
    return bool(first and second) and len(first & second) / len(first | second) >= _SIMILAR
