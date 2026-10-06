"""Research commands: run a goal once, inspect and replay runs, benchmark models."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from scout.app import App
from scout.cli._common import KINDS, RECENCY, err, out, settings, when
from scout.evaluate import EvalCase, case_files, load_scores, regressions, save_scores
from scout.llm.openai_compat import OpenAICompatBackend
from scout.report import (
    note_name,
    render_html,
    render_json,
    render_markdown,
    render_note,
    save,
)
from scout.research.pipeline import MAX_ROUNDS
from scout.settings import Settings
from scout.textutil import shorten
from scout.web.domains import hostname


@click.command()
@click.argument("goal")
@click.option("-n", "--max-results", type=click.IntRange(1, 20), help="Pages to read.")
@click.option("--kind", type=click.Choice(KINDS), help="Skip classifying the goal.")
@click.option("--recency", type=click.Choice(RECENCY), help="How recent sources must be.")
@click.option("--region", help="Search region, e.g. us-en, in-en, de-de.")
@click.option("--no-plan", is_flag=True, help="Search for the goal as written.")
@click.option("--deep", is_flag=True, help="Keep searching where the findings leave gaps.")
@click.option(
    "--rounds",
    type=click.IntRange(2, MAX_ROUNDS),
    default=3,
    show_default=True,
    help="With --deep: search rounds at most.",
)
@click.option("--llm", "llm_spec", help="Override SCOUT_LLM, e.g. exchange:./answers")
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
@click.option("--no-save", is_flag=True, help="Do not keep the run or write report files.")
def run(
    goal: str,
    max_results: int | None,
    kind: str | None,
    recency: str | None,
    region: str | None,
    no_plan: bool,
    deep: bool,
    rounds: int,
    llm_spec: str | None,
    as_json: bool,
    no_save: bool,
) -> None:
    """Research GOAL and print what was found."""
    overrides: dict[str, Any] = {"kind": kind, "recency": recency, "use_planner": not no_plan}
    if max_results:
        overrides["max_results"] = max_results
    if region:
        overrides["region"] = region

    with App(settings(), llm=llm_spec) as app:
        researcher = app.researcher(**overrides)
        with err.status("Researching\N{HORIZONTAL ELLIPSIS}", spinner="dots"):
            result = researcher.run_deep(goal, rounds=rounds) if deep else researcher.run(goal)
        app.learn_from(researcher)
        run_id = None if no_save else app.store.add_run(result)
        paths = None if no_save else save(result, app.settings.reports_dir, run_id=run_id)

    if as_json:
        out.print_json(render_json(result, run_id=run_id))
        return
    out.print(Markdown(render_markdown(result, run_id=run_id)))
    if paths:
        err.print(f"[dim]Saved {escape(str(paths[0]))}[/]")


@click.command()
@click.argument("question")
@click.option("-n", "--limit", default=8, show_default=True, help="How many facts to show.")
@click.option("--json", "as_json", is_flag=True, help="Print the facts as JSON.")
def ask(question: str, limit: int, as_json: bool) -> None:
    """Recall what earlier runs verified about QUESTION (no web, no model)."""
    with App(settings()) as app:
        found = app.store.recall(question, limit=limit)
    if as_json:
        out.print_json(data=[memory.to_dict() for memory in found])
        return
    if not found:
        out.print(f'Nothing found about that yet. Research it: scout run "{escape(question)}"')
        return
    for number, memory in enumerate(found, start=1):
        out.print(f"[bold]{number}.[/] {escape(memory.claim)}")
        out.print(f'   [dim]"{escape(shorten(memory.quote, 160))}"[/]')
        first, last = (when(at, date_only=True) for at in (memory.first_seen, memory.last_seen))
        seen = f"found {first} (run {memory.run_id})" + (
            f", last seen {last}" if last != first else ""
        )
        out.print(f"   {escape(hostname(memory.url))}: {seen}")


@click.command()
@click.argument("run_id", type=int)
@click.argument("number", type=int)
@click.argument("verdict", type=click.Choice(["good", "bad"]))
@click.option("--note", help="Why, in a few words.")
def rate(run_id: int, number: int, verdict: str, note: str | None) -> None:
    """Say whether finding NUMBER of a run (as its report numbers them) is right."""
    with App(settings()) as app:
        rating = app.rate(run_id, number, verdict, note=note)
    out.print(f"Rated {verdict}: {escape(rating.claim)}")
    if verdict == "bad" and rating.trusted:
        out.print(
            "  [dim]Scout had trusted it: it is forgotten, and eval cases made from this run "
            "will expect it untrusted.[/]"
        )


@click.command()
@click.option(
    "--export",
    "export_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write every rating to this JSON Lines file (a dataset).",
)
def ratings(export_path: Path | None) -> None:
    """List the findings people rated, or export them."""
    with App(settings()) as app:
        found = app.store.ratings()
    if export_path is not None:
        with export_path.open("w", encoding="utf-8") as handle:
            for rating in found:
                handle.write(json.dumps(rating.to_dict(), ensure_ascii=False) + "\n")
        out.print(f"Wrote {len(found)} rating(s) to {escape(str(export_path))}", soft_wrap=True)
        return
    if not found:
        out.print("No ratings yet. Rate a finding: scout rate RUN_ID NUMBER good|bad")
        return
    table = Table("run", "#", "verdict", "finding", "Scout trusted it", "note")
    for rating in found:
        table.add_row(
            str(rating.run_id),
            str(rating.number),
            rating.verdict,
            escape(shorten(rating.claim, 70)),
            "yes" if rating.trusted else "no",
            escape(rating.note or ""),
        )
    out.print(table)


@click.command()
@click.option(
    "--forget", "forget_site", metavar="SITE", help="Forget what Scout learned about SITE."
)
def sites(forget_site: str | None) -> None:
    """What Scout learned about the sites it read: which work, and whose quotes hold up."""
    with App(settings()) as app:
        if forget_site is not None:
            forgotten = app.store.forget_site(forget_site)
            out.print(f"Forgot {escape(forget_site)}." if forgotten else "Nothing known about it.")
            return
        records = app.store.all_sites()
    if not records:
        out.print("Nothing learned yet: every run teaches Scout about the sites it reads.")
        return
    now = datetime.now(UTC)
    table = Table("site", "read", "failed", "quotes verified", "rated wrong", "standing", "now")
    for record in sorted(records, key=lambda r: (-r.reads - r.failures, r.site)):
        verified = f"{record.verified}/{record.findings}" if record.findings else ""
        avoided = record.avoided(now)
        table.add_row(
            escape(record.site),
            str(record.reads),
            str(record.failures),
            verified,
            str(record.rated_bad or ""),
            f"{record.standing:+.2f}",
            "[yellow]skipped[/] (failing)" if avoided else "",
        )
    out.print(table)


@click.command()
def check() -> None:
    """Show the settings and test the model server."""
    current = settings()
    table = Table(show_header=False, box=None)
    for label, value in _describe(current):
        table.add_row(f"[dim]{label}[/]", escape(value))
    out.print(table)
    out.print()

    with App(current) as app:
        found = app.capabilities(refresh=True, test_structured=True)
    loaded = {True: "loaded", False: "not loaded (loads on first use)", None: "unknown"}[
        found.loaded
    ]
    out.print(f"[green]\N{CHECK MARK}[/] {escape(found.model)}: {loaded}")
    out.print(f"  context window  {found.context_tokens:,} tokens")
    out.print(f"  structured mode {found.structured_mode.value}")
    reasoning = ", ".join(found.reasoning_options) or "none reported"
    out.print(f"  reasoning       {reasoning}")
    if found.note:
        out.print(f"  [dim]{found.note}[/]")


@click.command()
def models() -> None:
    """List the models the server offers."""
    with App(settings()) as app:
        backend = app.backend
        if not isinstance(backend, OpenAICompatBackend):
            raise click.UsageError("`models` needs SCOUT_LLM=openai (a model server)")
        for model_id in backend.list_models():
            marker = "[green]\N{BLACK CIRCLE}[/]" if model_id == backend.model else " "
            out.print(f"{marker} {escape(model_id)}")


@click.command()
@click.option("-n", "--limit", default=20, show_default=True, help="How many runs to show.")
@click.option("--watch", "watch_name", help="Only the runs of this watch.")
def history(limit: int, watch_name: str | None) -> None:
    """List recent runs."""
    with App(settings()) as app:
        runs = app.store.recent_runs(limit=limit, watch=watch_name)
    if not runs:
        out.print('No runs yet. Try: scout run "your question"')
        return
    table = Table("id", "when", "watch", "goal", "confidence", "trusted")
    for summary in runs:
        table.add_row(
            str(summary.id),
            when(summary.started_at),
            summary.watch or "",
            escape(summary.goal),
            summary.confidence,
            f"{summary.verified}/{summary.findings}",
        )
    out.print(table)


@click.command()
@click.argument("run_id", type=int)
@click.option("--json", "as_json", is_flag=True, help="Print the stored result as JSON.")
def show(run_id: int, as_json: bool) -> None:
    """Show a stored run."""
    with App(settings()) as app:
        result = app.store.get_run(run_id)
    if result is None:
        raise click.BadParameter(f"no run with id {run_id}", param_hint="RUN_ID")
    if as_json:
        out.print_json(render_json(result, run_id=run_id))
    else:
        out.print(Markdown(render_markdown(result, run_id=run_id)))


@click.command()
@click.argument("folder", type=click.Path(file_okay=False, path_type=Path))
@click.option("--run", "run_ids", type=int, multiple=True, help="Only this run (repeatable).")
@click.option(
    "--format",
    "kind",
    type=click.Choice(["note", "html"]),
    default="note",
    show_default=True,
    help="note: Markdown with front matter (Obsidian and other vaults); html: a web page.",
)
@click.option("--overwrite", is_flag=True, help="Replace files that are already there.")
def export(folder: Path, run_ids: tuple[int, ...], kind: str, overwrite: bool) -> None:
    """Write stored runs into FOLDER, one file each; runs already there are skipped."""
    written = kept = 0
    with App(settings()) as app:
        for run_id in run_ids or [run.id for run in app.store.recent_runs(limit=1_000_000)]:
            result = app.store.get_run(run_id)
            if result is None:
                raise click.BadParameter(f"no run with id {run_id}", param_hint="--run")
            name = note_name(result, run_id=run_id)
            path = folder / (name if kind == "note" else f"{name.removesuffix('.md')}.html")
            if path.exists() and not overwrite:
                kept += 1
                continue
            render = render_note if kind == "note" else render_html
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(render(result, run_id=run_id), encoding="utf-8")
            written += 1
    already = f"; {kept} were already there" if kept else ""
    out.print(f"Wrote {written} file(s) to {escape(str(folder))}{already}.", soft_wrap=True)


@click.command()
@click.argument("run_id", type=int)
@click.option("--llm", "llm_spec", help="The model to analyze with, e.g. record:./answers")
@click.option("--json", "as_json", is_flag=True, help="Print the new result as JSON.")
@click.option("--no-save", is_flag=True, help="Do not keep the replay as a new run.")
def replay(run_id: int, llm_spec: str | None, as_json: bool, no_save: bool) -> None:
    """Analyze a stored run's pages again, with any model, and compare."""
    with App(settings(), llm=llm_spec) as app:
        with err.status("Replaying\N{HORIZONTAL ELLIPSIS}", spinner="dots"):
            before, after = app.replay(run_id)
        new_id = None if no_save else app.store.add_run(after, learn=False)

    if as_json:
        out.print_json(render_json(after, run_id=new_id))
        return
    table = Table("", f"run {run_id}", "replay" if new_id is None else f"replay (run {new_id})")
    table.add_row("model", escape(before.model), escape(after.model))
    table.add_row("findings", str(len(before.findings)), str(len(after.findings)))
    table.add_row("trusted", str(len(before.trusted)), str(len(after.trusted)))
    table.add_row("confidence", before.confidence.level, after.confidence.level)
    out.print(table)
    out.print(Markdown(render_markdown(after, run_id=new_id)))


