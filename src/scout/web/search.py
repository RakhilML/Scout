"""Web search backends, a cache in front of them, and merging results from several queries."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import requests
from ddgs import DDGS
from ddgs.exceptions import DDGSException

from scout.errors import ConfigError, SearchError
from scout.textutil import clean, fold
from scout.web.domains import canonical_url, hostname, is_ad_link

log = logging.getLogger(__name__)

# How far back a search may look: the ddgs `timelimit` codes.
RECENCY_CODES = {"day": "d", "week": "w", "month": "m", "year": "y"}


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    rank: int  # 1-based position in the result list of `query`
    query: str
    published: date | None = None  # news results carry a date
    source: str | None = None  # news outlet, when the engine names it

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "rank": self.rank,
            "query": self.query,
            "published": self.published.isoformat() if self.published else None,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchHit:
        published = data.get("published")
        return cls(
            url=data["url"],
            title=data["title"],
            snippet=data["snippet"],
            rank=data["rank"],
            query=data["query"],
            published=date.fromisoformat(published) if published else None,
            source=data.get("source"),
        )


class SearchBackend(Protocol):
    name: str

    def search(
        self, query: str, *, max_results: int, region: str, recency: str | None, news: bool
    ) -> list[SearchHit]: ...

    def close(self) -> None: ...


class DDGSBackend:
    """Metasearch through the `ddgs` package (Bing, Brave, DuckDuckGo, Google, Mojeek, ...)."""

    name = "ddgs"

    def __init__(
        self,
        *,
        timeout: int = 10,
        engines: str = "auto",
        retry_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._timeout = timeout
        self._engines = engines
        self._retry_delay = retry_delay
        self._sleep = sleep

    def close(self) -> None:
        """Nothing to release: each search opens and closes its own client."""

    def search(
        self, query: str, *, max_results: int, region: str, recency: str | None, news: bool
    ) -> list[SearchHit]:
        for attempt in range(2):
            try:
                with DDGS(timeout=self._timeout) as ddgs:
                    run = ddgs.news if news else ddgs.text
                    rows = run(
                        query,
                        region=region,
                        safesearch="moderate",
                        timelimit=RECENCY_CODES.get(recency or ""),
                        max_results=max_results,
                        backend=self._engines,
                    )
            except DDGSException as exc:
                # ddgs reports rate limits and bot challenges as "No results found", so a
                # genuinely empty result and throttling look alike: pause once and retry.
                log.info("search %r failed (%s)", query, exc)
                if attempt == 0:
                    self._sleep(self._retry_delay)
                continue
            hits = (
                _hit(
                    query,
                    rank,
                    url=row.get("href") or row.get("url"),
                    title=row.get("title"),
                    snippet=row.get("body"),
                    published=row.get("date"),
                    source=row.get("source"),
                )
                for rank, row in enumerate(rows, start=1)
            )
            return [hit for hit in hits if hit is not None]
        return []


class SearxngBackend:
    """A SearXNG instance: self-hosted metasearch, through its JSON API."""

    name = "searxng"

    def __init__(self, base_url: str, *, timeout: float = 10.0, user_agent: str = "") -> None:
        self._url = base_url.rstrip("/") + "/search"
        self._timeout = timeout
        self._session = requests.Session()
        if user_agent:
            self._session.headers["User-Agent"] = user_agent

    def close(self) -> None:
        self._session.close()

    def search(
        self, query: str, *, max_results: int, region: str, recency: str | None, news: bool
    ) -> list[SearchHit]:
        params = {"q": query, "format": "json", "categories": "news" if news else "general"}
        language = language_tag(region)
        if language:
            params["language"] = language
        if recency in RECENCY_CODES:
            params["time_range"] = recency
        try:
            response = self._session.get(self._url, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise SearchError(f"cannot reach SearXNG at {self._url}: {exc}") from exc
        if response.status_code == 403:
            raise SearchError(
                f"SearXNG at {self._url} refused JSON: add json to search.formats in its "
                "settings.yml"
            )
        if not response.ok:
            raise SearchError(f"SearXNG at {self._url} answered HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:  # a login or captcha page in front of the instance
            raise SearchError(f"SearXNG at {self._url} did not answer with JSON") from exc
        rows = data.get("results", []) if isinstance(data, dict) else []
        hits = (
            _hit(
                query,
                rank,
                url=row.get("url"),
                title=row.get("title"),
                snippet=row.get("content"),
                published=row.get("publishedDate"),
            )
            for rank, row in enumerate(rows, start=1)
        )
        return [hit for hit in hits if hit is not None][:max_results]


def make_search_backend(spec: str, *, timeout: float, user_agent: str = "") -> SearchBackend:
    """The search backend SCOUT_SEARCH names: "ddgs" or "searxng:<instance URL>"."""
    kind, _, target = spec.partition(":")
    if kind == "ddgs" and not target:
        return DDGSBackend(timeout=int(timeout))
    if kind == "searxng" and target.startswith(("http://", "https://")):
        return SearxngBackend(target, timeout=timeout, user_agent=user_agent)
    raise ConfigError(f"SCOUT_SEARCH must be ddgs or searxng:<URL>, not {spec!r}")


def language_tag(region: str) -> str | None:
    """The language of a ddgs region code as a BCP 47 tag: "in-en" -> "en-IN"; None for all."""
    country, _, language = region.partition("-")
    if not language or country in ("", "wt"):
        return None
    return f"{language}-{country.upper()}"


def _hit(
    query: str,
    rank: int,
    *,
    url: Any,
    title: Any,
    snippet: Any,
    published: Any = None,
    source: Any = None,
) -> SearchHit | None:
    """A search result as engines report it (any field may be missing or odd), or None."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    day = None
    if isinstance(published, str):
        try:
            day = datetime.fromisoformat(published.replace("Z", "+00:00")).date()
        except ValueError:
            day = None
    return SearchHit(
        url=url,
        title=clean(str(title or "")),
        snippet=clean(str(snippet or "")),
        rank=rank,
        query=query,
        published=day,
        source=source if isinstance(source, str) else None,
    )


