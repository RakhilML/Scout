"""Hostnames, URL canonicalization, and sites that never serve article text to scripts."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Hard paywalls and login walls: a script only ever gets a teaser or a sign-in page.
DEFAULT_SKIP_DOMAINS = frozenset(
    {
        "bloomberg.com",
        "economist.com",
        "ft.com",
        "hbr.org",
        "linkedin.com",
        "newyorker.com",
        "nytimes.com",
        "quora.com",
        "reuters.com",
        "theatlantic.com",
        "thetimes.co.uk",
        "washingtonpost.com",
        "wired.com",
        "wsj.com",
    }
)

# Query parameters that only track clicks; dropping them lets duplicate results collapse.
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "msclkid", "yclid"})

# Search-engine ad and click-tracking links (domain, path prefix). Search backends sometimes
# return sponsored results as these redirects; they lead to a shop's ad landing page, not to
# a result. Seen in practice: www.bing.com/aclick?ld=... for "cheapest RTX 5090 deal".
_AD_LINKS = (
    ("bing.com", "/aclick"),
    ("bing.com", "/ck/a"),
    ("duckduckgo.com", "/y.js"),
    ("duckduckgo.com", "/l/"),
    ("google.com", "/aclk"),
    ("google.com", "/url"),
    ("r.search.yahoo.com", "/"),
    ("yandex.ru", "/clck"),
    ("yandex.com", "/clck"),
)


def hostname(url: str) -> str:
    """Lower-case host of *url* without port or a leading ``www.``; empty if there is none."""
    host = (urlsplit(url).hostname or "").rstrip(".")
    return host.removeprefix("www.")


def matches(host: str, domain: str) -> bool:
    """True if *host* is *domain* itself or one of its subdomains."""
    domain = domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def matches_any(url: str, domains: Iterable[str]) -> bool:
    host = hostname(url)
    return bool(host) and any(matches(host, domain) for domain in domains)


def is_ad_link(url: str) -> bool:
    """True for a search engine's sponsored-result or click-tracking redirect."""
    host = hostname(url)
    path = urlsplit(url).path
    return any(matches(host, domain) and path.startswith(prefix) for domain, prefix in _AD_LINKS)


def canonical_url(url: str) -> str:
    """Stable form of *url* for de-duplication: no fragment, tracking params or trailing slash."""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))
