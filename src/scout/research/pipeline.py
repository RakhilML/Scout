"""A research run: plan, search, read, pack, extract, verify, assess. And a fact-check: the same
for each claim a text makes."""

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
    ScoutError,
    SearchError,
    StructuredOutputError,
    UnsupportedResponseFormat,
)
from scout.llm.base import Backend, Message
from scout.llm.structured import StructuredMode, generate
from scout.research import factcheck, prompts
from scout.research.plan import heuristic_plan, model_plan
from scout.research.rank import coverage, pack
from scout.research.reputation import SiteBook, SiteRecord
from scout.research.results import CHECK_KIND, Finding, Flag, Plan, RunResult, Source
from scout.research.schema import (
    ClaimList,
    ClaimToCheck,
    Extraction,
    FollowUp,
    Judgment,
    Synthesis,
)
from scout.research.values import attach_amounts, flag_values, offer_findings
from scout.research.verify import assess, verify
from scout.textutil import clean, fold, shorten
from scout.web.domains import (
    canonical_url,
    hostname,
    matches_any,
    private_address,
    registrable_domain,
)
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
    public_only: bool = False  # a caller a web page may have steered: no private addresses


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
        scope = [goal, mode, self._pin_scope, self._options.public_only]
        self._run_key = json.dumps(scope, ensure_ascii=False)
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
        avoid: Collection[str] = (),
        start: int = 1,
    ) -> list[Source]:
        """Search, then read the pages worth reading: as they were read before, if pinned."""
        key = _read_key(plan, exclude, avoid, self._options)
        read = self._pin(key)
        if read is None:
            found: list[str] = []
            hits = self._find(goal, plan, found, exclude=exclude, avoid=avoid)
            documents = self._fetcher.fetch_many(
                [hit.url for hit in hits], deadline=self._options.fetch_deadline
            )
            if avoid:
                hits, documents = _independent(hits, documents, avoid, found)
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

    def check(self, subject: str, *, max_claims: int = factcheck.MAX_CLAIMS) -> RunResult:
        """Fact-check a text, or the page at a web address, claim by claim.

        The model lists the claims and quotes pages on each. Scout keeps only the claims the
        text makes, and rules on each from the quotes it verified, so no ruling rests on the
        model's word. Pages from the checked page's own site are never evidence.

        With the public_only option (callers a web page may have steered, such as an assistant
        over MCP) an address on a private network is refused rather than read.
        """
        subject = clean(subject)
        if not subject:
            raise ScoutError("nothing to check")
        return self._pinned(
            hashlib.sha256(subject.encode()).hexdigest(),
            f"check:{max_claims}",
            lambda: self._check(subject, max_claims),
        )

    def _check(self, subject: str, max_claims: int) -> RunResult:
        started = self._started()
        today = started.date()
        warnings: list[str] = []
        text, page = self._subject(subject)
        claims, planner, text, set_aside = self._claims(text, today, max_claims, warnings)
        plan = Plan(
            queries=tuple(claim.query for claim in claims),
            kind=CHECK_KIND,
            recency=None,
            planner=planner,
        )
        origin = (subject, page.final_url or "") if page is not None else ()
        avoid = frozenset(registrable_domain(host) for host in map(hostname, origin) if host)
        sources: list[Source] = []
        weighed = []
        for number, claim in enumerate(claims, start=1):
            log.info("checking claim %d of %d", number, len(claims))
            noted: list[str] = []
            searched = replace(plan, queries=(claim.query,))
            item = self._check_claim(claim, searched, avoid, sources, today, noted)
            weighed.append(item._replace(caveat=factcheck.caveat(claim, text)))
            warnings.extend(f"claim {number}: {warning}" for warning in noted)
        findings, checks = factcheck.assemble(weighed)
        answer, confidence = factcheck.summarize(checks, findings, sources, set_aside=set_aside)
        named = subject if page is not None else shorten(" ".join(text.split()), 80)
        return RunResult(
            goal=f"fact-check: {named}",
            started_at=started,
            finished_at=self._clock(),
            model=self._backend.model,
            plan=plan,
            sources=tuple(sources),
            answer=answer,
            findings=tuple(findings),
            confidence=confidence,
            warnings=tuple(warnings),
            claims=tuple(checks),
            checked_text=text,
        )

    def _subject(self, subject: str) -> tuple[str, Document | None]:
        """The text to check, and the page it comes from when *subject* is a web address: that
        page is read once and pinned, like the pages a run reads."""
        if not subject.startswith(("http://", "https://")) or len(subject.split()) > 1:
            return subject, None
        public_only = self._options.public_only
        if public_only:
            _refuse_private(subject)
        pinned = self._pin("subject")
        page = (
            Document.from_dict(pinned)
            if pinned
            else self._fetcher.fetch_many([subject], deadline=self._options.fetch_deadline)[0]
        )
        if public_only and page.final_url:
            _refuse_private(page.final_url)
        self._reads["subject"] = page.to_dict()
        if not page.ok or not page.text:
            reason = page.error or page.status.value.replace("_", " ")
            raise ScoutError(f"could not read {subject}: {reason}")
        text = page.text
        if page.title and not fold(text).startswith(fold(page.title)):
            text = f"{page.title}\n\n{text}"
        return clean(text), page

    def _claims(
        self, text: str, today: date, max_claims: int, warnings: list[str]
    ) -> tuple[list[ClaimToCheck], str, str, int]:
        """The claims to check, who chose them (the model, or the heuristic of checking every
        sentence when the model's list is unusable), the text as checked (cut to what fits the
        model's context window), and how many listed claims the text does not make."""
        budget = source_budget(self._options.context_tokens)
        whole = len(text)
        try:
            try:
                text, listed = self._listed(text, today, max_claims, budget)
            except ContextOverflow:
                # Token estimates are approximate; the server's verdict wins. One smaller retry.
                text, listed = self._listed(text, today, max_claims, int(budget * 0.6))
        except StructuredOutputError as exc:
            warnings.append(
                "checked the text sentence by sentence: "
                f"the model's list of claims was unusable ({exc})"
            )
            text = _cut(text[:budget], whole, warnings)
            return factcheck.sentences(text, max_claims), "heuristic", text, 0
        claims, set_aside = factcheck.anchored(listed.claims, text, max_claims)
        warnings.extend(set_aside)
        return claims, "model", _cut(text, whole, warnings), len(set_aside)

    def _listed(
        self, text: str, today: date, max_claims: int, budget: int
    ) -> tuple[str, ClaimList]:
        text = text[:budget]
        messages = prompts.claims_messages(text, today, max_claims=max_claims)
        return text, self._generate(messages, ClaimList, purpose="claims")

    def _check_claim(
        self,
        claim: ClaimToCheck,
        plan: Plan,
        avoid: Collection[str],
        sources: list[Source],
        today: date,
        warnings: list[str],
    ) -> factcheck.Weighed:
        """Search and read for one claim, leaving out the sites *avoid*, then weigh the quotes
        the model finds on its pages. The pages join *sources*, the run's, keeping their number
        if an earlier claim read them. A reply that is unusable leaves this claim unclear; a
        model that is down or still to answer stops the check."""
        try:
            found = self._read(claim.claim, plan, warnings, avoid=avoid)
        except SearchError as exc:
            warnings.append(str(exc))
            return factcheck.Weighed(claim, [], [], None, (str(exc),))
        found = factcheck.renumber(found, sources)
        try:
            judgment = self._judge(claim.claim, found, today, warnings)
        except StructuredOutputError as exc:
            warnings.append(f"not judged ({exc})")
            return factcheck.Weighed(claim, [], [], None, (f"not judged ({exc})",))
        supports, refutes = factcheck.weigh(claim.claim, judgment, found)
        problems = (factcheck.NOTHING_READ,) if factcheck.NOTHING_READ in warnings else ()
        return factcheck.Weighed(claim, supports, refutes, clean(judgment.note) or None, problems)

    def _judge(
        self, claim: str, sources: Sequence[Source], today: date, warnings: list[str]
    ) -> Judgment:
        def ask(budget: int) -> tuple[Judgment, int]:
            blocks, left_out = self._blocks(claim, sources, budget)
            if not blocks:
                warnings.append(factcheck.NOTHING_READ)
                return Judgment(note=""), left_out
            messages = prompts.judge_messages(
                claim, today, blocks, max_evidence=factcheck.MAX_EVIDENCE
            )
            return self._generate(messages, Judgment, purpose="judge"), left_out

        return self._fitted(ask, warnings)

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
        self,
        goal: str,
        plan: Plan,
        warnings: list[str],
        *,
        exclude: Collection[str] = (),
        avoid: Collection[str] = (),
    ) -> list[SearchHit]:
        """The hits to read; *exclude* holds (canonical) URLs already read, *avoid* the sites
        (with their subdomains) that may not be read: the checked text's own."""
        wanted = self._options.max_results
        # Results from the avoided sites are dropped below: ask for more, so others fill in.
        count = wanted * 3 if avoid else wanted
        news = plan.kind == "news"
        results = [self._search(query, plan.recency, news, count) for query in plan.queries]
        if news and not any(results):
            results = [self._search(query, plan.recency, False, count) for query in plan.queries]
        if plan.recency and not any(results):
            warnings.append(f"nothing from the last {plan.recency}; searched without a date limit")
            results = [self._search(query, None, False, count) for query in plan.queries]
        hits = [
            hit
            for hit in merge_hits(results, limit=wanted * 3 + len(exclude))
            if canonical_url(hit.url) not in exclude
        ]
        if not hits:
            raise SearchError("no search results for: " + "; ".join(plan.queries))
        others = [hit for hit in hits if not matches_any(hit.url, avoid)]
        if len(others) < len(hits):
            where = f"{', '.join(sorted(avoid))}, where the text comes from"
            if not others:
                raise SearchError(f"every search result is from {where}")
            warnings.append(f"left out {len(hits) - len(others)} search result(s) from {where}")
            hits = others
        known = self._sites.site_records({hostname(hit.url) for hit in hits}) if self._sites else {}
        return _select(
            hits, goal, plan, limit=wanted, warnings=warnings, sites=known, now=self._clock()
        )

    def _search(self, query: str, recency: str | None, news: bool, count: int) -> list[SearchHit]:
        return self._search_backend.search(
            query,
            max_results=count,
            region=self._options.region,
            recency=recency,
            news=news,
        )

    def _extract(
        self, goal: str, plan: Plan, sources: Sequence[Source], today: date, warnings: list[str]
    ) -> Extraction:
        query = " ".join((goal, *plan.queries))
        return self._fitted(
            lambda budget: self._extract_within(goal, query, sources, today, budget), warnings
        )

    def _fitted(self, ask: Callable[[int], tuple[T, int]], warnings: list[str]) -> T:
        """*ask*'s reply with as much source text as the context window holds: *ask* takes a
        budget in characters and returns its reply and how many readable pages did not fit."""
        budget = source_budget(self._options.context_tokens)
        try:
            reply, left_out = ask(budget)
        except ContextOverflow:
            # Token estimates are approximate; the server's verdict wins. One smaller retry.
            warnings.append("the sources did not fit the model's context window; sent less text")
            reply, left_out = ask(int(budget * 0.6))
        if left_out:
            warnings.append(f"{left_out} readable page(s) did not fit the context window")
        return reply

    def _extract_within(
        self, goal: str, query: str, sources: Sequence[Source], today: date, budget: int
    ) -> tuple[Extraction, int]:
        """The model's extraction, and how many readable pages the budget had to leave out."""
        blocks, left_out = self._blocks(query, sources, budget)
        if not blocks:
            return Extraction(answer="None of the sources could be read.", findings=[]), left_out
        messages = prompts.extract_messages(goal, today, blocks, max_findings=MAX_FINDINGS)
        return self._generate(messages, Extraction, purpose="extract"), left_out

    def _blocks(self, query: str, sources: Sequence[Source], budget: int) -> tuple[list[str], int]:
        """The passages most relevant to *query* that fit *budget*, fenced for the prompt
        source by source, and how many readable pages did not fit."""
        packed = pack(
            [(source.index, source.text) for source in sources if source.text],
            query,
            budget=budget,
            per_source=self._options.max_source_chars,
        )
        left_out = sum(1 for s in sources if not s.snippet_only and s.index not in packed)
        blocks = [prompts.source_block(s, packed[s.index]) for s in sources if s.index in packed]
        return blocks, left_out

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
    """Keep the hits most about the goal (IDF-weighted term coverage), a few per site at most,
    in search order.

    What earlier runs learned about the sites comes second: it reorders relevant hits a little
    and skips sites that keep failing, but never makes an off-topic hit relevant. Relevance and
    reputation decide which pages are read, never their numbers, so the same pages make the
    same prompts however the sites' records changed.
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

    chosen: list[int] = []
    per_site: Counter[str] = Counter()
    for i in candidates:
        site = hostname(hits[i].url)
        if per_site[site] < _MAX_PER_SITE:
            chosen.append(i)
            per_site[site] += 1
        if len(chosen) == limit:
            break
    off_topic = len(hits) - len(on_topic)
    if filtered and off_topic:
        warnings.append(f"ignored {off_topic} off-topic search result(s)")
    return [hits[i] for i in sorted(chosen)]


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
    now = {source.reading: source for source in after}
    if not before or len(now) != len(after) or len(before) != len(after):
        return None
    if set(now) != {source.reading for source in before}:
        return None
    return [replace(now[source.reading], index=source.index) for source in before]


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


def _cut(text: str, whole: int, warnings: list[str]) -> str:
    if len(text) < whole:
        warnings.append(f"only the first {len(text):,} characters were checked")
    return text


def _independent(
    hits: Sequence[SearchHit],
    documents: Sequence[Document],
    avoid: Collection[str],
    warnings: list[str],
) -> tuple[list[SearchHit], list[Document]]:
    """The pages read that did not land on an avoided site (a short link, an aggregator)."""
    kept = [
        (hit, doc)
        for hit, doc in zip(hits, documents, strict=True)
        if not (doc.final_url and matches_any(doc.final_url, avoid))
    ]
    if len(kept) < len(hits):
        where = ", ".join(sorted(avoid))
        warnings.append(f"left out {len(hits) - len(kept)} page(s) that redirect to {where}")
    return [hit for hit, _ in kept], [doc for _, doc in kept]


def _refuse_private(url: str) -> None:
    address = private_address(url)
    if address is not None:
        raise ScoutError(f"will not read {url}: it is on a private network ({address})")


def _read_key(
    plan: Plan, exclude: Collection[str], avoid: Collection[str], options: ResearchOptions
) -> str:
    what = [plan.queries, plan.kind, plan.recency, sorted(exclude), sorted(avoid)]
    encoded = json.dumps([*what, options.max_results, options.region], ensure_ascii=False)
    return "read:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _unreadable_warning(sources: Sequence[Source]) -> list[str]:
    failed = Counter(source.status.replace("_", " ") for source in sources if source.snippet_only)
    if not failed:
        return []
    detail = ", ".join(f"{count} {status}" for status, count in failed.most_common())
    total = sum(failed.values())
    return [
        f"{total} of {len(sources)} pages could not be read ({detail}); used their search snippets"
    ]
