"""A research run: plan, search, read, pack, extract, verify, assess. And a fact-check: the same
for each claim a text makes."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from scout.clock import utcnow
from scout.errors import (
    AnswerPending,
    ContextOverflow,
    FetchError,
    LLMError,
    ScoutError,
    SearchError,
    StructuredOutputError,
    UnsupportedResponseFormat,
)
from scout.llm.base import Backend, Message
from scout.llm.structured import StructuredMode, generate
from scout.research import citations, factcheck, prompts
from scout.research.plan import heuristic_plan, model_plan
from scout.research.rank import coverage, pack, split_chunks
from scout.research.reputation import SiteBook, SiteRecord
from scout.research.results import CHECK_KIND, Finding, Flag, Plan, RunResult, Source
from scout.research.schema import (
    AllClaims,
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
from scout.web import moved, soft404
from scout.web.archive import Archive, Snapshot, in_archive, working
from scout.web.domains import (
    canonical_url,
    hostname,
    matches_any,
    private_address,
    registrable_domain,
)
from scout.web.fetch import Document, Fetcher, FetchStatus
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
_CITED_BATCH = 12  # cited pages read under one fetch deadline


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
    find_moved: bool = False  # a cite-check searches the sites of a dead page for where it moved


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
        archive: Archive | None = None,
    ) -> None:
        self._search_backend = search
        self._fetcher = fetcher
        self._backend = backend
        self._options = options or ResearchOptions()
        self._clock = clock
        self._sites = sites  # what earlier runs learned about sites; None: nothing
        self._pins = pins
        self._pin_scope = pin_scope  # who runs it (a watch): runs of one goal pin apart
        self._archive = archive  # where a cite-check looks up copies of dead pages; None: nowhere
        self._reads: dict[str, Any] = {}  # what this run read, kept if it must wait for an answer
        self._kept: set[str] = set()  # keys of _reads that are in the store
        self._resumable = False  # the run keeps what it reads however it stops
        self._run_key = ""
        self._asked = 0  # model requests made, to tell whether a check asked anything
        # A check's archived copies, by the citation number they stand in for, with why each
        # gives no evidence; the lookups it made; and why the archive stopped answering.
        self._copies: dict[int, tuple[Source | None, str | None]] = {}
        self._looked_up = 0
        self._archive_down: str | None = None
        # Where each cited address led; the live page each dead one moved to, by its number;
        # and why the search engine stopped answering.
        self._landed: dict[str, str] = {}
        self._moves: dict[int, Document | None] = {}
        self._search_down: str | None = None
        self._avoid: frozenset[str] = frozenset()  # the checked page's sites: never evidence
        self._cited_on: datetime | None = None  # when the checked page was written
        self._progress: Callable[[str], None] = _quiet
        self.structured_mode = self._options.structured_mode  # may drop to PROMPT during a run

    @property
    def asked(self) -> int:
        """Model requests made so far."""
        return self._asked

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

    def _pinned(
        self, goal: str, mode: str, work: Callable[[], RunResult], *, resumable: bool = False
    ) -> RunResult:
        """Do *work*, keeping what it read while it waits for a model answer: started again, the
        run reads the same pages on the same day, so its prompts (and their answers) match.

        A *resumable* run keeps what it read and was told when it stops for the model (an error,
        a wait), an interrupt or a crash, so that started again it continues where it stopped.
        It keeps the model's answers, so another model starts it afresh. Stopped for anything
        else (a page that could not be read), it keeps nothing: started again, it reads again.
        """
        self._reads, self._kept, self._resumable = {}, set(), resumable
        scope = [goal, mode, self._pin_scope, self._options.public_only]
        if resumable:
            scope.append(self._backend.model)
        self._run_key = json.dumps(scope, ensure_ascii=False)
        if self._pins is None:
            return work()
        self._pins.drop_pins(self._run_key, before=self._clock() - PIN_TTL)
        try:
            result = work()
        except BaseException as exc:
            if resumable and (isinstance(exc, LLMError) or not isinstance(exc, ScoutError)):
                self._keep()
            elif isinstance(exc, AnswerPending):
                self._pins.put_pins(self._run_key, self._reads, self._clock())
            elif isinstance(exc, Exception):
                self._pins.drop_pins(self._run_key)
            raise
        self._pins.drop_pins(self._run_key)
        return result

    def _pin(self, key: str) -> Any | None:
        if self._pins is None:
            return None
        pinned = self._pins.get_pin(self._run_key, key, newer_than=self._clock() - PIN_TTL)
        if pinned is not None:
            self._kept.add(key)
        return pinned

    def _keep(self) -> None:
        """Store what a resumable run read and was told since it last did: a run that dies
        before it can say so keeps it too. What is stored already is not written again."""
        if not self._resumable or self._pins is None:
            return
        new = {key: value for key, value in self._reads.items() if key not in self._kept}
        if new:
            self._pins.put_pins(self._run_key, new, self._clock())
            self._kept.update(new)

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

    def check(
        self,
        subject: str,
        *,
        max_claims: int = factcheck.MAX_CLAIMS,
        reuse: RunResult | None = None,
        cited: bool = False,
        audit: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Fact-check a text, or the page at a web address, claim by claim.

        The model lists the claims and quotes pages on each. Scout keeps only the claims the
        text makes, and rules on each from the quotes it verified, so no ruling rests on the
        model's word. Pages from the checked page's own site are never evidence.

        With the public_only option (callers a web page may have steered, such as an assistant
        over MCP) an address on a private network is refused rather than read.

        *reuse*, an earlier check of the same text (a claim watch's last run), keeps the claims
        steady: they are not listed again, and a claim whose pages read exactly as they did then
        keeps its evidence without asking the model.

        *cited* makes it a cite-check: each claim is judged only on the pages its sentence
        cites, so the question is whether the text's own sources say it. Nothing is searched.
        A web page cites what its links and footnotes lead to on other sites.

        *audit* (with *cited*) checks every cited sentence rather than the first few claims,
        however long the text: its claims are listed a part at a time, and up to AUDIT_LIMIT
        are judged. An audit keeps what it read and judged however it stops, so the same audit
        started again within a day continues where it stopped. *progress* hears of each part
        listed and each claim checked. With *reuse*, an earlier audit (a citation watch's last
        run), the claims of sentences still in the text are kept, only the parts holding a new
        or edited cited sentence are listed, and a claim whose cited pages read as they did
        then keeps its evidence.
        """
        if audit and not cited:
            raise ValueError("an audit is a cite-check: it needs cited=True")
        subject = clean(subject)
        if not subject:
            raise ScoutError("nothing to check")
        return self._pinned(
            hashlib.sha256(subject.encode()).hexdigest(),
            "audit" if audit else f"{'cite' if cited else 'check'}:{max_claims}",
            lambda: self._check(
                subject,
                max_claims,
                None if cited and not audit else reuse,
                cited,
                audit,
                progress or _quiet,
            ),
            resumable=audit,
        )

    def _check(
        self,
        subject: str,
        max_claims: int,
        reuse: RunResult | None,
        cited: bool,
        audit: bool,
        progress: Callable[[str], None],
    ) -> RunResult:
        started = self._started()
        today = started.date()
        warnings: list[str] = []
        self._copies, self._looked_up, self._archive_down = {}, 0, None
        self._landed, self._moves, self._search_down = {}, {}, None
        self._progress = progress
        text, page = self._subject(subject, links=cited)
        # A dead page's copy from before the page citing it was last written, when it says
        # when: what its author read, not what the address held later.
        written = (page.updated or page.published) if page is not None else None
        self._cited_on = datetime.combine(written, time.max, UTC) if written else None
        origin = (subject, page.final_url or "") if page is not None else ()
        avoid = frozenset(registrable_domain(host) for host in map(hostname, origin) if host)
        self._avoid = avoid
        pages: dict[int, str] = {}
        if cited:
            text, pages, clashes = citations.cited(text, own=avoid)
            if not pages:
                raise ScoutError(citations.NONE_CITED if page is None else citations.NONE_LINKED)
            warnings.extend(clashes)
        asked = self._asked
        covered = factcheck.Coverage()
        if audit:
            if reuse is not None and not reuse.audit:
                reuse = None
            title = page.title if page is not None else None
            prose = text[: factcheck.back_matter(text)] if page is not None else text
            if len(prose) < len(text):
                heading = text[len(prose) :].split("\n", 1)[0].strip("# ")
                warnings.append(f'the page\'s back matter, from "{heading}" on, was not audited')
            claims, planner, set_aside, covered = self._audit_claims(
                prose, pages, title, today, warnings, progress, reuse
            )
        elif (
            reuse is not None
            and reuse.plan.kind == CHECK_KIND
            and reuse.claims
            and reuse.checked_text == text
        ):
            claims = [
                ClaimToCheck(claim=c.claim, excerpt=c.excerpt, query=c.query) for c in reuse.claims
            ]
            planner, set_aside = reuse.plan.planner, 0
        else:
            reuse = None
            claims, planner, text, set_aside = self._claims(
                text, today, max_claims, warnings, cited=cited
            )
        if cited and not audit:
            claims, uncited = factcheck.citing(claims, text, pages)
            claims = claims[:max_claims]
            warnings.extend(uncited)
            if (partial := factcheck.partly_checked(claims, text, pages)) is not None:
                warnings.append(partial)
        plan = Plan(
            queries=() if cited else tuple(claim.query for claim in claims),
            kind=CHECK_KIND,
            recency=None,
            planner=planner,
        )
        sources: list[Source] = []
        if cited and reuse is not None:
            # Every cited page at once, in parallel: a run judging nothing takes no longer.
            every = {
                n: pages[n]
                for claim in claims
                for n in _cited_pages(claim, text, pages)[: factcheck.MAX_CITED_PAGES]
            }
            self._read_cited(every, sources, warnings)
        weighed = []
        notes: dict[str, list[int]] = {}
        for number, claim in enumerate(claims, start=1):
            self._keep()
            log.info("checking claim %d of %d", number, len(claims))
            progress(f"claim {number} of {len(claims)}")
            noted: list[str] = []
            if cited:
                item = self._check_cited(claim, text, pages, sources, today, noted, reuse)
            else:
                searched = replace(plan, queries=(claim.query,))
                item = self._check_claim(claim, searched, avoid, sources, today, noted, reuse)
            weighed.append(item._replace(caveat=factcheck.caveat(claim, text)))
            for note in noted:
                notes.setdefault(note, []).append(number)
        warnings.extend(_per_claim(notes))
        if cited and (kept := sum(item.kept for item in weighed)):
            warnings.append(
                f"{kept} of {len(claims)} claims kept their evidence: the pages they cite read "
                "as they did in the last run"
            )
        findings, checks = factcheck.assemble(weighed)
        answer, confidence = factcheck.summarize(
            checks, findings, sources, set_aside=set_aside, cited=cited, skipped=covered.unchecked
        )
        if checks and covered.line:
            answer = f"{answer} {covered.line}"
        named = subject if page is not None else shorten(" ".join(text.split()), 80)
        kind = "citation audit" if audit else "cite-check" if cited else "fact-check"
        return RunResult(
            goal=f"{kind}: {named}",
            started_at=started,
            finished_at=self._clock(),
            model=self._backend.model,
            plan=plan,
            sources=tuple(sorted(sources, key=lambda source: source.index)),
            answer=answer,
            findings=tuple(findings),
            confidence=confidence,
            warnings=tuple(warnings),
            carried_over=(
                reuse is not None and any(item.kept for item in weighed) and self._asked == asked
            ),
            claims=tuple(checks),
            checked_text=text,
            cited=cited,
            audit=audit,
            skipped=covered.skipped,
            unread=covered.unread,
            cites=pages,
        )

    def _subject(self, subject: str, *, links: bool) -> tuple[str, Document | None]:
        """The text to check, and the page it comes from when *subject* is a web address: that
        page is read once and pinned, like the pages a run reads. With *links*, its text keeps
        the links and notes it cites. A page that answers but is gone (soft404) is not read."""
        if not factcheck.web_address(subject):
            return subject, None
        public_only = self._options.public_only
        if public_only:
            _refuse_private(subject)
        pinned = self._pin("subject")
        if pinned:
            page = Document.from_dict(pinned)
        else:
            deadline = self._options.fetch_deadline
            read = self._fetcher.fetch_many([subject], deadline=deadline, links=links)
            (page,) = soft404.screened(self._fetcher, read, deadline=deadline, links=links)
        if public_only and page.final_url:
            _refuse_private(page.final_url)
        if not page.ok or not page.text:
            reason = page.error or page.status.value.replace("_", " ")
            raise ScoutError(f"could not read {subject}: {reason}")
        self._reads["subject"] = page.to_dict()
        text = page.text
        if page.title and not fold(text).startswith(fold(page.title)):
            text = f"{page.title}\n\n{text}"
        return clean(text), page

    def _claims(
        self, text: str, today: date, max_claims: int, warnings: list[str], *, cited: bool = False
    ) -> tuple[list[ClaimToCheck], str, str, int]:
        """The claims to check, who chose them (the model, or the heuristic of checking every
        sentence when the model's list is unusable), the text as checked (cut to what fits the
        model's context window), and how many listed claims the text does not make. For a
        cite-check there may be more: those its sentences cite no page for are dropped later."""
        budget = source_budget(self._options.context_tokens)
        whole = len(text)
        try:
            try:
                text, listed = self._listed(text, today, max_claims, budget, cited)
            except ContextOverflow:
                # Token estimates are approximate; the server's verdict wins. One smaller retry.
                text, listed = self._listed(text, today, max_claims, int(budget * 0.6), cited)
        except StructuredOutputError as exc:
            warnings.append(
                "checked the text sentence by sentence: "
                f"the model's list of claims was unusable ({exc})"
            )
            text = _cut(text[:budget], whole, warnings, cited=cited)
            return factcheck.sentences(text, None if cited else max_claims), "heuristic", text, 0
        limit = factcheck.CLAIM_LIMIT if cited else max_claims
        claims, set_aside = factcheck.anchored(listed.claims, text, limit, cited=cited)
        warnings.extend(set_aside)
        return claims, "model", _cut(text, whole, warnings, cited=cited), len(set_aside)

    def _listed(
        self, text: str, today: date, max_claims: int, budget: int, cited: bool
    ) -> tuple[str, ClaimList]:
        text = text[:budget]
        messages = prompts.claims_messages(text, today, max_claims=max_claims, cited=cited)
        return text, self._generate(messages, ClaimList, purpose="claims")

    def _audit_claims(
        self,
        text: str,
        pages: Mapping[int, str],
        title: str | None,
        today: date,
        warnings: list[str],
        progress: Callable[[str], None],
        reuse: RunResult | None,
    ) -> tuple[list[ClaimToCheck], str, int, factcheck.Coverage]:
        """An audit's claims: every claim the model lists in the sentences of *text* citing
        *pages*, a part at a time, in the order of the text and at most AUDIT_LIMIT. Also who
        listed them, how many it listed that the text does not make, and what they cover.

        Listing stops once AUDIT_LIMIT claims, or cited sentences, are listed. A part whose
        list is unusable is checked sentence by sentence; the others still count as the model's.

        With *reuse*, an earlier audit, its claims still made in *text* are kept where they
        stand, and only parts holding a cited sentence it did not know are listed, keeping the
        claims of those sentences: the model is never asked to list a sentence again.
        """
        cut = factcheck.parts(text, pages, source_budget(self._options.context_tokens))
        limit = factcheck.AUDIT_LIMIT
        earlier, known = (
            factcheck.kept_claims(reuse, text, pages) if reuse is not None else ([], set())
        )
        todo = [
            number
            for number, (_, _, part) in enumerate(cut, start=1)
            if any(
                factcheck.addressed(sentence, pages) not in known
                for sentence in factcheck.cited_sentences(part, pages)
            )
        ]
        found: list[ClaimToCheck] = []
        seen: set[tuple[str, tuple[int, ...]]] = set()
        kept = listed = set_aside = upto = 0
        planner = reuse.plan.planner if reuse is not None and earlier else "heuristic"

        def add(claims: Sequence[ClaimToCheck]) -> list[ClaimToCheck]:
            new = []
            for claim in claims:
                key = (fold(claim.claim), factcheck.cited_by(text, claim.excerpt))
                if key not in seen:
                    seen.add(key)
                    new.append(claim)
            found.extend(new)
            return new

        for number, (_, end, part) in enumerate(cut, start=1):
            if max(kept, listed) >= limit:
                break
            folded = fold(part)
            here = [factcheck.place(folded, claim.excerpt) is not None for claim in earlier]
            kept += len(add([c for c, inside in zip(earlier, here, strict=True) if inside]))
            earlier = [c for c, inside in zip(earlier, here, strict=True) if not inside]
            if number in todo:
                numbered = (number, len(cut)) if len(cut) > 1 else None
                new = "new " if reuse is not None else ""
                progress(f"listing {new}claims: part {todo.index(number) + 1} of {len(todo)}")
                self._keep()
                try:
                    claims = self._part_claims(part, pages, numbered, title, today, warnings)
                except StructuredOutputError as exc:
                    warnings.append(
                        f"{_in_part(numbered)}checked the cited sentences as written: "
                        f"the model's list of claims was unusable ({exc})"
                    )
                    claims, _ = factcheck.citing(factcheck.sentences(part), part, pages)
                else:
                    planner = "model"
                    claims, aside = factcheck.anchored(claims, text, len(claims), cited=True)
                    warnings.extend(aside)
                    set_aside += len(aside)
                claims = factcheck.unknown(claims, text, pages, known)
                kept += len(factcheck.citing(add(claims), text, pages)[0])
            listed += len(factcheck.cited_sentences(part, pages))
            upto = end
        else:
            upto = len(text)
            add(earlier)  # a kept claim whose passage runs across two parts
        claims, uncited = factcheck.citing(found, text, pages)
        warnings.extend(uncited)
        folded = fold(text)
        claims.sort(key=lambda claim: (factcheck.place(folded, claim.excerpt) or (len(folded),))[0])
        stopped = None
        if upto < len(text):
            what = "claims" if kept >= limit else "cited sentences"
            stopped = f"its limit of {limit} {what}"
        elif len(claims) > limit:
            stopped = f"its limit of {limit} claims"
        claims = claims[:limit]
        covered = factcheck.coverage(claims, text, pages, upto=upto, stopped=stopped)
        return claims, planner, set_aside, covered

    def _part_claims(
        self,
        part: str,
        pages: Mapping[int, str],
        numbered: tuple[int, int] | None,
        title: str | None,
        today: date,
        warnings: list[str],
    ) -> list[ClaimToCheck]:
        """The claims the model lists in one part of an audited text. A part the server finds
        too long is listed again in smaller pieces; a piece too long even so is told, and its
        sentences go unchecked."""
        try:
            return self._listed_part(part, pages, numbered, title, today)
        except ContextOverflow:
            # Token estimates are approximate; the server's verdict wins.
            budget = int(source_budget(self._options.context_tokens) * 0.6)
            pieces = factcheck.parts(part, pages, budget)
        claims: list[ClaimToCheck] = []
        for _, _, piece in pieces:
            try:
                claims += self._listed_part(piece, pages, numbered, title, today)
            except ContextOverflow:
                warnings.append(
                    f"{_in_part(numbered)}a passage of {len(piece):,} characters did not fit the "
                    "model's context window: its cited sentences were not checked"
                )
        return claims

    def _listed_part(
        self,
        part: str,
        pages: Mapping[int, str],
        numbered: tuple[int, int] | None,
        title: str | None,
        today: date,
    ) -> list[ClaimToCheck]:
        """The model's list of every cited claim in *part*: pinned, so a resumed audit does
        not ask for it again."""
        key = _pin_key("listed", part)
        listed = self._pin(key)
        if listed is None:
            messages = prompts.claims_messages(
                part,
                today,
                max_claims=2 * len(factcheck.cited_sentences(part, pages)),
                cited=True,
                every=True,
                part=numbered,
                title=title,
            )
            listed = self._generate(messages, AllClaims, purpose="claims").model_dump()
        self._reads[key] = listed
        return AllClaims.model_validate(listed).claims

    def _check_claim(
        self,
        claim: ClaimToCheck,
        plan: Plan,
        avoid: Collection[str],
        sources: list[Source],
        today: date,
        warnings: list[str],
        reuse: RunResult | None,
    ) -> factcheck.Weighed:
        """Search and read for one claim, leaving out the sites *avoid*, then weigh the quotes
        the model finds on its pages, unless *reuse* weighed the same pages already. The pages
        join *sources*, the run's, keeping their number if an earlier claim read them. A reply
        that is unusable leaves this claim unclear (and judged again next time); a model that is
        down or still to answer stops the check."""
        try:
            found = self._read(claim.claim, plan, warnings, avoid=avoid)
        except SearchError as exc:
            warnings.append(str(exc))
            return factcheck.Weighed(claim, [], [], None, (str(exc),))
        found = factcheck.renumber(found, sources)
        kept = factcheck.carried(claim, reuse, found) if reuse is not None else None
        if kept is not None:
            warnings.append("no page changed since the last check; its evidence was kept")
            return kept
        try:
            judgment = self._judge(claim.claim, found, today, warnings)
        except StructuredOutputError as exc:
            unjudged = f"{factcheck.NOT_JUDGED} ({exc})"
            warnings.append(unjudged)
            return factcheck.Weighed(claim, [], [], None, (unjudged,))
        supports, refutes = factcheck.weigh(claim.claim, judgment, found)
        problems = (factcheck.NOTHING_READ,) if factcheck.NOTHING_READ in warnings else ()
        note = clean(judgment.note) or None
        pages = tuple(source.index for source in found)
        return factcheck.Weighed(claim, supports, refutes, note, problems, pages=pages)

    def _check_cited(
        self,
        claim: ClaimToCheck,
        text: str,
        pages: Mapping[int, str],
        sources: list[Source],
        today: date,
        warnings: list[str],
        reuse: RunResult | None,
    ) -> factcheck.Weighed:
        """Weigh the quotes the model finds for one claim on the pages its sentence cites, and
        on no other page, so that a quote can count only for a page the text cites for it. A
        claim none of whose pages could be read costs no model request, nor does one whose
        pages read as they did for *reuse*, an earlier audit: it keeps its evidence. With an
        archive, a claim citing a dead page is also judged on the copies of its dead pages, so
        that each dead citation can be replaced by a copy proven to back it, or by the live page
        it moved to, proven to hold the same quote.

        A sentence citing many pages is judged on the first few: a page, which chooses its
        own links, must not make one claim read hundreds of pages."""
        cites = _cited_pages(claim, text, pages)
        if len(cites) > factcheck.MAX_CITED_PAGES:
            warnings.append(
                f"its sentence cites {len(cites)} pages; "
                f"it was judged on the first {factcheck.MAX_CITED_PAGES}"
            )
            cites = cites[: factcheck.MAX_CITED_PAGES]
        found = self._read_cited({n: pages[n] for n in cites}, sources, warnings)
        problems = tuple(_unread(source) for source in found if source.snippet_only)
        kept = factcheck.carried(claim, reuse, found) if reuse is not None else None
        if kept is not None:
            return kept._replace(problems=problems)
        weighed = self._weighed(claim, found, cites, problems, today, warnings)
        if self._archive is not None:
            copies = self._on_copies(claim, found, pages, sources, today, warnings)
            weighed = weighed._replace(archived=copies)
        return weighed

    def _weighed(
        self,
        claim: ClaimToCheck,
        found: Sequence[Source],
        on: tuple[int, ...],
        problems: tuple[str, ...],
        today: date,
        warnings: list[str],
    ) -> factcheck.Weighed:
        """*claim* weighed on *found*, the pages numbered *on*, and on no other page; none
        that could be read, no model request. A reply that is unusable leaves it not judged."""
        readable = sum(1 for source in found if not source.snippet_only)
        if not readable:
            return factcheck.Weighed(claim, [], [], None, problems, pages=on)
        # A page cited alone may fill the context window, not a page's usual share of it.
        share = source_budget(self._options.context_tokens) // readable
        per_source = max(self._options.max_source_chars, share)
        try:
            judgment = self._judged(claim.claim, found, today, warnings, per_source)
        except StructuredOutputError as exc:
            unjudged = f"{factcheck.NOT_JUDGED} ({exc})"
            warnings.append(unjudged)
            return factcheck.Weighed(claim, [], [], None, (*problems, unjudged), pages=on)
        supports, refutes = factcheck.weigh(claim.claim, judgment, found)
        note = clean(judgment.note) or None
        return factcheck.Weighed(claim, supports, refutes, note, problems, pages=on)

    def _on_copies(
        self,
        claim: ClaimToCheck,
        found: Sequence[Source],
        pages: Mapping[int, str],
        sources: list[Source],
        today: date,
        warnings: list[str],
    ) -> factcheck.Weighed | None:
        """*claim* judged on the archive's newest working copies of the pages among *found*
        (those it cites) that are gone, and on no page that could be read: what they said when
        archived, never what they say now. None when no page it cites is gone.

        A dead page whose copy backs the claim is looked for where it moved; the quote that
        backed it on the copy, verified again on the live page found, backs it there too, and
        that page joins the run's sources. It is not one of the pages the claim was judged on."""
        dead = [page for page in found if factcheck.gone(page)]
        if not dead:
            return None
        copies, problems = [], []
        for page in dead:
            copy, problem = self._copy(page, pages, sources, warnings)
            copies += [(page, copy)] if copy is not None else []
            problems += [problem] if problem is not None else []
        on = tuple(copy.index for _, copy in copies)
        read = [copy for _, copy in copies]
        weighed = self._weighed(claim, read, on, tuple(problems), today, warnings)
        on_live = []
        for page, copy in copies:
            backing = [f for f in weighed.supports if f.trusted and f.source == copy.index]
            if not backing:
                continue
            live = self._moved(page, copy, backing[0].quote, pages, sources, warnings)
            if live is None:
                continue
            again = factcheck.found_again(claim.claim, backing[0], live)
            if again is not None:
                on_live.append(again)
                if all(source.index != live.index for source in sources):
                    sources.append(live)
        return weighed._replace(supports=[*weighed.supports, *on_live])

    def _moved(
        self,
        page: Source,
        copy: Source,
        quote: str,
        pages: Mapping[int, str],
        sources: Sequence[Source],
        warnings: list[str],
    ) -> Source | None:
        """The live page that *page*, a dead cited page whose archived *copy* backs a claim with
        *quote*, moved to, as a source numbered after every other (or None): proven by what it
        holds (web/moved), never by its address. It is looked for once a run, and searched for
        only with the find_moved option, until the search engine fails. A site that no longer
        resolves sent the page nowhere."""
        n = page.index
        known = next((source for source in sources if source.moved_from == n), None)
        if known is not None:
            return known
        if page.status == FetchStatus.NETWORK_ERROR:
            return None
        if n not in self._moves:
            self._moves[n] = self._relocated(page, copy, quote, warnings)
        found = self._moves[n]
        if found is None:
            return None
        address = found.final_url or found.url
        hit = SearchHit(
            url=address, title=copy.title, snippet="", rank=1, query=f"new address of [{n}]"
        )
        (read,) = _sources([hit], [found])
        index = max([*pages, *(source.index for source in sources)]) + 1
        return replace(read, index=index, moved_from=n)

    def _relocated(
        self, page: Source, copy: Source, quote: str, warnings: list[str]
    ) -> Document | None:
        """Where *page* moved, read: pinned like a page (whether it was searched for, too), so
        a resumed audit searches and reads nothing again; a search that failed is not."""
        search_too = self._options.find_moved and self._search_down is None
        key = _pin_key("moved", [canonical_url(page.url), search_too])
        lookup = self._reads.get(key) or self._pin(key)
        if lookup is None:
            self._progress(f"looking for where [{page.index}] moved")
            try:
                found = moved.relocate(
                    self._fetcher,
                    self._search_backend,
                    url=page.url,
                    landed=self._landed.get(page.url),
                    copy_text=copy.text,
                    quote=quote,
                    own=self._avoid,
                    region=self._options.region,
                    deadline=self._options.fetch_deadline,
                    search_too=search_too,
                )
            except SearchError as exc:
                self._search_down = str(exc)
                warnings.append(
                    f"the search engine did not answer ({exc}): later dead pages were not looked "
                    "for at new addresses"
                )
                return None
            lookup = {"document": found.to_dict() if found is not None else None}
        self._reads[key] = lookup
        return Document.from_dict(lookup["document"]) if lookup["document"] else None

    def _copy(
        self, page: Source, pages: Mapping[int, str], sources: list[Source], warnings: list[str]
    ) -> tuple[Source | None, str | None]:
        """The archive's copy of *page*, a dead cited page, as a source of the run numbered after
        every citation (or None), and why it gives no evidence (or None). A page is looked up
        only if factcheck.archivable allows it, once a run; at most ARCHIVE_LIMIT pages are, and
        none after the archive failed to answer. A lookup is pinned like a page, so a resumed
        check judges the very same copy; a failed one is not."""
        n = page.index
        if not factcheck.archivable(page):
            why = "it is an archived copy already" if in_archive(page.url) else "no public name"
            return None, factcheck.not_looked_up(n, why)
        if n in self._copies:
            return self._copies[n]
        key = _pin_key("archived", canonical_url(page.url))
        lookup = self._reads.get(key)  # looked up already, for this page under another number
        if lookup is None:
            limit = factcheck.ARCHIVE_LIMIT
            if self._looked_up >= limit:
                why = f"at most {limit} cited pages are looked up in a run"
                return None, factcheck.not_looked_up(n, why)
            lookup = self._pin(key)
            if lookup is None and self._archive is not None and self._archive_down is None:
                self._progress(f"looking up [{n}] on the Wayback Machine")
                try:
                    lookup = _lookup(self._archive, page.url, before=self._cited_on)
                except FetchError as exc:
                    self._archive_down = str(exc)
                    warnings.append(
                        f"the Wayback Machine did not answer ({exc}): later unreadable cited "
                        "pages were not looked up"
                    )
            if lookup is None:
                why = f"the Wayback Machine did not answer ({self._archive_down})"
                return None, factcheck.not_looked_up(n, why)
            self._looked_up += 1
            self._reads[key] = lookup
        if lookup["snapshot"] is None:
            self._copies[n] = None, factcheck.no_copy(n)
            return self._copies[n]
        snapshot = Snapshot(lookup["snapshot"][0], datetime.fromisoformat(lookup["snapshot"][1]))
        hit = SearchHit(
            url=snapshot.page, title=page.title, snippet="", rank=1, query=f"archived copy of [{n}]"
        )
        (read,) = _sources([hit], [Document.from_dict(lookup["document"])])
        index = max([*pages, *(source.index for source in sources)]) + 1
        copy = replace(read, index=index, site=page.site, copy_of=n)
        sources.append(copy)
        problem = None
        if copy.snippet_only:
            problem = f"could not read the archived copy of [{n}] ({factcheck.why_unread(copy)})"
        self._copies[n] = copy, problem
        return self._copies[n]

    def _read_cited(
        self, cited: Mapping[int, str], sources: list[Source], warnings: list[str]
    ) -> list[Source]:
        """The pages *cited* (by their citation numbers), each read once a run: a page read
        for an earlier claim is not read again, even under another number. A page that answers
        but is gone (soft404) reads as not found. What is read is pinned like a search's pages,
        so a resumed check judges the very same pages, and probes no site again."""
        known = {source.index: source for source in sources}
        by_url = {
            source.url: source
            for source in sources
            if source.copy_of is None and source.moved_from is None
        }
        new = {n: url for n, url in cited.items() if n not in known and url not in by_url}
        if new:
            listed = json.dumps(list(new.items()))
            key = "cited:" + hashlib.sha256(listed.encode("utf-8")).hexdigest()[:16]
            pinned = self._pin(key)
            if pinned is None:
                # A few pages at a time, each batch with its own deadline: hundreds of cited
                # pages under one would leave the last abandoned, run after run.
                urls = list(dict.fromkeys(new.values()))
                deadline = self._options.fetch_deadline
                fetched: list[Document] = []
                for start in range(0, len(urls), _CITED_BATCH):
                    batch = urls[start : start + _CITED_BATCH]
                    answered = self._fetcher.fetch_many(batch, deadline=deadline)
                    fetched += soft404.screened(self._fetcher, answered, deadline=deadline)
                read = dict(zip(urls, fetched, strict=True))
                pinned = [read[url].to_dict() for url in new.values()]
            self._reads[key] = pinned
            hits = [
                SearchHit(url=url, title=url, snippet="", rank=rank, query=f"cited as [{n}]")
                for rank, (n, url) in enumerate(new.items(), start=1)
            ]
            documents = [Document.from_dict(document) for document in pinned]
            self._landed.update(
                (url, document.final_url)
                for url, document in zip(new.values(), documents, strict=True)
                if document.final_url
            )
            for n, source in zip(new, _sources(hits, documents), strict=True):
                known[n] = by_url[source.url] = replace(source, index=n)
                sources.append(known[n])
                if source.snippet_only:
                    warnings.append(_unread(known[n]))
        for n, url in cited.items():
            if n not in known:
                known[n] = replace(by_url[url], index=n, query=f"cited as [{n}]")
                sources.append(known[n])
        return [known[n] for n in cited]

    def _judged(
        self,
        claim: str,
        sources: Sequence[Source],
        today: date,
        warnings: list[str],
        per_source: int,
    ) -> Judgment:
        """The model's judgment of *claim* on the cited *sources*. A resumable run pins it, with
        what judging it noted, so that started again it asks nothing again for this claim: the
        judgment is weighed as before, and rules the same."""
        if not self._resumable:
            return self._judge(claim, sources, today, warnings, per_source=per_source)
        key = _pin_key("judged", [claim, [source.reading for source in sources], per_source])
        judged = self._pin(key)
        if judged is None:
            noted: list[str] = []
            judgment = self._judge(claim, sources, today, noted, per_source=per_source)
            judged = {"judgment": judgment.model_dump(), "warnings": noted}
        self._reads[key] = judged
        warnings.extend(judged["warnings"])
        return Judgment.model_validate(judged["judgment"])

    def _judge(
        self,
        claim: str,
        sources: Sequence[Source],
        today: date,
        warnings: list[str],
        *,
        per_source: int | None = None,
    ) -> Judgment:
        """The model's judgment of *claim* on *sources*. With *per_source*, a page may fill
        more of the budget than its usual share, and a page sent only in part even so is told:
        what it says of the claim may be in the part left out."""
        partly: list[str] = []

        def ask(budget: int) -> tuple[Judgment, int]:
            packed = self._packed(claim, sources, budget, per_source)
            partly[:] = _partly_sent(sources, packed)
            blocks, left_out = _fenced(sources, packed)
            if not blocks:
                warnings.append(factcheck.NOTHING_READ)
                return Judgment(note=""), left_out
            messages = prompts.judge_messages(
                claim, today, blocks, max_evidence=factcheck.MAX_EVIDENCE
            )
            return self._generate(messages, Judgment, purpose="judge"), left_out

        judgment = self._fitted(ask, warnings)
        if per_source is not None:
            warnings.extend(partly)
        return judgment

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
        blocks, left_out = _fenced(sources, self._packed(query, sources, budget))
        if not blocks:
            return Extraction(answer="None of the sources could be read.", findings=[]), left_out
        messages = prompts.extract_messages(goal, today, blocks, max_findings=MAX_FINDINGS)
        return self._generate(messages, Extraction, purpose="extract"), left_out

    def _packed(
        self, query: str, sources: Sequence[Source], budget: int, per_source: int | None = None
    ) -> dict[int, list[str]]:
        """The passages of each source most relevant to *query* that fit *budget*."""
        return pack(
            [(source.index, source.text) for source in sources if source.text],
            query,
            budget=budget,
            per_source=per_source or self._options.max_source_chars,
        )

    def _generate(self, messages: list[Message], output: type[T], *, purpose: str) -> T:
        self._asked += 1
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


