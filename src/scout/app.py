"""Wires Scout's parts together from settings. The CLI, the daemon and the MCP server share it."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from scout.errors import ScoutError
from scout.evaluate import EvalCase, EvalScore, RecordedReplies, score
from scout.llm import make_backend, model_server
from scout.llm.base import Backend
from scout.llm.probe import AGENT_CONTEXT_TOKENS, Capabilities, probe
from scout.research.pipeline import Researcher, ResearchOptions
from scout.research.results import RunResult, Source
from scout.settings import Settings
from scout.store import Rating, Store
from scout.web.fetch import FetchConfig, Fetcher
from scout.web.render import make_renderer
from scout.web.search import CachedSearch, language_tag, make_search_backend

CAPABILITIES_TTL = timedelta(hours=24)
# Interactive runs reuse an hour of cached searches and pages.
SEARCH_CACHE_SECONDS = 3600.0
# Watches want the current web: pages are revalidated (ETag) after five minutes.
WATCH_PAGE_FRESHNESS = 300.0
WATCH_SEARCH_CACHE_SECONDS = 600.0


class App:
    def __init__(self, settings: Settings, *, llm: str | None = None) -> None:
        self.settings = replace(settings, llm=llm) if llm else settings
        # First what can fail on a bad setting, so nothing is left open when it does.
        self._search_backend = make_search_backend(
            self.settings.search,
            timeout=self.settings.fetch_timeout,
            user_agent=self.settings.user_agent,
        )
        self._renderer = make_renderer(
            self.settings.render,
            timeout=self.settings.fetch_timeout * 2,
            user_agent=self.settings.user_agent,
        )
        self.store = Store(self.settings.db_path)
        fetch_config = FetchConfig(
            timeout=self.settings.fetch_timeout,
            retries=self.settings.fetch_retries,
            user_agent=self.settings.user_agent,
            accept_language=accept_language(self.settings.region),
        )
        self.fetcher = Fetcher(fetch_config, cache=self.store, renderer=self._renderer)
        self.search = CachedSearch(self._search_backend, self.store, max_age=SEARCH_CACHE_SECONDS)
        # The same caches, with fresher limits, for watches.
        self.watch_fetcher = Fetcher(
            replace(fetch_config, fresh_for=WATCH_PAGE_FRESHNESS),
            cache=self.store,
            renderer=self._renderer,
        )
        self.watch_search = CachedSearch(
            self._search_backend, self.store, max_age=WATCH_SEARCH_CACHE_SECONDS
        )
        self._backend: Backend | None = None
        self._backend_lock = threading.Lock()

    def __enter__(self) -> App:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.fetcher.close()
        self.watch_fetcher.close()
        self._search_backend.close()
        if self._renderer is not None:
            self._renderer.close()
        if self._backend is not None:
            self._backend.close()
        self.store.close()

    @property
    def backend(self) -> Backend:
        with self._backend_lock:  # created on first use: history and show need no model
            if self._backend is None:
                self._backend = make_backend(self.settings)
            return self._backend

    def capabilities(self, *, refresh: bool = False, test_structured: bool = False) -> Capabilities:
        """What the configured model can do; probed at most once a day unless *refresh*."""
        key = self._capabilities_key()
        now = datetime.now(UTC)
        if not refresh:
            cached = self.store.get_capabilities(key, newer_than=now - CAPABILITIES_TTL)
            if cached is not None:
                return Capabilities.from_dict(cached)
        found = probe(
            self.backend,
            context_override=self.settings.context_tokens,
            test_structured=test_structured,
        )
        self.store.put_capabilities(key, found.to_dict(), now)
        return found

    def learn_from(self, researcher: Researcher) -> None:
        """Keep what a run discovered about the model, such as a json_schema mode that failed."""
        found = self.capabilities()
        if researcher.structured_mode is not found.structured_mode:
            updated = replace(found, structured_mode=researcher.structured_mode)
            self.store.put_capabilities(
                self._capabilities_key(), updated.to_dict(), datetime.now(UTC)
            )

    def researcher(
        self, *, fresh: bool = False, scope: str = "", **overrides: object
    ) -> Researcher:
        """A researcher for the configured model; *fresh* uses the watches' fresher caches, and
        *scope* names who runs it (a watch), so that runs of one goal resume apart."""
        found = self.capabilities()
        options = ResearchOptions(
            max_results=self.settings.max_results,
            region=self.settings.region,
            context_tokens=found.context_tokens,
            max_source_chars=self.settings.max_source_chars,
            structured_mode=found.structured_mode,
            reasoning_options=found.reasoning_options,
        )
        return Researcher(
            search=self.watch_search if fresh else self.search,
            fetcher=self.watch_fetcher if fresh else self.fetcher,
            backend=self.backend,
            options=replace(options, **overrides),
            sites=self.store,
            pins=self.store,
            pin_scope=scope,
        )

    def restore_sources(self, result: RunResult) -> tuple[list[Source], int]:
        """A stored run's sources with the exact page text it read, and how many are gone."""
        restored: list[Source] = []
        missing = 0
        for source in result.sources:
            if source.snippet_only:
                restored.append(source)
                continue
            page = self.restore_source(source)
            if page is None:
                missing += 1
                restored.append(replace(source, snippet_only=True, text=""))
            else:
                restored.append(page)
        return restored, missing

    def restore_source(self, source: Source) -> Source | None:
        """A stored page source with the text it had then; None when that version is gone."""
        snapshot = self.store.get_snapshot(source.content_hash) if source.content_hash else None
        if snapshot is None:
            return None
        return replace(source, text=snapshot.text, offers=snapshot.offers)

    def replay(self, run_id: int) -> tuple[RunResult, RunResult]:
        """Analyze a stored run's pages again with the configured model: (before, after)."""
        before = self.store.get_run(run_id)
        if before is None:
            raise ScoutError(f"no run with id {run_id}")
        sources, missing = self.restore_sources(before)
        note = f"replay of run {run_id}"
        if missing:
            note += f"; {missing} page(s) of it are no longer stored"
        researcher = self.researcher()
        after = researcher.reanalyze(before.goal, before.plan, sources, note=note)
        self.learn_from(researcher)
        return before, after

    def rate(self, run_id: int, number: int, verdict: str, *, note: str | None = None) -> Rating:
        """Record a verdict on finding *number* of a run, as its report numbers them."""
        result = self.store.get_run(run_id)
        if result is None:
            raise ScoutError(f"no run with id {run_id}")
        findings = result.numbered
        if not 1 <= number <= len(findings):
            raise ScoutError(f"run {run_id} has findings 1 to {len(findings)}, not {number}")
        finding = findings[number - 1]
        source = result.source(finding.source)
        rating = Rating(
            run_id=run_id,
            number=number,
            verdict=verdict,
            note=note,
            rated_at=datetime.now(UTC),
            goal=result.goal,
            model=result.model,
            claim=finding.claim,
            quote=finding.quote,
            value=finding.value,
            url=source.url if source is not None else None,
            trusted=finding.trusted,
        )
        self.store.put_rating(rating)
        return rating

    def case_from_run(self, run_id: int, name: str) -> EvalCase:
        """A stored run as an eval case; its ratings become what the case expects."""
        result = self.store.get_run(run_id)
        if result is None:
            raise ScoutError(f"no run with id {run_id}")
        sources, missing = self.restore_sources(result)
        if missing:
            raise ScoutError(f"run {run_id} cannot become a case: {missing} page(s) are gone")
        ratings = self.store.ratings(run_id=run_id)
        return EvalCase(
            name=name,
            goal=result.goal,
            plan=result.plan,
            sources=tuple(sources),
            expect_facts=tuple(r.value or r.quote for r in ratings if r.verdict == "good"),
            expect_untrusted=tuple(r.value or r.quote for r in ratings if r.verdict == "bad"),
        )

    def evaluate(self, case: EvalCase, *, recorded: bool = False) -> tuple[RunResult, EvalScore]:
        """Score the configured model on *case*, or replay the case's own recorded replies."""
        if recorded:
            researcher = Researcher(
                search=self.search,
                fetcher=self.fetcher,
                backend=RecordedReplies(case),
                options=ResearchOptions(context_tokens=AGENT_CONTEXT_TOKENS),
            )
        else:
            researcher = self.researcher()
        result = researcher.reanalyze(case.goal, case.plan, case.sources, note=f"eval {case.name}")
        return result, score(case, result)

    def _capabilities_key(self) -> str:
        server = model_server(self.backend)
        where = server.base_url if server is not None else "local"
        return f"{where}|{self.backend.model}|{self.settings.context_tokens or ''}"


def accept_language(region: str) -> str:
    """HTTP Accept-Language for a ddgs region such as 'in-en' (country-language)."""
    tag = language_tag(region)
    if tag is None:
        return "en;q=0.9"
    return f"{tag},{tag.split('-')[0]};q=0.9"
