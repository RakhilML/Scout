"""A research run: plan, search, read, pack, extract, verify, assess."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from scout.clock import utcnow
from scout.errors import (
    AnswerPending,
    ContextOverflow,
    LLMError,
    SearchError,
    StructuredOutputError,
    UnsupportedResponseFormat,
)
from scout.llm.base import Backend, Message
from scout.llm.structured import StructuredMode, generate
from scout.research import prompts
from scout.research.plan import heuristic_plan, model_plan
from scout.research.rank import coverage, pack
from scout.research.reputation import SiteBook, SiteRecord
from scout.research.results import Finding, Flag, Plan, RunResult, Source
from scout.research.schema import Extraction, FollowUp, Synthesis
from scout.research.values import attach_amounts, flag_values, offer_findings
from scout.research.verify import assess, verify
from scout.textutil import clean
from scout.web.domains import canonical_url, hostname
from scout.web.fetch import Document, Fetcher
from scout.web.search import SearchBackend, SearchHit, merge_hits

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_CHARS_PER_TOKEN = 3.2  # conservative for web text full of numbers and names
_PROMPT_OVERHEAD_TOKENS = 1200  # system prompt, schema and framing around the sources
MAX_FINDINGS = 12  # also stated in the prompt; extra findings are dropped
MAX_ROUNDS = 5  # deep research: search rounds at most
_MIN_COVERAGE = 0.34  # hits sharing less of the goal's (IDF-weighted) terms are off-topic
_LISTING_PENALTY = 0.15  # price goals: product pages beat search-result and category pages
_LISTING_HINTS = ("/s?", "/search", "/sch/", "?q=", "&q=", "/pl?", "/category/", "/tag/")
_MAX_PER_SITE = 2
_STALE_KINDS = ("news", "release")  # goals about what is new; a price page's date says little
_RECENCY_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}
PIN_TTL = timedelta(days=1)  # a run still waiting a day after it first waited reads the web again


@dataclass(frozen=True, slots=True)
class ResearchOptions:
    max_results: int = 6
    region: str = "us-en"
    kind: str | None = None  # force the goal kind instead of letting the planner decide
    recency: str | None = None  # force recency; "any" turns the time filter off
    use_planner: bool = True
    context_tokens: int = 8192
    max_source_chars: int = 6000
    fetch_deadline: float = 45.0
    structured_mode: StructuredMode = StructuredMode.PROMPT
    reasoning_options: tuple[str, ...] = ()


class Pins(Protocol):
    """Where a run waiting for a model answer keeps what it read (the Store)."""

    def get_pin(self, run: str, key: str, *, newer_than: datetime) -> Any | None: ...
    def put_pins(self, run: str, pins: Mapping[str, Any], at: datetime) -> None: ...
    def drop_pins(self, run: str, *, before: datetime | None = None) -> None: ...


class Researcher:
    def __init__(
        self,
        *,
        search: SearchBackend,
        fetcher: Fetcher,
        backend: Backend,
        options: ResearchOptions | None = None,
        clock: Callable[[], datetime] = utcnow,
        sites: SiteBook | None = None,
        pins: Pins | None = None,
        pin_scope: str = "",
    ) -> None:
        self._search_backend = search
        self._fetcher = fetcher
        self._backend = backend
        self._options = options or ResearchOptions()
        self._clock = clock
        self._sites = sites  # what earlier runs learned about sites; None: nothing
        self._pins = pins
        self._pin_scope = pin_scope  # who runs it (a watch): runs of one goal pin apart
        self._reads: dict[str, Any] = {}  # what this run read, kept if it must wait for an answer
        self._run_key = ""
        self.structured_mode = self._options.structured_mode  # may drop to PROMPT during a run

    def run(
        self, goal: str, *, plan: Plan | None = None, reuse: RunResult | None = None
    ) -> RunResult:
        """Plan (unless *plan* is given), search and read, then analyze what was read.

        When *reuse* (an earlier run) read exactly the same pages, its analysis is carried over
        and the model is not asked again: a watch whose pages did not change costs no GPU time.
        """
        goal = clean(goal)
        return self._pinned(goal, "run", lambda: self._run(goal, plan, reuse))

    def _run(self, goal: str, plan: Plan | None, reuse: RunResult | None) -> RunResult:
        started = self._started()
        warnings: list[str] = []
        plan = (
            self._forced(plan) if plan is not None else self._plan(goal, started.date(), warnings)
        )
        sources = self._read(goal, plan, warnings)
        carried = _carried_sources(reuse.sources, sources) if reuse is not None else None
        if reuse is not None and carried is not None:
            today = started.date()
            findings = _report_order(_flag_stale(reuse.findings, carried, plan, today))
            return replace(
                reuse,
                started_at=started,
                finished_at=self._clock(),
                plan=plan,
                sources=tuple(carried),
                findings=tuple(findings),
                confidence=assess(findings, carried, kind=plan.kind, today=today),
                warnings=(*warnings, "no page changed since the last run; its findings were kept"),
                carried_over=True,
            )
        return self._analyze(goal, plan, sources, started, warnings)

    def _pinned(self, goal: str, mode: str, work: Callable[[], RunResult]) -> RunResult:
        """Do *work*, keeping what it read while it waits for a model answer: started again, the
        run reads the same pages on the same day, so its prompts (and their answers) match."""
        self._reads = {}
        self._run_key = json.dumps([goal, mode, self._pin_scope], ensure_ascii=False)
        if self._pins is None:
            return work()
        self._pins.drop_pins(self._run_key, before=self._clock() - PIN_TTL)
        try:
            result = work()
        except AnswerPending:
            self._pins.put_pins(self._run_key, self._reads, self._clock())
            raise
        except Exception:
            self._pins.drop_pins(self._run_key)
            raise
        self._pins.drop_pins(self._run_key)
        return result

    def _pin(self, key: str) -> Any | None:
        if self._pins is None:
            return None
        return self._pins.get_pin(self._run_key, key, newer_than=self._clock() - PIN_TTL)

    def _started(self) -> datetime:
        pinned = self._pin("started")
        started = datetime.fromisoformat(pinned) if pinned else self._clock()
        self._reads["started"] = started.isoformat()
        return started

    def _read(
        self,
        goal: str,
        plan: Plan,
        warnings: list[str],
        *,
        exclude: Collection[str] = (),
        start: int = 1,
    ) -> list[Source]:
        """Search, then read the pages worth reading: as they were read before, if pinned."""
        key = _read_key(plan, exclude, self._options)
        read = self._pin(key)
        if read is None:
            found: list[str] = []
            hits = self._find(goal, plan, found, exclude=exclude)
            documents = self._fetcher.fetch_many(
                [hit.url for hit in hits], deadline=self._options.fetch_deadline
            )
            read = {
                "hits": [hit.to_dict() for hit in hits],
                "documents": [document.to_dict() for document in documents],
                "warnings": found,
            }
        self._reads[key] = read
        warnings.extend(read["warnings"])
        sources = _sources(
            [SearchHit.from_dict(hit) for hit in read["hits"]],
            [Document.from_dict(document) for document in read["documents"]],
            start=start,
        )
        warnings.extend(_unreadable_warning(sources))
        return sources

    def reanalyze(
        self, goal: str, plan: Plan, sources: Sequence[Source], *, note: str | None = None
    ) -> RunResult:
        """Extract and verify again from sources already read: replays and evaluations."""
        return self._analyze(goal, plan, list(sources), self._clock(), [note] if note else [])

    def run_deep(self, goal: str, *, rounds: int = 3) -> RunResult:
        """Research, then look again where the verified findings leave gaps.

        After each round the model names up to three follow-up searches. Only pages not read
        yet are read, and their findings are verified like any others. It stops when the model
        sees no gaps, when a round adds nothing trusted, or after *rounds*; the answer is then
        written from the verified findings alone.
        """
        goal = clean(goal)
        return self._pinned(goal, f"deep:{rounds}", lambda: self._run_deep(goal, rounds))

    def _run_deep(self, goal: str, rounds: int) -> RunResult:
        result = self._run(goal, None, None)
        for _ in range(1, min(rounds, MAX_ROUNDS)):
            try:
                follow_up = self._follow_up(result)
                searched = set(result.plan.queries)
                queries = [
                    q
                    for q in dict.fromkeys(map(clean, follow_up.queries))
                    if q and q not in searched
                ]
                if follow_up.done or not queries:
                    break
                deeper = self._deepen(result, queries)
            except AnswerPending:
                raise
            except LLMError as exc:
                result = _warn(result, f"stopped looking further: {exc}")
                break
            if deeper is None:
                break
            result = deeper
        if result.rounds == 1:
            return result
        pages = sum(1 for source in result.sources if not source.snippet_only)
        return _warn(
            self._write_answer(result), f"deep research: {result.rounds} rounds, {pages} pages read"
        )

    def _follow_up(self, result: RunResult) -> FollowUp:
        facts = [
            prompts.fact_line(number, f, result.source(f.source))
            for number, f in enumerate(result.trusted, start=1)
        ]
        set_aside = [
            f"- {f.claim} ({f.note or f.verdict.value})" for f in result.findings if not f.trusted
        ]
        messages = prompts.follow_up_messages(
            result.goal,
            result.started_at.date(),
            facts=facts,
            set_aside=set_aside,
            searched=result.plan.queries,
        )
        return self._generate(messages, FollowUp, purpose="follow_up")

    def _deepen(self, result: RunResult, queries: list[str]) -> RunResult | None:
        """One more round: new searches, unread pages, verified findings not known yet."""
        warnings: list[str] = []
        read = {canonical_url(source.url) for source in result.sources}
        this_round = replace(result.plan, queries=tuple(queries))
        start = max((s.index for s in result.sources), default=0) + 1
        try:
            sources = self._read(result.goal, this_round, warnings, exclude=read, start=start)
        except SearchError:
            return None
        today = result.started_at.date()
        extraction = self._extract(result.goal, this_round, sources, today, warnings)
        found = verify(extraction.findings[:MAX_FINDINGS], sources)
        if result.plan.kind == "price":
            found += offer_findings(sources)
        found = _flag_stale(found, sources, result.plan, today)
        # Only unread pages were read, so every trusted finding is new (or corroborates).
        if not any(finding.trusted for finding in found):
            return None
        every_source = (*result.sources, *sources)
        findings = _report_order(
            flag_values(
                attach_amounts([*_without_outlier_flags(result.findings), *found]), result.goal
            )
        )
        return replace(
            result,
            finished_at=self._clock(),
            plan=replace(result.plan, queries=(*result.plan.queries, *queries)),
            sources=every_source,
            findings=tuple(findings),
            confidence=assess(findings, every_source, kind=result.plan.kind, today=today),
            warnings=(*result.warnings, *(f"round {result.rounds + 1}: {w}" for w in warnings)),
            rounds=result.rounds + 1,
        )

    def _write_answer(self, result: RunResult) -> RunResult:
        """The answer, written again from everything verified in all rounds."""
        facts = [
            prompts.fact_line(number, f, result.source(f.source))
            for number, f in enumerate(result.trusted, start=1)
        ]
        messages = prompts.answer_messages(result.goal, result.started_at.date(), facts)
        try:
            synthesis = self._generate(messages, Synthesis, purpose="answer")
        except AnswerPending:
            raise
        except LLMError as exc:
            return _warn(result, f"kept the first round's answer: the final one failed ({exc})")
        return replace(result, answer=clean(synthesis.answer), finished_at=self._clock())

    def _analyze(
        self,
        goal: str,
        plan: Plan,
        sources: list[Source],
        started: datetime,
        warnings: list[str],
    ) -> RunResult:
        today = started.date()
        extraction = self._extract(goal, plan, sources, today, warnings)
        findings = verify(extraction.findings[:MAX_FINDINGS], sources)
        if plan.kind == "price":
            findings += offer_findings(sources)
        findings = _flag_stale(findings, sources, plan, today)
        findings = _report_order(flag_values(attach_amounts(findings), goal))
        return RunResult(
            goal=goal,
            started_at=started,
            finished_at=self._clock(),
            model=self._backend.model,
            plan=plan,
            sources=tuple(sources),
            answer=clean(extraction.answer),
            findings=tuple(findings),
            confidence=assess(findings, sources, kind=plan.kind, today=today),
            warnings=tuple(warnings),
        )

    def _plan(self, goal: str, today: date, warnings: list[str]) -> Plan:
        plan = heuristic_plan(goal)
        if self._options.use_planner:
            try:
                plan = self._generate_plan(goal, today)
            except StructuredOutputError as exc:
                warnings.append(
                    f"searched for the goal as written: the model's plan was unusable ({exc})"
                )
        return self._forced(plan)

    def _forced(self, plan: Plan) -> Plan:
        """The plan with the kind and recency the options insist on."""
        if self._options.kind:
            plan = replace(plan, kind=self._options.kind)
        if self._options.recency:
            plan = replace(
                plan, recency=None if self._options.recency == "any" else self._options.recency
            )
        return plan

    def _generate_plan(self, goal: str, today: date) -> Plan:
        try:
            return model_plan(
                goal,
                self._backend,
                mode=self.structured_mode,
                today=today,
                reasoning_effort=self._effort("low"),
            )
        except (UnsupportedResponseFormat, StructuredOutputError):
            if not self._fall_back_to_prompt_mode():
                raise
            return self._generate_plan(goal, today)

    def _find(
        self, goal: str, plan: Plan, warnings: list[str], *, exclude: Collection[str] = ()
    ) -> list[SearchHit]:
        """The hits to read; *exclude* holds (canonical) URLs already read."""
        wanted = self._options.max_results
        news = plan.kind == "news"
        results = [self._search(query, plan.recency, news) for query in plan.queries]
        if news and not any(results):
            results = [self._search(query, plan.recency, False) for query in plan.queries]
        if plan.recency and not any(results):
            warnings.append(f"nothing from the last {plan.recency}; searched without a date limit")
            results = [self._search(query, None, False) for query in plan.queries]
        hits = [
            hit
            for hit in merge_hits(results, limit=wanted * 3 + len(exclude))
            if canonical_url(hit.url) not in exclude
        ]
        if not hits:
            raise SearchError("no search results for: " + "; ".join(plan.queries))
        known = self._sites.site_records({hostname(hit.url) for hit in hits}) if self._sites else {}
        return _select(
            hits, goal, plan, limit=wanted, warnings=warnings, sites=known, now=self._clock()
        )

    def _search(self, query: str, recency: str | None, news: bool) -> list[SearchHit]:
        return self._search_backend.search(
            query,
            max_results=self._options.max_results,
            region=self._options.region,
            recency=recency,
            news=news,
        )

    def _extract(
        self, goal: str, plan: Plan, sources: Sequence[Source], today: date, warnings: list[str]
    ) -> Extraction:
        budget = source_budget(self._options.context_tokens)
        query = " ".join((goal, *plan.queries))
        try:
            extraction, left_out = self._extract_within(goal, query, sources, today, budget)
        except ContextOverflow:
            # Token estimates are approximate; the server's verdict wins. One smaller retry.
            warnings.append("the sources did not fit the model's context window; sent less text")
            extraction, left_out = self._extract_within(
                goal, query, sources, today, int(budget * 0.6)
            )
        if left_out:
            warnings.append(f"{left_out} readable page(s) did not fit the context window")
        return extraction

    def _extract_within(
        self, goal: str, query: str, sources: Sequence[Source], today: date, budget: int
    ) -> tuple[Extraction, int]:
        """The model's extraction, and how many readable pages the budget had to leave out."""
        packed = pack(
            [(source.index, source.text) for source in sources if source.text],
            query,
            budget=budget,
            per_source=self._options.max_source_chars,
        )
        left_out = sum(1 for s in sources if not s.snippet_only and s.index not in packed)
        blocks = [prompts.source_block(s, packed[s.index]) for s in sources if s.index in packed]
        if not blocks:
            return Extraction(answer="None of the sources could be read.", findings=[]), left_out
        messages = prompts.extract_messages(goal, today, blocks, max_findings=MAX_FINDINGS)
        return self._generate(messages, Extraction, purpose="extract"), left_out

    def _generate(self, messages: list[Message], output: type[T], *, purpose: str) -> T:
        try:
            return generate(
                self._backend,
                messages,
                output,
                mode=self.structured_mode,
                purpose=purpose,
                reasoning_effort=self._effort("medium"),
            )
        except (UnsupportedResponseFormat, StructuredOutputError):
            if not self._fall_back_to_prompt_mode():
                raise
            return self._generate(messages, output, purpose=purpose)

    def _fall_back_to_prompt_mode(self) -> bool:
        """Switch from server-side schemas to prompt mode once; False if already there."""
        if self.structured_mode is StructuredMode.PROMPT:
            return False
        log.warning("structured output via json_schema failed; switching to prompt mode")
        self.structured_mode = StructuredMode.PROMPT
        return True

    def _effort(self, level: str) -> str | None:
        return level if level in self._options.reasoning_options else None


