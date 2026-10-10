"""Scout as an MCP server: research, fact-checks, recall and watches as tools for AI assistants.

Everything a tool returns is evidence-first: each finding comes with the quote that backs it and
the page it is on, and findings Scout could not verify are listed apart with the reason.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

from scout import __version__
from scout.app import App
from scout.errors import AnswerPending, ConfigError, ScoutError
from scout.monitor.claims import READ, departed, noticed_for, ruling_keys
from scout.monitor.diff import Change, Delta
from scout.monitor.runner import WatchRun, continued, notifier_for, rules_of, run_watch
from scout.monitor.trends import series
from scout.monitor.watches import Watch, WatchBook
from scout.report import render_markdown
from scout.research.citations import relinked
from scout.research.deadlinks import dead_links, replacements
from scout.research.factcheck import (
    CLAIM_LIMIT,
    MAX_CLAIMS,
    PAGES_PER_CLAIM,
    cited_label,
    web_address,
)
from scout.research.results import ClaimCheck, Finding, RunResult
from scout.textutil import clean
from scout.web import fragments
from scout.web.archive import Copy, snapshot_of

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError as exc:  # an optional extra
    raise ConfigError("the MCP server needs the mcp package: pip install 'scout[mcp]'") from exc

INSTRUCTIONS = """\
Scout researches the web with the user's own local model and reports only facts it can back \
with a verbatim quote from a page; a quote's link opens its page with the quote highlighted, the \
link to cite. Try `recall` first: it answers instantly from what Scout has already verified. \
`research` reads the web now and can take a few minutes. `fact_check` checks a \
text (a draft answer, an article, a web address) claim by claim against independent pages. With \
cited, fact_check checks that the pages a text or a web page cites (its links, footnotes or \
numbered sources) state what it says; nothing is searched. Use it on your own answer before \
sending it. With archive as well, a claim citing a dead page is also judged on its newest \
archived copy (web.archive.org); with fix, it also returns the text with each dead citation \
replaced by a copy proven to back it, or by the live page it moved to, proven to hold the same \
quote (find_moved also searches for that page). Watches re-run a goal on a schedule and report \
what changed; a watch can also re-check a text's claims and report when a ruling changes. A \
citation watch (cited) audits the citations of a text or a web page on every run and reports \
when a cited page stops backing a claim or goes dead, with its archived copy; its first run must \
happen outside the assistant."""

Recency = Literal["day", "week", "month", "year", "any"]


