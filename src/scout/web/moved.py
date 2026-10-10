"""Where a dead cited page moved, proven by what it held.

Sites that change their domain or their software drop the old addresses, or send every one to
the new home page, while the pages live on: often at the same path on the site the old address
now leads to, else somewhere on either site, where a search for a sentence of the page finds
them. A candidate is never taken on its address, slug or title, as a guess would be: it is the
page only when it holds most of what the page's archived copy held, and is mostly that (an index
page carrying the post among others holds it all, yet is another page).
"""

from __future__ import annotations

from collections.abc import Collection
from urllib.parse import urlsplit, urlunsplit

from scout.research.verify import span_of
from scout.textutil import clean, fold, shingles
from scout.web import soft404
from scout.web.archive import in_archive
from scout.web.domains import canonical_url, hostname, matches_any, registrable_domain
from scout.web.fetch import Document, Fetcher
from scout.web.search import SearchBackend

SAME_PAGE = 0.5  # share of the copy's three-word runs a candidate must hold
MOSTLY = 0.25  # share of the candidate's runs that must come from the copy
PHRASE_WORDS = 16  # words of a quote searched for at most
MIN_PHRASE_WORDS = 6  # fewer are on too many pages to find one
HITS = 3  # search results read per site
_DOUBLE_QUOTES = str.maketrans(
    dict.fromkeys('"\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}', " ")
)


def same_path(url: str, landed: str | None) -> str | None:
    """*url*'s path and query on the host it now redirects to (*landed*): where a site that
    changed its domain, or folded a subdomain into another, keeps its pages. None when it was
    not sent to another host, or when that address is where it was sent."""
    if landed is None or hostname(landed) == hostname(url):
        return None
    cited, there = urlsplit(url), urlsplit(landed)
    found = urlunsplit((there.scheme, there.netloc, cited.path, cited.query, ""))
    return None if canonical_url(found) == canonical_url(landed) else found


def phrase(copy_text: str, quote: str) -> str | None:
    """What to search for of *quote*, a quote verified on *copy_text*: its words as the copy
    writes them (the model's may differ by a quote mark or a dropped word, and an engine matches
    a phrase as written), at most PHRASE_WORDS, without the double quotes that would end the
    phrase. None when it is shorter than MIN_PHRASE_WORDS."""
    span = span_of(copy_text, fold(quote), min_digits=1)
    written = copy_text[span[0] : span[1]] if span else clean(quote)
    words = written.translate(_DOUBLE_QUOTES).split()
    return " ".join(words[:PHRASE_WORDS]) if len(words) >= MIN_PHRASE_WORDS else None


def sites(url: str, landed: str | None) -> list[str]:
    """The sites searched for the page at *url*: the one it now redirects to (*landed*), where
    a site that moved put it, then its own."""
    found = (registrable_domain(hostname(address)) for address in (landed, url) if address)
    return list(dict.fromkeys(site for site in found if site))


def same_page(copy_text: str, text: str) -> bool:
    """*text* is the page *copy_text* is a copy of: it holds most of the copy, and is mostly
    the copy."""
    copy, page = shingles(copy_text), shingles(text)
    held = len(copy & page)
    return bool(copy and page) and held >= SAME_PAGE * len(copy) and held >= MOSTLY * len(page)


def relocate(
    fetcher: Fetcher,
    search: SearchBackend,
    *,
    url: str,
    landed: str | None,
    copy_text: str,
    quote: str,
    own: Collection[str],
    region: str,
    deadline: float,
    search_too: bool,
) -> Document | None:
    """The live page that the dead one at *url*, whose archived copy reads *copy_text*, moved
    to, read; None when no candidate proves to be it. First the same path on the host *url* now
    redirects to (*landed*), at no search; then, with *search_too*, what a search for *quote* as
    the copy writes it finds on each of sites(). The checked text is never searched for, and a
    page on the sites *own* (the checked page's) is never a candidate: it is no evidence. Nor
    is a candidate gone though it answers (soft404). SearchError when the engine fails."""
    tried = {canonical_url(url)}

    def proven(candidates: list[str]) -> Document | None:
        fresh = []
        for candidate in candidates:
            if canonical_url(candidate) not in tried and not matches_any(candidate, own):
                tried.add(canonical_url(candidate))
                fresh.append(candidate)
        if not fresh:
            return None
        read = soft404.screened(
            fetcher, fetcher.fetch_many(fresh, deadline=deadline), deadline=deadline
        )
        return next((doc for doc in read if doc.ok and same_page(copy_text, doc.text)), None)

    there = same_path(url, landed)
    if there is not None and (found := proven([there])) is not None:
        return found
    said = phrase(copy_text, quote) if search_too else None
    for site in sites(url, landed) if said is not None else []:
        hits = search.search(
            f'"{said}" site:{site}', max_results=HITS, region=region, recency=None, news=False
        )
        candidates = [
            hit.url
            for hit in hits
            if registrable_domain(hostname(hit.url)) == site and not in_archive(hit.url)
        ]
        if (found := proven(candidates)) is not None:
            return found
    return None