def source_budget(context_tokens: int) -> int:
    """Characters of source text that fit beside the instructions, reasoning and answer."""
    reserve = max(2048, context_tokens // 4)
    tokens = max(1024, context_tokens - reserve - _PROMPT_OVERHEAD_TOKENS)
    return int(tokens * _CHARS_PER_TOKEN)


def _select(
    hits: Sequence[SearchHit],
    goal: str,
    plan: Plan,
    *,
    limit: int,
    warnings: list[str],
    sites: Mapping[str, SiteRecord],
    now: datetime,
) -> list[SearchHit]:
    """Keep the hits most about the goal (IDF-weighted term coverage), a few per site at most.

    What earlier runs learned about the sites comes second: it reorders relevant hits a little
    and skips sites that keep failing, but never makes an off-topic hit relevant.
    """
    texts = [f"{hit.title} {hit.snippet}" for hit in hits]
    relevance = [
        max(values)
        for values in zip(*(coverage(q, texts) for q in (goal, *plan.queries)), strict=True)
    ]
    if plan.kind == "price":
        relevance = [
            score - _LISTING_PENALTY if any(h in hit.url for h in _LISTING_HINTS) else score
            for score, hit in zip(relevance, hits, strict=True)
        ]
    records = [sites.get(hostname(hit.url)) for hit in hits]
    ranking = [
        score + (record.bonus if record else 0.0)
        for score, record in zip(relevance, records, strict=True)
    ]
    order = sorted(range(len(hits)), key=lambda i: (-ranking[i], hits[i].rank))
    on_topic = [i for i in order if relevance[i] >= _MIN_COVERAGE]
    # Filtering must not starve the run: with too few on-topic hits, rank all of them instead.
    filtered = len(on_topic) >= max(1, limit // 2)
    candidates = on_topic if filtered else order

    avoided = {
        i: why for i in candidates if (record := records[i]) and (why := record.avoided(now))
    }
    if avoided and len(candidates) - len(avoided) >= max(1, limit // 2):
        candidates = [i for i in candidates if i not in avoided]
        warnings.append(
            "skipped sites that keep failing: " + "; ".join(sorted(set(avoided.values())))
        )

    chosen: list[SearchHit] = []
    per_site: Counter[str] = Counter()
    for i in candidates:
        site = hostname(hits[i].url)
        if per_site[site] < _MAX_PER_SITE:
            chosen.append(hits[i])
            per_site[site] += 1
        if len(chosen) == limit:
            break
    off_topic = len(hits) - len(on_topic)
    if filtered and off_topic:
        warnings.append(f"ignored {off_topic} off-topic search result(s)")
    return chosen


def _sources(
    hits: Sequence[SearchHit], documents: Sequence[Document], *, start: int = 1
) -> list[Source]:
    sources = []
    for index, (hit, doc) in enumerate(zip(hits, documents, strict=True), start=start):
        readable = doc.ok and bool(doc.text or doc.offers)
        sources.append(
            Source(
                index=index,
                url=hit.url,
                title=doc.title or hit.title or hit.url,
                site=(doc.site if readable else None) or hostname(hit.url),
                status=doc.status.value,
                query=hit.query,
                published=(doc.published if readable else None) or hit.published,
                updated=doc.updated if readable else None,
                snippet_only=not readable,
                text=doc.text if readable else hit.snippet,
                content_hash=doc.content_hash if readable else None,
                offers=doc.offers if readable else (),
                error=doc.error,
            )
        )
    return sources


def _without_outlier_flags(findings: Sequence[Finding]) -> list[Finding]:
    """Outliers are judged again once more values are known."""
    return [
        replace(finding, flag=None, note=None) if finding.flag is Flag.OUTLIER else finding
        for finding in findings
    ]


def _warn(result: RunResult, warning: str) -> RunResult:
    return replace(result, warnings=(*result.warnings, warning))


def _carried_sources(before: Sequence[Source], after: Sequence[Source]) -> list[Source] | None:
    """*after* numbered as *before* was, when both are the same pages with the same content
    (snippets compared by text) in any order; None when anything differs."""

    def signature(source: Source) -> tuple[str, str | None, str | None]:
        return (source.url, source.content_hash, source.text if source.snippet_only else None)

    now = {signature(source): source for source in after}
    if not before or len(now) != len(after) or len(before) != len(after):
        return None
    if set(now) != {signature(source) for source in before}:
        return None
    return [replace(now[signature(source)], index=source.index) for source in before]


def _report_order(findings: Sequence[Finding]) -> list[Finding]:
    """Trusted findings first, as reports number them, each group in the model's order."""
    return sorted(findings, key=lambda finding: not finding.trusted)


def _flag_stale(
    findings: Sequence[Finding], sources: Sequence[Source], plan: Plan, today: date
) -> list[Finding]:
    """For news and releases, a fact from a page dated well before the period the goal asks
    about (twice its length) is old news, however exactly it is quoted."""
    days = _RECENCY_DAYS.get(plan.recency or "")
    if plan.kind not in _STALE_KINDS or days is None:
        return list(findings)
    dates = {source.index: source.freshest_date for source in sources}
    marked = []
    for finding in findings:
        dated = dates.get(finding.source)
        if finding.trusted and dated is not None and (today - dated).days > 2 * days:
            note = f"from a page dated {dated.isoformat()}, older than the {plan.recency} asked"
            finding = replace(finding, flag=Flag.STALE, note=note)
        marked.append(finding)
    return marked


def _read_key(plan: Plan, exclude: Collection[str], options: ResearchOptions) -> str:
    what = [plan.queries, plan.kind, plan.recency, sorted(exclude), options.max_results]
    encoded = json.dumps([*what, options.region], ensure_ascii=False).encode("utf-8")
    return "read:" + hashlib.sha256(encoded).hexdigest()[:16]


def _unreadable_warning(sources: Sequence[Source]) -> list[str]:
    failed = Counter(source.status.replace("_", " ") for source in sources if source.snippet_only)
    if not failed:
        return []
    detail = ", ".join(f"{count} {status}" for status, count in failed.most_common())
    total = sum(failed.values())
    return [
        f"{total} of {len(sources)} pages could not be read ({detail}); used their search snippets"
    ]