class SearchCache(Protocol):
    def get_search(self, key: str, *, newer_than: datetime) -> list[SearchHit] | None: ...

    def put_search(self, key: str, hits: Sequence[SearchHit], fetched_at: datetime) -> None: ...


class CachedSearch:
    """Reuse recent results: a resumed run sees the same hits; repeat runs spare the engines."""

    def __init__(
        self,
        backend: SearchBackend,
        cache: SearchCache,
        *,
        max_age: float = 3600.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name = backend.name
        self._backend = backend
        self._cache = cache
        self._max_age = max_age
        self._clock = clock

    def search(
        self, query: str, *, max_results: int, region: str, recency: str | None, news: bool
    ) -> list[SearchHit]:
        key = "|".join(
            (
                self.name,
                "news" if news else "web",
                region,
                recency or "any",
                str(max_results),
                query,
            )
        )
        now = self._clock()
        cached = self._cache.get_search(key, newer_than=now - timedelta(seconds=self._max_age))
        if cached is not None:
            return cached
        hits = self._backend.search(
            query, max_results=max_results, region=region, recency=recency, news=news
        )
        if hits:  # an empty list may just mean throttling; don't pin it
            self._cache.put_search(key, hits, now)
        return hits


def merge_hits(result_lists: Iterable[Sequence[SearchHit]], limit: int) -> list[SearchHit]:
    """Interleave several result lists by rank, up to *limit* hits.

    Ad redirects are dropped, and so are duplicates: the same URL, or the same title on the
    same site (one page reached through different URLs).
    """
    lists = [list(hits) for hits in result_lists]
    merged: list[SearchHit] = []
    seen: set[str] = set()
    for position in range(max((len(hits) for hits in lists), default=0)):
        for hits in lists:
            if position >= len(hits) or is_ad_link(hits[position].url):
                continue
            hit = hits[position]
            keys = {canonical_url(hit.url), f"{hostname(hit.url)}|{fold(hit.title)}"}
            if keys & seen:
                continue
            seen |= keys
            merged.append(hit)
            if len(merged) == limit:
                return merged
    return merged
