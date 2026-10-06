"""Reports: Markdown and HTML for people, JSON for machines, notes for Markdown vaults."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import yaml

from scout import __version__
from scout.research.results import Finding, RunResult, Source
from scout.textutil import shorten

_SLUG_JUNK = re.compile(r"[^a-z0-9]+")
_UNSAFE_IN_NAMES = re.compile(r'[<>:"/\\|?*#^\[\]\x00-\x1f]')  # Windows; Obsidian links
_MARKUP = re.compile(r"([\\\[\]<>])")
# Characters that would end a Markdown link or a table cell early.
_URL_ESCAPES = str.maketrans(
    {" ": "%20", "<": "%3C", ">": "%3E", "|": "%7C", "[": "%5B", "]": "%5D"}
)
_STYLE = """
:root { color-scheme: light dark; --muted: #6b7280; --line: #d1d5db; }
body { font: 16px/1.55 system-ui, sans-serif; max-width: 52rem; margin: 2rem auto;
       padding: 0 1rem; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.2rem; margin-top: 2rem; }
blockquote { margin: .4rem 0 .8rem; padding-left: .8rem; border-left: 3px solid var(--line); }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
td, th { border-bottom: 1px solid var(--line); padding: .35rem .5rem; text-align: left; }
.meta, footer { color: var(--muted); font-size: .9rem; }
footer { margin-top: 2.5rem; }
"""


def render_markdown(result: RunResult, *, run_id: int | None = None) -> str:
    lines = [f"# {_md(result.goal)}", ""]
    lines += [
        f"**Answer** \N{EM DASH} {result.confidence.level} confidence ({result.confidence.reason})",
        "",
        _md(result.answer),
        "",
    ]

    trusted = len(result.trusted)
    if trusted:
        lines += ["## Findings", ""]
        for number, finding in enumerate(result.numbered[:trusted], start=1):
            lines += _finding_lines(number, finding, result)
    if len(result.numbered) > trusted:
        lines += ["## Not used (could not be verified or looks wrong)", ""]
        for number, finding in enumerate(result.numbered[trusted:], start=trusted + 1):
            reason = finding.note or finding.verdict.value
            ref = _source_ref(result, finding.source)
            lines.append(f"{number}. {_md(finding.claim)} ({ref}) \N{EM DASH} {_md(reason)}")
        lines.append("")

    lines += [
        "## Sources",
        "",
        "| # | Source | Read | Date | Found by |",
        "|---|---|---|---|---|",
    ]
    for source in result.sources:
        read = (
            "snippet only (" + source.status.replace("_", " ") + ")"
            if source.snippet_only
            else "yes"
        )
        title = _md_link(source.url, _cell(shorten(source.title, 70)))
        lines.append(
            f"| {source.index} | {title} \N{EM DASH} {_cell(source.site)} "
            f"| {read} | {_dates(source)} | {_cell(shorten(source.query, 40))} |"
        )
    lines.append("")

    if result.warnings:
        lines += ["## Notes", ""] + [f"- {_md(warning)}" for warning in result.warnings] + [""]

    lines += ["---", f"_{_footer(result, run_id)}_", ""]
    return "\n".join(lines)


def render_html(result: RunResult, *, run_id: int | None = None) -> str:
    """The report as one self-contained page, to open in a browser or to share."""
    e = html.escape
    confidence = f"{result.confidence.level} confidence ({result.confidence.reason})"
    body = [
        f"<h1>{e(result.goal)}</h1>",
        f'<p class="meta"><strong>Answer</strong> &mdash; {e(confidence)}</p>',
        f"<p>{e(result.answer)}</p>",
    ]
    trusted = len(result.trusted)
    if trusted:
        body.append("<h2>Findings</h2><ol>")
        for finding in result.numbered[:trusted]:
            evidence = (
                '<p class="meta">price published by the page as structured data</p>'
                if finding.origin == "structured-data"
                else f"<blockquote>{e(finding.quote)}</blockquote>"
            )
            body.append(
                f"<li>{e(finding.claim)} {_html_ref(result, finding.source)}{evidence}</li>"
            )
        body.append("</ol>")
    if len(result.numbered) > trusted:
        body.append("<h2>Not used (could not be verified or looks wrong)</h2>")
        body.append(f'<ol start="{trusted + 1}">')
        for finding in result.numbered[trusted:]:
            reason = finding.note or finding.verdict.value
            ref = _html_ref(result, finding.source)
            body.append(f"<li>{e(finding.claim)} {ref} &mdash; {e(reason)}</li>")
        body.append("</ol>")
    body.append(
        "<h2>Sources</h2><table><tr><th>#</th><th>Source</th><th>Read</th><th>Date</th></tr>"
    )
    for source in result.sources:
        read = f"snippet only ({source.status.replace('_', ' ')})" if source.snippet_only else "yes"
        body.append(
            f"<tr><td>{source.index}</td><td>{_html_link(source.url, source.title)} "
            f"&mdash; {e(source.site)}</td><td>{e(read)}</td><td>{e(_dates(source))}</td></tr>"
        )
    body.append("</table>")
    if result.warnings:
        body.append("<h2>Notes</h2><ul>")
        body += [f"<li>{e(warning)}</li>" for warning in result.warnings]
        body.append("</ul>")
    body.append(f'<footer class="meta">{e(_footer(result, run_id))}</footer>')
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{e(result.goal)}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n"
        + "\n".join(body)
        + "\n</body>\n</html>\n"
    )


def render_note(result: RunResult, *, run_id: int | None = None) -> str:
    """The Markdown report with YAML front matter, for Obsidian and other note vaults."""
    front = {
        "goal": result.goal,
        "date": result.started_at.date().isoformat(),
        "confidence": result.confidence.level,
        "run": run_id,
        "sources": [source.url for source in result.sources if not source.snippet_only],
        "tags": ["scout"],
    }
    front_matter = yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
    return f"---\n{front_matter}---\n\n{render_markdown(result, run_id=run_id)}"


def note_name(result: RunResult, *, run_id: int | None = None) -> str:
    """A readable file name for a note, safe on every file system."""
    title = " ".join(_UNSAFE_IN_NAMES.sub(" ", result.goal).split())[:80].rstrip(" .") or "run"
    suffix = f" (run {run_id})" if run_id is not None else ""
    return f"{result.started_at:%Y-%m-%d} {title}{suffix}.md"


def render_json(result: RunResult, *, run_id: int | None = None) -> str:
    data = {"scout_version": __version__, "run_id": run_id, **result.to_dict()}
    return json.dumps(data, indent=2, ensure_ascii=False)


def save(result: RunResult, directory: Path, *, run_id: int | None = None) -> tuple[Path, Path]:
    """Write ``<date>_<time>_<slug>.md`` and ``.json``; returns both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{result.started_at:%Y-%m-%d_%H%M%S}_{slug(result.goal)}"
    markdown = directory / f"{stem}.md"
    data = directory / f"{stem}.json"
    markdown.write_text(render_markdown(result, run_id=run_id), encoding="utf-8")
    data.write_text(render_json(result, run_id=run_id), encoding="utf-8")
    return markdown, data


def slug(text: str, limit: int = 60) -> str:
    return _SLUG_JUNK.sub("-", text.lower()).strip("-")[:limit].rstrip("-") or "run"


def _finding_lines(number: int, finding: Finding, result: RunResult) -> list[str]:
    indent = " " * len(f"{number}. ")  # what keeps the lines below inside the list item
    lines = [f"{number}. {_md(finding.claim)} ({_source_ref(result, finding.source)})"]
    if finding.origin == "structured-data":
        lines.append(f"{indent}_(price published by the page as structured data)_")
    else:
        lines.append(f"{indent}> {_md(finding.quote)}")
    if finding.note:
        lines.append(f"{indent}_{_md(finding.note)}_")
    return [*lines, ""]


def _dates(source: Source) -> str:
    published = source.published.isoformat() if source.published else ""
    if source.updated and source.updated != source.published:
        return f"{published} (updated {source.updated.isoformat()})".strip()
    return published


def _source_ref(result: RunResult, index: int) -> str:
    source = result.source(index)
    return _md_link(source.url, f"source {index}") if source else f"source {index}"


def _md(text: str) -> str:
    """Web and model text, shown as written: it brings no links, images or HTML of its own."""
    return _MARKUP.sub(r"\\\1", text)


def _md_link(url: str, text: str) -> str:
    if not url.startswith(("https://", "http://")):
        return text
    url = url.translate(_URL_ESCAPES)
    return f"[{text}](<{url}>)" if "(" in url or ")" in url else f"[{text}]({url})"


def _cell(text: str) -> str:
    return _md(text).replace("|", "\\|").replace("\n", " ")


def _footer(result: RunResult, run_id: int | None) -> str:
    seconds = (result.finished_at - result.started_at).total_seconds()
    parts = [
        f"Scout {__version__}",
        f"run {run_id}" if run_id is not None else None,
        f"model {result.model}",
        f"planner: {result.plan.planner}",
        f"{result.started_at:%Y-%m-%d %H:%M} UTC",
        f"{seconds:.0f}s",
    ]
    return " \N{MIDDLE DOT} ".join(part for part in parts if part)


def _html_link(url: str, text: str) -> str:
    """A link, but only to a web page: a hostile source cannot plant a javascript: URL."""
    if not url.startswith(("https://", "http://")):
        return html.escape(text)
    return f'<a href="{html.escape(url)}">{html.escape(text)}</a>'


def _html_ref(result: RunResult, index: int) -> str:
    source = result.source(index)
    text = f"(source {index})"
    return _html_link(source.url, text) if source else text
