"""Download pages and turn them into Documents. One bad page never aborts a run."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from scout.clock import utcnow
from scout.errors import ExtractionError, RenderError
from scout.settings import DEFAULT_USER_AGENT
from scout.textutil import looks_like_junk
from scout.web.domains import DEFAULT_SKIP_DOMAINS, hostname, matches_any, private_address
from scout.web.extract import (
    EXTRACTOR_VERSION,
    Extracted,
    Offer,
    extract_html,
    extract_pdf,
    extract_plain_text,
)
from scout.web.render import Renderer

log = logging.getLogger(__name__)

# Below this much main text a page is navigation or a stub (unless it publishes offers).
_MIN_TEXT_CHARS = 200
_ACCEPT = "text/html,application/xhtml+xml,application/pdf;q=0.9,text/plain;q=0.8,*/*;q=0.5"


class FetchStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"  # on the skip list (paywall / login wall)
    BLOCKED = "blocked"  # 401/403/407/429/451: the site refuses scripts
    NOT_FOUND = "not_found"
    HTTP_ERROR = "http_error"  # other 4xx and redirect loops
    SERVER_ERROR = "server_error"  # 5xx
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    REFUSED = "refused"  # on a private network, when only public pages may be read
    UNSUPPORTED = "unsupported"  # a content type or file we cannot read
    TOO_LARGE = "too_large"
    JUNK = "junk"  # decoded to binary garbage
    EMPTY = "empty"  # no readable main text


TRANSIENT_STATUSES = frozenset(
    {FetchStatus.SERVER_ERROR, FetchStatus.TIMEOUT, FetchStatus.NETWORK_ERROR}
)


@dataclass(frozen=True, slots=True)
class Document:
    url: str
    status: FetchStatus
    fetched_at: datetime
    final_url: str | None = None
    kind: str | None = None  # "html" | "pdf" | "text"
    title: str | None = None
    site: str | None = None
    author: str | None = None
    published: date | None = None
    updated: date | None = None
    text: str = ""
    offers: tuple[Offer, ...] = ()
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    from_cache: bool = False
    error: str | None = None
    extractor: int = 0  # the EXTRACTOR_VERSION that read the page (0: older than versioning)

    @property
    def ok(self) -> bool:
        return self.status is FetchStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status.value,
            "fetched_at": self.fetched_at.isoformat(),
            "final_url": self.final_url,
            "kind": self.kind,
            "title": self.title,
            "site": self.site,
            "author": self.author,
            "published": self.published.isoformat() if self.published else None,
            "updated": self.updated.isoformat() if self.updated else None,
            "text": self.text,
            "offers": [offer.to_dict() for offer in self.offers],
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_hash": self.content_hash,
            "error": self.error,
            "extractor": self.extractor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        return cls(
            url=data["url"],
            status=FetchStatus(data["status"]),
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            final_url=data.get("final_url"),
            kind=data.get("kind"),
            title=data.get("title"),
            site=data.get("site"),
            author=data.get("author"),
            published=_date_or_none(data.get("published")),
            updated=_date_or_none(data.get("updated")),
            text=data.get("text", ""),
            offers=tuple(Offer.from_dict(item) for item in data.get("offers", [])),
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            content_hash=data.get("content_hash"),
            error=data.get("error"),
            extractor=data.get("extractor", 0),
        )


class PageCache(Protocol):
    def get_page(self, url: str) -> Document | None: ...

    def put_page(self, doc: Document) -> None: ...


@dataclass(frozen=True, slots=True)
class FetchConfig:
    timeout: float = 12.0
    retries: int = 1  # extra attempts, only for 5xx, timeouts and network errors
    user_agent: str = DEFAULT_USER_AGENT
    accept_language: str = "en-US,en;q=0.9"
    max_bytes: int = 10_000_000
    fresh_for: float = 3600.0  # reuse a cached page this many seconds without asking the server
    failed_fresh_for: float = 600.0  # transient failures are retried sooner
    skip_domains: frozenset[str] = DEFAULT_SKIP_DOMAINS
    public_only: bool = False  # refuse hosts on private networks, redirect hops included


# A backslash, a space or a control character makes URL parsers disagree on the host.
_AMBIGUOUS = re.compile(r"[\\\x00-\x20\x7f]")


class _BodyTooLarge(Exception):
    pass


class _BodyTooSlow(Exception):
    pass


class _PrivateHop(Exception):
    def __init__(self, url: str | None, address: str) -> None:
        super().__init__(address)
        self.url = url  # None: refused at the socket, wherever the address came from
        self.address = address


class _PublicPeer:
    """Refuses a connection whose peer is not on the public internet. Checked on the socket
    itself, so no spelling of an address, no second DNS answer and no redirect gets through,
    and nothing is sent before the check."""

    def _new_conn(self) -> socket.socket:
        sock: socket.socket = super()._new_conn()  # type: ignore[misc]
        address = ipaddress.ip_address(str(sock.getpeername()[0]).split("%")[0])
        unwrapped = getattr(address, "ipv4_mapped", None) or address
        if not unwrapped.is_global:
            sock.close()
            raise _PrivateHop(None, str(unwrapped))
        return sock


class _PublicHTTPConnection(_PublicPeer, HTTPConnection):
    pass


class _PublicHTTPSConnection(_PublicPeer, HTTPSConnection):
    pass


class _PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = _PublicHTTPConnection


class _PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class _PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _PublicHTTPPool,
            "https": _PublicHTTPSPool,
        }


class Fetcher:
    """Thread-safe page fetcher with a cache and conditional requests (ETag / Last-Modified)."""

    def __init__(
        self,
        config: FetchConfig | None = None,
        cache: PageCache | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        renderer: Renderer | None = None,
    ) -> None:
        self._config = config or FetchConfig()
        self._cache = cache
        self._clock = clock
        self._sleep = sleep
        self._renderer = renderer  # reads pages whose text only appears after their scripts run
        # Checked before each redirect is followed, so a private hop is never requested.
        self._hooks = {"response": _refuse_private_hop} if self._config.public_only else {}
        self._idle: queue.SimpleQueue[requests.Session] = queue.SimpleQueue()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def fetch(self, url: str) -> Document:
        now = self._clock()
        if matches_any(url, self._config.skip_domains):
            return Document(
                url=url,
                status=FetchStatus.SKIPPED,
                fetched_at=now,
                error="site only serves paywalled or login pages to scripts",
            )
        if self._config.public_only:
            if _AMBIGUOUS.search(url):
                return _refused(url, "its address is ambiguous", now)
            if (address := private_address(url)) is not None:
                return _private(url, _PrivateHop(url, address), now)
        cached = self._cache.get_page(url) if self._cache is not None else None
        if self._config.public_only and cached is not None and _landed_privately(cached):
            cached = None  # read earlier by a fetcher that may read private pages
        if cached is not None and cached.ok and cached.extractor != EXTRACTOR_VERSION:
            cached = None  # read by an older extractor: download and read it again
        if cached is not None and self._is_fresh(cached, now):
            return replace(cached, from_cache=True)

        doc = self._download(url, cached, now)
        if self._cache is not None and doc.status is not FetchStatus.REFUSED:
            self._cache.put_page(doc)
        return doc

    def fetch_many(
        self, urls: Sequence[str], *, workers: int = 4, deadline: float = 45.0
    ) -> list[Document]:
        """Fetch *urls* in parallel; whatever is unfinished at *deadline* seconds is abandoned."""
        if not urls:
            return []
        results: dict[int, Document] = {}
        pool = ThreadPoolExecutor(max_workers=min(workers, len(urls)), thread_name_prefix="fetch")
        futures = {pool.submit(self.fetch, url): index for index, url in enumerate(urls)}
        try:
            for future in as_completed(futures, timeout=deadline):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:  # a page that trips a library must not end the run
                    log.warning("reading %s failed: %r", urls[index], exc)
                    results[index] = Document(
                        url=urls[index],
                        status=FetchStatus.UNSUPPORTED,
                        fetched_at=self._clock(),
                        error=f"could not be read: {type(exc).__name__}",
                    )
        except FuturesTimeout:
            log.info(
                "fetch deadline (%.0fs) reached with %d page(s) pending",
                deadline,
                len(urls) - len(results),
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        now = self._clock()
        return [
            results.get(index)
            or Document(
                url=url,
                status=FetchStatus.TIMEOUT,
                fetched_at=now,
                error="abandoned at the fetch deadline",
            )
            for index, url in enumerate(urls)
        ]

    def _is_fresh(self, doc: Document, now: datetime) -> bool:
        limit = (
            self._config.failed_fresh_for
            if doc.status in TRANSIENT_STATUSES
            else self._config.fresh_for
        )
        return (now - doc.fetched_at).total_seconds() < limit

    @contextmanager
    def _session(self) -> Iterator[requests.Session]:
        """A session no other thread is using, kept for the next download (and its connections)."""
        try:
            session = self._idle.get_nowait()
        except queue.Empty:
            session = requests.Session()
            if self._config.public_only:
                session.trust_env = False  # a proxy would hide which host is reached
                for scheme in ("http://", "https://"):
                    session.mount(scheme, _PublicAdapter())
            # Accept-Encoding is left to urllib3, which only offers br/zstd when it can decode them.
            session.headers.update(
                {
                    "User-Agent": self._config.user_agent,
                    "Accept": _ACCEPT,
                    "Accept-Language": self._config.accept_language,
                }
            )
            with self._sessions_lock:
                self._sessions.append(session)
        try:
            yield session
        finally:
            self._idle.put(session)

    def _download(self, url: str, cached: Document | None, now: datetime) -> Document:
        with self._session() as session:
            headers = _revalidation_headers(cached)
            failure = Document(url=url, status=FetchStatus.NETWORK_ERROR, fetched_at=now)
            for attempt in range(self._config.retries + 1):
                if attempt:
                    self._sleep(min(4.0, 0.75 * 2 ** (attempt - 1)))
                try:
                    with session.get(
                        url,
                        headers=headers,
                        timeout=self._config.timeout,
                        stream=True,
                        hooks=self._hooks,
                    ) as resp:
                        if resp.status_code == 304 and cached is not None:
                            return replace(cached, fetched_at=now, from_cache=True)
                        status = _status_for_http(resp.status_code)
                        if status is None:
                            return self._read(url, resp, now)
                        failure = Document(
                            url=url,
                            status=status,
                            fetched_at=now,
                            final_url=resp.url,
                            error=f"HTTP {resp.status_code}",
                        )
                        if status is not FetchStatus.SERVER_ERROR:
                            return failure
                except _PrivateHop as hop:
                    return _private(url, hop, now)
                except requests.TooManyRedirects:
                    return Document(
                        url=url,
                        status=FetchStatus.HTTP_ERROR,
                        fetched_at=now,
                        error="redirect loop",
                    )
                except requests.Timeout:
                    failure = Document(
                        url=url,
                        status=FetchStatus.TIMEOUT,
                        fetched_at=now,
                        error=f"no response within {self._config.timeout:g}s",
                    )
                except requests.RequestException as exc:  # DNS, TLS, refused, reset, bad encoding
                    failure = Document(
                        url=url,
                        status=FetchStatus.NETWORK_ERROR,
                        fetched_at=now,
                        error=_describe(exc),
                    )
            return failure

    def _read(self, url: str, resp: requests.Response, now: datetime) -> Document:
        def failed(status: FetchStatus, error: str) -> Document:
            return Document(url=url, status=status, fetched_at=now, final_url=resp.url, error=error)

        try:
            body = _read_body(resp, self._config.max_bytes, self._config.timeout * 3)
        except _BodyTooLarge:
            return failed(FetchStatus.TOO_LARGE, f"larger than {self._config.max_bytes:,} bytes")
        except _BodyTooSlow:
            return failed(FetchStatus.TIMEOUT, "body download too slow")

        media_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        kind = _content_kind(media_type, body)
        if kind is None:
            return failed(
                FetchStatus.UNSUPPORTED, f"cannot read content type {media_type or 'unknown'!r}"
            )
        try:
            extracted = _extract(kind, body, resp, today=now.date())
        except ExtractionError as exc:
            return failed(FetchStatus.UNSUPPORTED, str(exc))
        except Exception as exc:  # parsers meeting hostile bytes raise anything
            log.warning("extracting %s failed: %r", url, exc)
            return failed(FetchStatus.UNSUPPORTED, f"could not be read: {type(exc).__name__}")

        if looks_like_junk(extracted.text):
            return failed(FetchStatus.JUNK, "content decoded to binary garbage")
        if _readable(extracted):
            return _document(url, now, kind, extracted, resp)
        if kind == "html" and self._renderer is not None:
            return _rendered(self._renderer, url, now, resp)
        return failed(FetchStatus.EMPTY, "no readable main text")


def _rendered(renderer: Renderer, url: str, now: datetime, resp: requests.Response) -> Document:
    """The page as a browser shows it, for pages that build their text with scripts."""
    try:
        html = renderer.render(resp.url or url)
    except RenderError as exc:  # our browser failed, not the site: UNSUPPORTED, not EMPTY
        error = f"no readable main text, and rendering failed: {exc}"
        return Document(url=url, status=FetchStatus.UNSUPPORTED, fetched_at=now, error=error)
    extracted = extract_html(html.encode("utf-8"), resp.url or url, today=now.date())
    if not _readable(extracted):
        error = "no readable main text, even rendered"
        return Document(url=url, status=FetchStatus.EMPTY, fetched_at=now, error=error)
    return _document(url, now, "html", extracted, resp)


def _readable(extracted: Extracted) -> bool:
    return len(extracted.text) >= _MIN_TEXT_CHARS or bool(extracted.offers)


def _document(
    url: str, now: datetime, kind: str, extracted: Extracted, resp: requests.Response
) -> Document:
    return Document(
        url=url,
        status=FetchStatus.OK,
        fetched_at=now,
        final_url=resp.url,
        kind=kind,
        title=extracted.title,
        site=extracted.site or hostname(resp.url or url),
        author=extracted.author,
        published=extracted.published,
        updated=extracted.updated,
        text=extracted.text,
        offers=extracted.offers,
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
        content_hash=_content_hash(extracted),
        extractor=EXTRACTOR_VERSION,
    )


def _status_for_http(code: int) -> FetchStatus | None:
    if 200 <= code < 300:
        return None
    if code in (401, 403, 407, 429, 451):
        return FetchStatus.BLOCKED
    if code in (404, 410):
        return FetchStatus.NOT_FOUND
    if code >= 500:
        return FetchStatus.SERVER_ERROR
    return FetchStatus.HTTP_ERROR


def _refuse_private_hop(response: requests.Response, *args: Any, **kwargs: Any) -> None:
    if response.is_redirect:
        target = urljoin(response.url, response.headers["location"])
        address = private_address(target)
        if address is not None:
            raise _PrivateHop(target, address)


def _landed_privately(doc: Document) -> bool:
    return bool(doc.final_url) and private_address(doc.final_url or "") is not None


def _private(url: str, hop: _PrivateHop, now: datetime) -> Document:
    if hop.url is None:
        why = f"it leads to a private network ({hop.address})"
    elif hop.url == url:
        why = f"it is on a private network ({hop.address})"
    else:
        why = f"it redirects to {hop.url}, which is on a private network ({hop.address})"
    return _refused(url, why, now)


def _refused(url: str, why: str, now: datetime) -> Document:
    return Document(url=url, status=FetchStatus.REFUSED, fetched_at=now, error=f"not read: {why}")


def _revalidation_headers(cached: Document | None) -> dict[str, str]:
    if cached is None or not cached.ok:
        return {}
    headers = {}
    if cached.etag:
        headers["If-None-Match"] = cached.etag
    if cached.last_modified:
        headers["If-Modified-Since"] = cached.last_modified
    return headers


def _read_body(resp: requests.Response, max_bytes: int, max_seconds: float) -> bytes:
    declared = resp.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise _BodyTooLarge
    started = time.monotonic()
    chunks: list[bytes] = []
    size = 0
    for chunk in resp.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > max_bytes:
            raise _BodyTooLarge
        if time.monotonic() - started > max_seconds:
            raise _BodyTooSlow  # read timeouts are per-chunk; a slow drip needs a total limit
        chunks.append(chunk)
    return b"".join(chunks)


def _content_kind(media_type: str, body: bytes) -> str | None:
    if media_type == "application/pdf" or body.startswith(b"%PDF-"):
        return "pdf"
    if media_type in ("text/html", "application/xhtml+xml"):
        return "html"
    if media_type == "text/plain":
        return "text"
    if media_type in ("", "application/octet-stream", "text/xml", "application/xml"):
        head = body[:2048].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")) or b"<html" in head:
            return "html"
    return None


def _extract(kind: str, body: bytes, resp: requests.Response, *, today: date) -> Extracted:
    if kind == "pdf":
        return extract_pdf(body, today=today)
    if kind == "html":
        return extract_html(body, resp.url, today=today)
    encoding = resp.encoding if "charset" in resp.headers.get("Content-Type", "") else None
    try:
        return extract_plain_text(body, encoding or resp.apparent_encoding or "utf-8")
    except LookupError:  # a charset Python does not know, e.g. "utf8mb4"
        return extract_plain_text(body, "utf-8")


def _content_hash(extracted: Extracted) -> str:
    digest = hashlib.sha256(extracted.text.encode("utf-8"))
    for offer in extracted.offers:
        fields = (offer.product, offer.price, offer.currency, offer.availability, offer.unit)
        digest.update("".join(f"|{field}" for field in fields).encode())
    return digest.hexdigest()


def _describe(exc: requests.RequestException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 200 else text[:199] + "\N{HORIZONTAL ELLIPSIS}"


def _date_or_none(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