def build(app: App) -> MCPServer:
    """The MCP server, with its tools working on *app*."""
    server = MCPServer(name="scout", version=__version__, instructions=INSTRUCTIONS)
    book = WatchBook(app.settings.watches_path)
    reads = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    browses = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

    @server.tool(annotations=browses)
    def research(
        goal: str, max_results: int = 6, recency: Recency | None = None, deep: bool = False
    ) -> dict[str, Any]:
        """Research a goal on the web now: an answer plus verified findings with their quotes.
        With deep, keep searching (up to 3 rounds) where the findings leave gaps."""
        options: dict[str, Any] = {"max_results": max(1, min(max_results, 20))}
        if recency is not None:
            options["recency"] = recency
        with _as_tool_errors():
            researcher = app.researcher(public_only=True, **options)
            result = researcher.run_deep(goal) if deep else researcher.run(goal)
            app.learn_from(researcher)
            run_id = app.store.add_run(result)
        return _brief(result, run_id)

    @server.tool(annotations=browses)
    def fact_check(
        text: str,
        max_claims: int = MAX_CLAIMS,
        cited: bool = False,
        archive: bool = False,
        fix: bool = False,
        find_moved: bool = False,
    ) -> dict[str, Any]:
        """Check a text (a draft answer, an article, or a web address) claim by claim against
        independent pages. Each ruling rests on quotes Scout found word for word on a page;
        quotes the model offered that are not there are listed under set_aside, and numbers
        the text states that no claim covered under unchecked. Addresses on private networks
        are refused. With cited, fact_check checks that the pages a text or a web page cites
        (its links, footnotes or numbered sources) state what it says; nothing is searched.
        Use it on your own answer before sending it: each claim's label says what its pages
        said ("backed", "contradicted", "disputed", "not found", "unreadable") and cites lists
        them. With archive (and cited), a claim citing a dead page is also judged on the newest
        archived copies of its dead pages: "archived" gives that reading, which never changes
        its label, with links to the quotes on the copies, and dead_links lists each dead page
        with what to do ("replace" gives the copy's link to cite instead). A dead page whose
        copy backs a claim is also looked for at the same path on the site it now redirects
        to, and with find_moved (cited: implies archive) searched for on that site and its own
        by one sentence of the copy: "moved" gives the live page that still holds the quote,
        whose link is then the one to cite. With fix (cited, a text: implies archive),
        fixed_text is the text with each dead citation whose archived copy backs it replaced
        by that copy, or by the page it moved to."""
        if fix and not cited:
            raise ToolError("fix applies to cited checks")
        if fix and web_address(clean(text)):
            raise ToolError("fix rewrites a text: pass the text itself")
        if find_moved and not cited:
            raise ToolError("find_moved applies to cited checks")
        archive = archive or fix or find_moved
        if archive and not cited:
            raise ToolError("archive applies to cited checks")
        with _as_tool_errors():
            researcher = app.researcher(
                public_only=True,
                max_results=PAGES_PER_CLAIM,
                archive=archive,
                find_moved=find_moved,
            )
            result = researcher.check(
                text, max_claims=max(1, min(max_claims, CLAIM_LIMIT)), cited=cited
            )
            app.learn_from(researcher)
            run_id = app.store.add_run(result)
        checked = _checked(result, run_id)
        if fix:
            checked["fixed_text"] = relinked(text, replacements(dead_links(result))).text
        return checked

    @server.tool(annotations=reads)
    def recall(question: str, limit: int = 8) -> dict[str, Any]:
        """What earlier research verified about a question: facts with quotes, no web access."""
        found = app.store.recall(question, limit=max(1, min(limit, 50)))
        return {"facts": [memory.to_dict() for memory in found]}

    @server.tool(annotations=reads)
    def report(run_id: int) -> str:
        """The full Markdown report of an earlier research run."""
        result = app.store.get_run(run_id)
        if result is None:
            raise ToolError(f"no run with id {run_id}")
        return render_markdown(result, run_id=run_id)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    def watch_add(
        name: str,
        goal: str,
        every: str = "1d",
        alerts: list[str] | None = None,
        check: bool = False,
        cited: bool = False,
    ) -> dict[str, Any]:
        """Watch a goal: re-research it on a schedule ("6h", "1d") and alert on changes.
        Alert rules: new, changed, price below 1800 USD, drop 5%, mentions "text", in stock.
        With check, goal is a short text of claims, fact-checked on each run, alerting when a
        claim's ruling changes because a page did; rules: changed, supported, refuted.
        With cited, goal is a web address or a text with citations, whose cited sentences are
        checked against the pages they cite on each run; rules: changed, backed,
        contradicted, dead (a cited page that could not be read twice in a row)."""
        with _as_tool_errors():
            watch = Watch(
                name=name,
                goal=goal,
                every=every,
                alerts=tuple(alerts or ()),
                check=check or cited,
                cited=cited,
            )
            book.add(watch)
        return _watch_view(app, watch)

    @server.tool(annotations=reads)
    def watch_list() -> dict[str, Any]:
        """The watches, with their schedules, alert rules and last runs."""
        with _as_tool_errors():
            return {"watches": [_watch_view(app, watch) for watch in book.load()]}

    @server.tool(annotations=browses)
    def watch_run(name: str) -> dict[str, Any]:
        """Run a watch now: what changed since its last run, and which alerts that raised.
        For a claim watch, "claims" gives each claim's ruling (as page-proven evidence has it)
        with the quote behind it ("gone": that quote left its page) and the evidence the model
        newly noticed, which is shown but never alerted on. For a citation watch, "labels"
        counts its claims' labels, "claims" lists those that are new or changed with the
        pages they cite, "pages" the cited pages that went dead (with the newest archived copy,
        and whether it still holds the quote; "archive" says "archived", "not archived", or "not
        looked up" when the archive did not answer) or came back, and "left" how many claims
        left the text. A citation watch's first run audits every cited sentence, so it is
        refused here: run `scout watch run NAME` or let `scout daemon` run it."""
        with _as_tool_errors():
            watch = book.get(name)
            if watch.cited and not _has_run(app, watch):
                raise ToolError(
                    f"citation watch {name} has not run yet: its first run audits every cited "
                    "sentence, which takes far longer than an assistant waits; run "
                    f"`scout watch run {name}` or let `scout daemon` run it"
                )
            outcome = run_watch(
                app, watch, notifier=notifier_for(watch), book=book, public_only=True
            )
        ran = {
            "run_id": outcome.run_id,
            "first_run": outcome.baseline,
            "model_asked": outcome.asked > 0,
            "changes": {change.value: count for change, count in outcome.counts.items()},
            "alerts": [trigger.reason for trigger in outcome.alerts],
        }
        if outcome.is_cited:
            ran.update(_audited(outcome))
        elif outcome.is_check:
            ran["claims"] = [
                {
                    "claim": delta.fact.claim,
                    "ruling": delta.fact.value,
                    "was": delta.previous.value if delta.previous is not None else None,
                    "change": delta.change.value,
                    "quote": delta.fact.quote,
                    "url": delta.fact.url,
                    "link": delta.fact.link,
                    "gone": departed(delta, outcome.deltas),
                    "noticed": [
                        {
                            "stance": fact.value,
                            "quote": fact.quote,
                            "url": fact.url,
                            "link": fact.link,
                        }
                        for fact in noticed_for(delta.fact.key, outcome.deltas, standing=True)
                    ],
                }
                for delta in outcome.rulings
            ]
        return ran

    @server.tool(annotations=reads)
    def watch_trend(name: str) -> dict[str, Any]:
        """How the prices and other numbers a watch follows have moved, run by run."""
        with _as_tool_errors():
            book.get(name)
        return {
            "series": [
                {
                    "value": line.label,
                    "in": line.measure,
                    "first": str(line.first),
                    "last": str(line.last),
                    "low": str(line.low),
                    "high": str(line.high),
                    "change_percent": None if line.change is None else round(float(line.change), 2),
                    "points": [[moment.isoformat(), str(amount)] for moment, amount in line.points],
                }
                for line in series(app.store.observations(name))
            ]
        }

    @server.tool(annotations=reads)
    def watch_changes(name: str, limit: int = 20) -> dict[str, Any]:
        """A watch's alerts, newest first, each with the page and quote it rests on."""
        with _as_tool_errors():
            book.get(name)
        alerts = app.store.alerts(name, limit=max(1, min(limit, 200)))
        return {
            "alerts": [
                {
                    "when": alert.created_at.isoformat(),
                    "alert": alert.reason,
                    "url": alert.url,
                    "link": alert.link,
                    "quote": alert.quote,
                    "delivered": alert.delivered_at is not None,
                }
                for alert in alerts
            ]
        }

    return server


