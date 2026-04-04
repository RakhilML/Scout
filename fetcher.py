"""
HTTP fetcher with retry logic, anti-bot headers, and multi-strategy content extraction.

Extraction priority:
  1. trafilatura  — best article text extraction
  2. BeautifulSoup — fallback manual extraction
  3. DDG snippet   — last resort if fetch fails entirely
"""
from __future__ import annotations

import re
import time
import logging
from typing import Optional
from urllib.parse import urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

# Suppress trafilatura's internal ERROR/WARNING logs — they're expected noise
# (many pages fail gracefully and we handle that ourselves)
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)
logging.getLogger("trafilatura.core").setLevel(logging.CRITICAL)
logging.getLogger("trafilatura.utils").setLevel(logging.CRITICAL)
logging.getLogger("trafilatura.htmlprocessing").setLevel(logging.CRITICAL)

from models import FetchStatus

logger = logging.getLogger("scout.fetcher")

# ─── HEADERS ─────────────────────────────────────────────────────────────────

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

# Sites known to block scrapers via Cloudflare/paywalls — skip immediately, use DDG snippet
# Add domains here if you keep seeing HTTP 403/429 for a site
BLOCKED_DOMAINS = {
    # Paywalls
    "reuters.com",
    "ft.com",
    "wsj.com",
    "nytimes.com",
    "bloomberg.com",
    "washingtonpost.com",
    "economist.com",
    "hbr.org",
    # Cloudflare-protected tech sites
    "overclock3d.net",
    "extremetech.com",
    "techspot.com",
    "techradar.com",
    "tomshardware.com",
    "tomsguide.com",
    # Login-gated
    "medium.com",
    "quora.com",
    "linkedin.com",
    # Deal sites (Cloudflare-protected)
    "slickdeals.net",
}

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _clean_text(text: str) -> str:
    """Normalize whitespace and strip common junk."""
    text = re.sub(r"[\r\n]{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    return text


def _extract_with_bs4(html: str, max_chars: int) -> str:
    """Fallback extraction using BeautifulSoup."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "aside", "form", "noscript", "iframe", "figure",
                     "button", "input", "select", "textarea"]):
        tag.decompose()

    # Try to find main content area first
    main = (
        soup.find("main") or
        soup.find("article") or
        soup.find(attrs={"role": "main"}) or
        soup.find(id=re.compile(r"(content|main|article|body)", re.I)) or
        soup.find(class_=re.compile(r"(content|main|article|post|entry)", re.I)) or
        soup.body
    )
    if main is None:
        main = soup

    text = main.get_text(separator="\n", strip=True)
    text = _clean_text(text)
    return text[:max_chars]


def _extract_with_trafilatura(html: str, url: str, max_chars: int) -> Optional[str]:
    """Primary extraction using trafilatura."""
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        favor_precision=False,
        favor_recall=True,
    )
    if not text or len(text.strip()) < 50:
        return None
    text = _clean_text(text)
    return text[:max_chars]


# ─── MAIN FETCH ───────────────────────────────────────────────────────────────

class FetchResult:
    __slots__ = ("text", "status", "error")

    def __init__(self, text: str, status: FetchStatus, error: str = ""):
        self.text = text
        self.status = status
        self.error = error


def fetch_page(
    url: str,
    user_agent: str,
    timeout: int,
    retries: int,
    max_chars: int,
    snippet_fallback: str = "",
) -> FetchResult:
    """
    Fetch a URL and extract its text content.
    Returns a FetchResult with text, status, and optional error string.
    """
    domain = _get_domain(url)

    # Skip known paywalled/blocking sites immediately
    if any(blocked in domain for blocked in BLOCKED_DOMAINS):
        logger.debug("Skipping blocked domain: %s", domain)
        return FetchResult(
            text=snippet_fallback,
            status=FetchStatus.BLOCKED,
            error=f"Domain {domain} is known to block scrapers",
        )

    headers = {**BASE_HEADERS, "User-Agent": user_agent}
    last_error = ""

    max_attempts = max(1, retries + 1)   # retries=2 → 3 attempts total

    for attempt in range(max_attempts):
        is_last = attempt == max_attempts - 1
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )

            if resp.status_code in (401, 403, 406, 429, 451):
                logger.debug("HTTP %d for %s", resp.status_code, url)
                return FetchResult(
                    text=snippet_fallback,
                    status=FetchStatus.BLOCKED,
                    error=f"HTTP {resp.status_code}",
                )

            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                if not is_last:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return FetchResult(
                    text=snippet_fallback,
                    status=FetchStatus.ERROR,
                    error=last_error,
                )

            html = resp.text
            if not html or len(html) < 100:
                return FetchResult(
                    text=snippet_fallback,
                    status=FetchStatus.SNIPPET_ONLY,
                    error="Empty response body",
                )

            # Try trafilatura first
            text = _extract_with_trafilatura(html, url, max_chars)
            if text:
                logger.debug("trafilatura ok: %d chars from %s", len(text), url)
                return FetchResult(text=text, status=FetchStatus.SUCCESS)

            # Fall back to BS4
            text = _extract_with_bs4(html, max_chars)
            if text and len(text) > 100:
                logger.debug("bs4 fallback: %d chars from %s", len(text), url)
                return FetchResult(text=text, status=FetchStatus.SUCCESS)

            # Both extractors returned garbage — use snippet
            return FetchResult(
                text=snippet_fallback,
                status=FetchStatus.SNIPPET_ONLY,
                error="Extractors returned no usable text",
            )

        except requests.exceptions.Timeout:
            last_error = "Request timed out"
            logger.debug("Timeout fetching %s (attempt %d/%d)", url, attempt + 1, max_attempts)
            if not is_last:
                time.sleep(1.0)
        except requests.exceptions.TooManyRedirects:
            return FetchResult(
                text=snippet_fallback,
                status=FetchStatus.ERROR,
                error="Too many redirects",
            )
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {e}"
            logger.debug("Connection error for %s: %s", url, e)
            if not is_last:
                time.sleep(1.5)
        except Exception as e:
            last_error = str(e)
            logger.debug("Unexpected error for %s: %s", url, e)
            if not is_last:
                time.sleep(1.0)
            # Don't break — exhaust remaining retries

    # All attempts exhausted
    status = FetchStatus.TIMEOUT if "timed out" in last_error else FetchStatus.ERROR
    return FetchResult(text=snippet_fallback, status=status, error=last_error)
