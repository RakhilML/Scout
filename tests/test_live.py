"""Checks against the real network. Skipped by default; run with ``pytest -m live``."""

import pytest

from scout.web.fetch import Fetcher, FetchStatus
from scout.web.render import PlaywrightRenderer
from scout.web.search import DDGSBackend

pytestmark = pytest.mark.live


def test_search_returns_results():
    hits = DDGSBackend().search(
        "python 3.13 what's new", max_results=5, region="us-en", recency=None, news=False
    )
    assert hits
    assert all(hit.url.startswith("http") for hit in hits)


def test_brotli_served_page_is_readable():
    # Cloudflare serves brotli to clients that accept it; the old fetcher got binary junk here.
    with Fetcher() as fetcher:
        doc = fetcher.fetch("https://www.cloudflare.com/learning/what-is-cloudflare/")
    assert doc.status is FetchStatus.OK
    assert "Cloudflare" in doc.text


def test_pdf_is_read_as_text():
    with Fetcher() as fetcher:
        doc = fetcher.fetch("https://arxiv.org/pdf/1706.03762")
    assert (doc.status, doc.kind) == (FetchStatus.OK, "pdf")
    assert "attention" in doc.text.lower()


def test_script_built_pages_are_read_through_the_browser():
    pytest.importorskip("playwright")
    url = "https://quotes.toscrape.com/js/"
    with Fetcher() as plain:
        assert plain.fetch(url).status is FetchStatus.EMPTY
    renderer = PlaywrightRenderer(timeout=30)
    try:
        with Fetcher(renderer=renderer) as fetcher:
            doc = fetcher.fetch(url)
    finally:
        renderer.close()
    assert doc.status is FetchStatus.OK
    assert "Einstein" in doc.text