@contextmanager
def _as_tool_errors() -> Iterator[None]:
    """Scout's expected failures, as messages the calling model can act on."""
    try:
        yield
    except AnswerPending as exc:
        raise ToolError(
            f"waiting for the model's answer: write it to {exc.request_path}, then call again"
        ) from exc
    except ScoutError as exc:
        raise ToolError(str(exc)) from exc


def _quoted(result: RunResult, finding: Finding) -> dict[str, Any]:
    """A finding's quote with its page, and the link that opens the page at it."""
    quoted: dict[str, Any] = {"quote": finding.quote}
    source = result.source(finding.source)
    if source is not None:
        date = source.freshest_date
        quoted |= {
            "url": source.url,
            "site": source.site,
            "date": date.isoformat() if date else None,
            "link": fragments.link(source.url, finding.anchor),
        }
    return quoted


def _brief(result: RunResult, run_id: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "answer": result.answer,
        "confidence": result.confidence.level,
        "confidence_reason": result.confidence.reason,
        "findings": [
            {"claim": finding.claim, **_quoted(result, finding)} for finding in result.trusted
        ],
        "set_aside": [
            {"claim": finding.claim, "why": finding.note or finding.verdict.value}
            for finding in result.findings
            if not finding.trusted
        ],
        "warnings": list(result.warnings),
    }


