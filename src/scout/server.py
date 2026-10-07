"""Scout as an MCP server: research, fact-checks, recall and watches as tools for AI assistants.

Everything a tool returns is evidence-first: each finding comes with the quote that backs it and
the page it is on, and findings Scout could not verify are listed apart with the reason.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from scout import __version__
from scout.app import App
from scout.errors import AnswerPending, ConfigError, ScoutError
from scout.monitor.runner import DEFAULT_RULES, notifier_for, run_watch
from scout.monitor.trends import series
from scout.monitor.watches import Watch, WatchBook
from scout.report import render_markdown
from scout.research.factcheck import CLAIM_LIMIT, MAX_CLAIMS
from scout.research.results import RunResult

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError as exc:  # an optional extra
    raise ConfigError("the MCP server needs the mcp package: pip install 'scout[mcp]'") from exc

INSTRUCTIONS = """\
Scout researches the web with the user's own local model and reports only facts it can back \
with a verbatim quote from a page. Try `recall` first: it answers instantly from what Scout has \
already verified. `research` reads the web now and can take a few minutes. `fact_check` checks a \
text (a draft answer, an article, a web address) claim by claim against independent pages. \
Watches re-run a goal on a schedule and report what changed."""

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
    def fact_check(text: str, max_claims: int = MAX_CLAIMS) -> dict[str, Any]:
        """Check a text (a draft answer, an article, or a web address) claim by claim against
        independent pages. Each ruling rests on quotes Scout found word for word on a page;
        quotes the model offered that are not there are listed under set_aside, and numbers
        the text states that no claim covered under unchecked. Addresses on private networks
        are refused."""
        with _as_tool_errors():
            researcher = app.researcher(public_only=True, max_results=3)
            result = researcher.check(text, max_claims=max(1, min(max_claims, CLAIM_LIMIT)))
            app.learn_from(researcher)
            run_id = app.store.add_run(result)
        return _checked(result, run_id)

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
        name: str, goal: str, every: str = "1d", alerts: list[str] | None = None
    ) -> dict[str, Any]:
        """Watch a goal: re-research it on a schedule ("6h", "1d") and alert on changes.
        Alert rules: new, changed, price below 1800 USD, drop 5%, mentions "text", in stock."""
        with _as_tool_errors():
            watch = Watch(name=name, goal=goal, every=every, alerts=tuple(alerts or ()))
            book.add(watch)
        return _watch_view(app, watch)

    @server.tool(annotations=reads)
    def watch_list() -> dict[str, Any]:
        """The watches, with their schedules, alert rules and last runs."""
        with _as_tool_errors():
            return {"watches": [_watch_view(app, watch) for watch in book.load()]}

    @server.tool(annotations=browses)
    def watch_run(name: str) -> dict[str, Any]:
        """Run a watch now: what changed since its last run, and which alerts that raised."""
        with _as_tool_errors():
            watch = book.get(name)
            outcome = run_watch(
                app, watch, notifier=notifier_for(watch), book=book, public_only=True
            )
        return {
            "run_id": outcome.run_id,
            "first_run": outcome.baseline,
            "model_asked": not outcome.result.carried_over,
            "changes": {change.value: count for change, count in outcome.counts.items()},
            "alerts": [trigger.reason for trigger in outcome.alerts],
        }

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


def _page(result: RunResult, index: int) -> dict[str, Any]:
    source = result.source(index)
    if source is None:
        return {}
    date = source.freshest_date
    return {"url": source.url, "site": source.site, "date": date.isoformat() if date else None}


def _brief(result: RunResult, run_id: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "answer": result.answer,
        "confidence": result.confidence.level,
        "confidence_reason": result.confidence.reason,
        "findings": [
            {"claim": finding.claim, "quote": finding.quote, **_page(result, finding.source)}
            for finding in result.trusted
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
        return [
            {"quote": evidence[n - 1].quote, **_page(result, evidence[n - 1].source)}
            for n in numbers
        ]

    return {
        "run_id": run_id,
        "summary": result.answer,
        "confidence": result.confidence.level,
        "confidence_reason": result.confidence.reason,
        "claims": [
            {
                "claim": claim.claim,
                "in_text": claim.excerpt,
                "ruling": claim.ruling.value,
                "note": claim.note,
                "supports": quotes(claim.supports),
                "refutes": quotes(claim.refutes),
                "set_aside": [
                    {"quote": finding.quote, "why": finding.note or finding.verdict.value}
                    for finding in (evidence[n - 1] for n in claim.set_aside)
                ],
                "unchecked": list(claim.unchecked),
            }
            for claim in result.claims
        ],
        "warnings": list(result.warnings),
    }


def _watch_view(app: App, watch: Watch) -> dict[str, Any]:
    runs = app.store.recent_runs(limit=1, watch=watch.name)
    return {
        "name": watch.name,
        "goal": watch.goal,
        "schedule": f"every {watch.every}" if watch.every else f"cron {watch.cron}",
        "alerts": list(watch.alerts or DEFAULT_RULES),
        "paused": watch.paused,
        "last_run": runs[0].started_at.isoformat() if runs else None,
    }
