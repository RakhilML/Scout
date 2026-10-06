import pytest

from scout.web.domains import (
    DEFAULT_SKIP_DOMAINS,
    canonical_url,
    hostname,
    is_ad_link,
    matches_any,
)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://www.wsj.com/articles/x", "wsj.com"),
        ("https://WIRED.com:443/story", "wired.com"),
        ("https://learn.microsoft.com/en-us/", "learn.microsoft.com"),
        ("https://web.dev/a", "web.dev"),
        ("not a url", ""),
    ],
)
def test_hostname_strips_only_the_www_prefix(url, host):
    assert hostname(url) == host


@pytest.mark.parametrize(
    "url",
    [
        "https://www.wsj.com/a",
        "https://www.washingtonpost.com/a",
        "https://www.wired.com/a",
        "https://blogs.ft.com/x",
    ],
)
def test_skip_list_catches_domains_the_old_code_missed(url):
    assert matches_any(url, DEFAULT_SKIP_DOMAINS)


@pytest.mark.parametrize(
    "url", ["https://learn.microsoft.com/a", "https://www.lyft.com/a", "https://draft.dev/a"]
)
def test_skip_list_no_longer_blocks_lookalike_suffixes(url):
    assert not matches_any(url, DEFAULT_SKIP_DOMAINS)


def test_canonical_url_collapses_tracking_and_fragments():
    a = canonical_url("https://Example.com/post/?utm_source=x&id=7&fbclid=abc#section")
    b = canonical_url("https://example.com/post?id=7")
    assert a == b == "https://example.com/post?id=7"


def test_canonical_url_keeps_meaningful_parameters():
    assert canonical_url("https://github.com/o/r?ref=main") == "https://github.com/o/r?ref=main"


@pytest.mark.parametrize(
    ("url", "is_ad"),
    [
        ("https://www.bing.com/aclick?ld=e8Go3oHweq0-k_q9", True),
        ("https://duckduckgo.com/y.js?ad_domain=newegg.com", True),
        ("https://www.google.com/aclk?sa=l&ai=abc", True),
        ("https://r.search.yahoo.com/_ylt=abc/RU=https%3a%2f%2fshop/", True),
        ("https://www.bing.com/search?q=rtx", False),
        ("https://blogs.bing.com/search/2026/new-features", False),
        ("https://www.newegg.com/p/pl?N=100007709", False),
    ],
)
def test_ad_links_are_recognized(url, is_ad):
    assert is_ad_link(url) is is_ad