@click.group(name="eval")
def eval_group() -> None:
    """Benchmark models on saved research."""


@eval_group.command(name="run")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--llm", "llm_spec", help="The model to score, e.g. openai or exchange:./answers")
@click.option("--recorded", is_flag=True, help="Replay each case's recorded model replies.")
@click.option("--strict", is_flag=True, help="Exit with status 1 on a missed fact or violation.")
@click.option(
    "--save",
    "save_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Keep the scores as a baseline.",
)
@click.option(
    "--against",
    "baseline_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Compare with a saved baseline; exit with status 1 if anything got worse.",
)
def eval_run(
    paths: tuple[Path, ...],
    llm_spec: str | None,
    recorded: bool,
    strict: bool,
    save_path: Path | None,
    baseline_path: Path | None,
) -> None:
    """Score a model on case files (or folders of them)."""
    files = case_files(paths)
    if not files:
        raise click.UsageError("no case files (*.json) found")
    baseline = load_scores(baseline_path) if baseline_path is not None else None
    table = Table("case", "model", "trusted", "expected facts", "violations", "confidence", "time")
    failed = False
    scores = []
    with App(settings(), llm=llm_spec) as app:
        for path in files:
            case = EvalCase.load(path)
            with err.status(f"Evaluating {case.name}\N{HORIZONTAL ELLIPSIS}", spinner="dots"):
                _, result = app.evaluate(case, recorded=recorded)
            facts = (
                f"{len(result.facts_found)}/{len(result.facts_found) + len(result.facts_missed)}"
            )
            table.add_row(
                escape(result.case),
                escape(result.model),
                f"{result.trusted}/{result.findings}",
                facts,
                escape(", ".join(result.violations)) or "none",
                result.confidence,
                f"{result.seconds:.0f}s",
            )
            failed = failed or bool(result.facts_missed or result.violations)
            scores.append(result)
    out.print(table)
    if save_path is not None:
        save_scores(scores, save_path)
        out.print(f"Saved the scores to {escape(str(save_path))}", soft_wrap=True)
    if baseline is not None:
        worse = regressions(baseline, scores)
        for problem in worse:
            err.print(f"[red]worse:[/] {escape(problem)}")
        if worse:
            raise click.exceptions.Exit(1)
        out.print(f"[green]Nothing got worse[/] than {escape(str(baseline_path))}")
    if strict and failed:
        raise click.exceptions.Exit(1)


@eval_group.command(name="export")
@click.argument("run_id", type=int)
@click.argument("path", type=click.Path(dir_okay=False, path_type=Path))
def eval_export(run_id: int, path: Path) -> None:
    """Save a stored run, with the exact pages it read, as a case file."""
    with App(settings()) as app:
        case = app.case_from_run(run_id, name=path.stem)
    case.save(path)
    out.print(
        f'Wrote {escape(str(path))}. List expected facts under "expect" to measure recall.',
        soft_wrap=True,
    )


def _describe(config: Settings) -> list[tuple[str, str]]:
    key = "set" if config.llm_api_key else "not set"
    ttl = f", unloaded after {config.model_ttl}s idle" if config.model_ttl else ""
    return [
        ("settings file", str(config.env_file or "none (environment only)")),
        ("model backend", config.llm),
        ("server", config.llm_base_url),
        ("model", (config.llm_model or "not set") + ttl),
        ("planning model", config.planner_model or "the same"),
        ("API key", key),
        ("search", config.search),
        ("JavaScript pages", config.render or "not rendered"),
        ("data folder", str(config.data_dir)),
        ("reports folder", str(config.reports_dir)),
    ]