def _fenced(sources: Sequence[Source], packed: Mapping[int, list[str]]) -> tuple[list[str], int]:
    """The *packed* passages fenced for the prompt source by source, and how many readable
    pages did not fit."""
    left_out = sum(1 for s in sources if not s.snippet_only and s.index not in packed)
    blocks = [prompts.source_block(s, packed[s.index]) for s in sources if s.index in packed]
    return blocks, left_out


def _partly_sent(sources: Sequence[Source], packed: Mapping[int, list[str]]) -> list[str]:
    told = []
    for source in sources:
        sent = packed.get(source.index, [])
        if sent and len(sent) < len(split_chunks(source.text)):
            name = f"[{source.index}]"
            if source.copy_of is not None:  # the text's [n] are its citations
                name = f"the archived copy of [{source.copy_of}]"
            told.append(
                f"{name} is {len(source.text):,} characters; only the passages closest to the "
                "claim were read"
            )
    return told


def _unread(source: Source) -> str:
    """Why a cited page gave no evidence: "could not read [4] example.org (not found: HTTP 404)"."""
    return f"could not read [{source.index}] {source.site} ({factcheck.why_unread(source)})"


def _lookup(archive: Archive, url: str, *, before: datetime | None) -> dict[str, Any]:
    """The archive's newest working copy of *url* (taken before *before*), read, as a pin keeps
    it. FetchError when the archive did not answer, or did not serve the copy."""
    found = working(archive, url, rejected=soft404.error_page, before=before)
    if found is None:
        return {"snapshot": None, "document": None}
    snapshot, document = found
    return {"snapshot": [snapshot.url, snapshot.taken.isoformat()], "document": document.to_dict()}


