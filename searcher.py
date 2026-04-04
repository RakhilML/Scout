"""
Web search and content gathering using DuckDuckGo (no API key required).

Pipeline:
  1. Search DuckDuckGo for the user's goal
  2. For each result URL, fetch and extract page text via fetcher.py
  3. Return list of PageContent objects for the LLM
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from typing import Optional

from ddgs import DDGS

from config import ScoutConfig
from exceptions import SearchError
from fetcher import fetch_page
from models import PageContent, SearchResult, FetchStatus

logger = logging.getLogger("scout.searcher")

# ─── DDG SEARCH ──────────────────────────────────────────────────────────────

def _ddg_search(query: str, max_results: int) -> list[SearchResult]:
    """
    Search DuckDuckGo for the query.
    Retries once on failure with a 2s delay.
    Raises SearchError if both attempts fail.
    """
    last_error: Optional[Exception] = None

    for attempt in range(2):
        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=max_results))
            results = []
            for i, r in enumerate(raw):
                results.append(SearchResult(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    snippet=r.get("body", ""),
                    rank=i + 1,
                ))
            if not results:
                raise SearchError(f"DuckDuckGo returned no results for: {query!r}")
            logger.debug("DDG returned %d results for %r", len(results), query)
            return results

        except SearchError:
            raise
        except Exception as e:
            last_error = e
            logger.warning("DDG search attempt %d failed: %s", attempt + 1, e)
            if attempt == 0:
                time.sleep(2.0)

    raise SearchError(f"DuckDuckGo search failed after 2 attempts: {last_error}")


# ─── PARALLEL FETCH ───────────────────────────────────────────────────────────

def _fetch_one(result: SearchResult, cfg: ScoutConfig) -> PageContent:
    """Fetch a single search result page. Always returns a PageContent (never raises)."""
    if not result.url:
        return PageContent(
            title=result.title,
            url="",
            snippet=result.snippet,
            full_text=result.snippet,
            fetch_status=FetchStatus.SNIPPET_ONLY,
            fetch_error="No URL",
        )

    fetch = fetch_page(
        url=result.url,
        user_agent=cfg.user_agent,
        timeout=cfg.fetch_timeout,
        retries=cfg.fetch_retries,
        max_chars=cfg.max_page_chars,
        snippet_fallback=result.snippet,
    )

    return PageContent(
        title=result.title,
        url=result.url,
        snippet=result.snippet,
        full_text=fetch.text or result.snippet,
        fetch_status=fetch.status,
        fetch_error=fetch.error or None,
    )


def _fetch_all_parallel(results: list[SearchResult], cfg: ScoutConfig) -> list[PageContent]:
    """
    Fetch all search results in parallel using a thread pool.
    Max 4 workers — enough to speed things up without hammering servers.

    Uses a wall-clock timeout on the entire batch. Any future that hasn't
    completed by then gets a snippet-only fallback — no None slots ever.
    """
    if not results:
        return []

    pages: list[Optional[PageContent]] = [None] * len(results)
    max_workers = min(4, len(results))
    wall_timeout = cfg.fetch_timeout * max(1, cfg.fetch_retries) + 20

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_fetch_one, result, cfg): i
            for i, result in enumerate(results)
        }
        try:
            for future in as_completed(future_to_idx, timeout=wall_timeout):
                idx = future_to_idx[future]
                try:
                    pages[idx] = future.result(timeout=3)
                except Exception as e:
                    r = results[idx]
                    logger.warning("Fetch failed for %s: %s", r.url, e)
                    pages[idx] = PageContent(
                        title=r.title,
                        url=r.url,
                        snippet=r.snippet,
                        full_text=r.snippet,
                        fetch_status=FetchStatus.ERROR,
                        fetch_error=str(e),
                    )
        except TimeoutError:
            # Wall timeout hit — cancel any remaining futures
            for future, idx in future_to_idx.items():
                if not future.done():
                    future.cancel()
                    logger.warning("Fetch timed out (wall clock) for %s", results[idx].url)

    # Fill any remaining None slots (futures that were cancelled or never ran)
    for i, page in enumerate(pages):
        if page is None:
            r = results[i]
            pages[i] = PageContent(
                title=r.title,
                url=r.url,
                snippet=r.snippet,
                full_text=r.snippet,
                fetch_status=FetchStatus.TIMEOUT,
                fetch_error="Cancelled — wall-clock timeout exceeded",
            )

    return pages  # type: ignore[return-value]


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def gather(goal: str, cfg: ScoutConfig) -> list[PageContent]:
    """
    Full gather pipeline:
    1. Search DDG for goal
    2. Fetch each result page in parallel
    3. Return list of PageContent ordered by search rank

    Raises SearchError if DDG fails completely.
    """
    logger.info("Searching DuckDuckGo for: %r", goal)
    results = _ddg_search(goal, cfg.max_results)

    logger.info("Fetching %d pages...", len(results))
    pages = _fetch_all_parallel(results, cfg)

    successful = sum(1 for p in pages if p.has_real_content)
    logger.info("Fetched %d/%d pages with real content", successful, len(pages))

    return pages


def gather_stats(pages: list[PageContent]) -> dict:
    """Return a summary dict of fetch statistics."""
    from collections import Counter
    status_counts = Counter(p.fetch_status.value for p in pages)
    return {
        "total": len(pages),
        "successful": sum(1 for p in pages if p.has_real_content),
        "statuses": dict(status_counts),
        "total_chars": sum(p.char_count for p in pages),
        "avg_chars": int(sum(p.char_count for p in pages) / len(pages)) if pages else 0,
    }