def _checked(result: RunResult, run_id: int) -> dict[str, Any]:
    evidence = result.numbered

    def quotes(numbers: tuple[int, ...]) -> list[dict[str, Any]]:
        return [_quoted(result, evidence[n - 1]) for n in numbers]

    def set_aside(numbers: tuple[int, ...]) -> list[dict[str, Any]]:
        return [
            {"quote": finding.quote, "why": finding.note or finding.verdict.value}
            for finding in (evidence[n - 1] for n in numbers)
        ]

    def checked(claim: ClaimCheck) -> dict[str, Any]:
        view = {
            "claim": claim.claim,
            "in_text": claim.excerpt,
            "ruling": claim.ruling.value,
            "note": claim.note,
            "supports": quotes(claim.supports),
            "refutes": quotes(claim.refutes),
            "set_aside": set_aside(claim.set_aside),
            "unchecked": list(claim.unchecked),
        }
        if result.cited:
            pages = [page for n in claim.pages if (page := result.source(n)) is not None]
            view["label"] = cited_label(claim, result.sources)
            view["cites"] = [
                {"n": page.index, "url": page.url, "read": not page.snippet_only} for page in pages
            ]
            view["problems"] = list(claim.problems)
            view["archived"] = None if claim.archived is None else archived(claim.archived)
        return view

    def archived(claim: ClaimCheck) -> dict[str, Any]:
        copies = [page for n in claim.pages if (page := result.source(n)) is not None]
        quoted = [result.source(evidence[n - 1].source) for n in claim.supports]
        moves = {
            page.moved_from: page.url
            for page in quoted
            if page is not None and page.moved_from is not None
        }
        return {
            "label": cited_label(claim, result.sources),
            "copies": [
                {
                    "of": copy.copy_of,
                    "url": copy.url,
                    "taken": snapshot.taken_on if (snapshot := snapshot_of(copy.url)) else None,
                    "read": not copy.snippet_only,
                }
                for copy in copies
            ],
            "moved": [{"of": n, "url": url} for n, url in moves.items()],
            "supports": quotes(claim.supports),
            "refutes": quotes(claim.refutes),
            "set_aside": set_aside(claim.set_aside),
            "problems": list(claim.problems),
        }

    brief = {
        "run_id": run_id,
        "summary": result.answer,
        "confidence": result.confidence.level,
        "confidence_reason": result.confidence.reason,
        "claims": [checked(claim) for claim in result.claims],
        "warnings": list(result.warnings),
    }
    if links := dead_links(result):
        brief["dead_links"] = [link.to_dict() for link in links]
    return {**brief, "cited": True} if result.cited else brief


def _has_run(app: App, watch: Watch) -> bool:
    """The watch has a run its next run continues from: it is past its first."""
    last = app.store.last_runs(watch.name, limit=1)
    return bool(last) and continued(app, watch, last[0][1]) is not None


def _audited(outcome: WatchRun) -> dict[str, Any]:
    """A citation watch's run: only what moved, since an audit may hold hundreds of claims."""
    result = outcome.result
    cites: dict[str, list[str]] = {}
    for key, claim in zip(ruling_keys(result), result.claims, strict=True):
        cites.setdefault(key, [page.url for page in map(result.source, claim.pages) if page])
    return {
        "labels": dict(Counter(delta.fact.value for delta in outcome.rulings)),
        "claims": [
            {
                "claim": delta.fact.claim,
                "label": delta.fact.value,
                "was": delta.previous.value if delta.previous is not None else None,
                "change": delta.change.value,
                "quote": delta.fact.quote,
                "url": delta.fact.url,
                "link": delta.fact.link,
                "cites": cites.get(delta.fact.key, []),
                "gone": departed(delta, outcome.deltas),
            }
            for delta in outcome.rulings
            if delta.change in (Change.NEW, Change.CHANGED)
        ],
        "pages": [
            {
                "url": page.fact.url,
                "state": _page_state(page),
                "was": page.previous.value if page.previous is not None else None,
                "why": None if page.fact.value == READ else page.fact.value,
                "archive": _archive_state(page, outcome.copies),
                "archived": _copy_view(outcome.copies.get(page.fact.key)),
            }
            for page in outcome.pages
            if page.change is Change.CHANGED
        ],
        "left": outcome.left,
    }


def _archive_state(page: Delta, copies: Mapping[str, Copy | None]) -> str | None:
    """What the archive said of a dead page: "archived", "not archived", or "not looked up"
    (it did not answer, or more pages died than a run looks up): worth asking again later."""
    if page.fact.value == READ:
        return None
    if page.fact.key not in copies:
        return "not looked up"
    return "not archived" if copies[page.fact.key] is None else "archived"


def _copy_view(copy: Copy | None) -> dict[str, Any] | None:
    """A dead page's newest archived copy, and whether it still holds what the page said."""
    if copy is None:
        return None
    return {
        "taken": copy.snapshot.taken_on,
        "url": copy.snapshot.page,
        "link": copy.link,
        "holds": copy.holds,
        "read": copy.unread is None,
    }


def _page_state(page: Delta) -> str:
    """A cited page that moved: "dead", "back" (read before it died), or "read" at last."""
    if page.fact.value != READ:
        return "dead"
    return "back" if page.previous is not None and page.previous.missed else READ


def _watch_view(app: App, watch: Watch) -> dict[str, Any]:
    runs = app.store.recent_runs(limit=1, watch=watch.name)
    return {
        "name": watch.name,
        "goal": watch.label,
        "schedule": f"every {watch.every}" if watch.every else f"cron {watch.cron}",
        "alerts": list(rules_of(watch)),
        "check": watch.check,
        "cited": watch.cited,
        "paused": watch.paused,
        "last_run": runs[0].started_at.isoformat() if runs else None,
    }