def _cited_pages(claim: ClaimToCheck, text: str, pages: Mapping[int, str]) -> tuple[int, ...]:
    """The citation numbers of the web pages a claim's sentence cites, in order."""
    return tuple(n for n in factcheck.cited_by(text, claim.excerpt) if n in pages)


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


def _cut(text: str, whole: int, warnings: list[str], *, cited: bool = False) -> str:
    if len(text) < whole:
        cut = f"only the first {len(text):,} characters were checked"
        warnings.append(f"{cut}; {factcheck.EVERY_CITED}" if cited else cut)
    return text


def _per_claim(notes: Mapping[str, Sequence[int]]) -> list[str]:
    """Each note on the claims it was made for, once: "claims 4, 9, 12: [3] is 41,200
    characters; ...". An audit would otherwise repeat a long page's note for every claim
    citing it."""
    told = []
    for note, numbers in notes.items():
        claims = list(dict.fromkeys(numbers))
        told.append(f"claim{'s' if len(claims) > 1 else ''} {', '.join(map(str, claims))}: {note}")
    return told


def _quiet(step: str) -> None:
    """Progress that nobody follows."""


def _in_part(numbered: tuple[int, int] | None) -> str:
    return f"part {numbered[0]} of {numbered[1]}: " if numbered else ""


def _pin_key(kind: str, what: Any) -> str:
    encoded = json.dumps(what, ensure_ascii=False)
    return f"{kind}:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


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
