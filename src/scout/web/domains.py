"""Hostnames and the sites they belong to, private addresses, URL canonicalization, and sites
that never serve article text to scripts."""

from __future__ import annotations

import ipaddress
import socket
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

# Second-level labels under which country domains register names: bbc.co.uk, abc.net.au.
_SECOND_LEVELS = frozenset({"ac", "co", "com", "edu", "gov", "net", "or", "org"})

# Platforms whose subdomains are other people's sites: someone.substack.com is not substack.com.
_HOSTED = frozenset(
    {
        "blogspot.com",
        "github.io",
        "gitlab.io",
        "hashnode.dev",
        "medium.com",
        "neocities.org",
        "netlify.app",
        "pages.dev",
        "readthedocs.io",
        "substack.com",
        "tumblr.com",
        "vercel.app",
        "wordpress.com",
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


def registrable_domain(host: str) -> str:
    """The domain *host* belongs to, so that one site counts once: docs.python.org and
    peps.python.org are python.org, news.bbc.co.uk is bbc.co.uk. An IP address stays as it is.

    An approximation: Scout ships no public-suffix list, so a country domain counts as a suffix
    only after one of the common second levels (co.uk, com.au), and only the common hosting
    platforms (substack.com, github.io) keep their users' sites apart.
    """
    if _is_address(host):
        return host
    labels = host.split(".")
    country = len(labels[-1]) == 2 and len(labels) > 2 and labels[-2] in _SECOND_LEVELS
    kept = 3 if country or ".".join(labels[-2:]) in _HOSTED else 2
    return ".".join(labels[-kept:])


def private_address(url: str) -> str | None:
    """An address of *url*'s host that is not on the public internet (loopback, a private
    network, link-local cloud metadata), or None. A host that does not resolve is None: its
    fetch fails anyway."""
    host = urlsplit(url).hostname
    if not host:
        return None
    try:
        found = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            return None
        found = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in resolved]
    for address in found:
        # ::ffff:10.0.0.5 is 10.0.0.5; older Python releases call such addresses global.
        unwrapped = getattr(address, "ipv4_mapped", None) or address
        if not unwrapped.is_global:
            return str(unwrapped)
    return None


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


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
