"""Reports: Markdown and HTML for people, JSON for machines, notes for Markdown vaults."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import yaml

from scout import __version__
from scout.research.factcheck import annotate, sites
from scout.research.results import CHECK_KIND, ClaimCheck, Finding, Ruling, RunResult, Source
from scout.textutil import shorten

_SLUG_JUNK = re.compile(r"[^a-z0-9]+")
_UNSAFE_IN_NAMES = re.compile(r'[<>:"/\\|?*#^\[\]\x00-\x1f]')  # Windows; Obsidian links
_MARKUP = re.compile(r"([\\\[\]<>*_`])")
# Characters that would end a Markdown link or a table cell early.
_URL_ESCAPES = str.maketrans(
    {" ": "%20", "<": "%3C", ">": "%3E", "|": "%7C", "[": "%5B", "]": "%5D"}
)
_STYLE = """
:root { color-scheme: light dark; --muted: #6b7280; --line: #d1d5db; --supported: #dcfce7;
        --refuted: #fee2e2; --disputed: #fef3c7; --unclear: #e5e7eb; }
@media (prefers-color-scheme: dark) {
  :root { --supported: #14532d; --refuted: #7f1d1d; --disputed: #78350f; --unclear: #374151; }
}
body { font: 16px/1.55 system-ui, sans-serif; max-width: 52rem; margin: 2rem auto;
       padding: 0 1rem; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.2rem; margin-top: 2rem; }
blockquote { margin: .4rem 0 .8rem; padding-left: .8rem; border-left: 3px solid var(--line); }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
td, th { border-bottom: 1px solid var(--line); padding: .35rem .5rem; text-align: left; }
.meta, footer { color: var(--muted); font-size: .9rem; }
footer { margin-top: 2.5rem; }
.checked { white-space: pre-wrap; }
mark, .ruling { color: inherit; border-radius: .25rem; padding: 0 .15rem; }
a.mark { color: inherit; text-decoration: none; }
.badge { font-size: .75rem; vertical-align: super; text-decoration: none; color: inherit;
         border-radius: .25rem; padding: 0 .2rem; margin-left: .1rem; }
.supported { background: var(--supported); } .refuted { background: var(--refuted); }
.disputed { background: var(--disputed); } .unclear { background: var(--unclear); }
"""
# A sentence holding several claims takes the colour of the first of their rulings here.
_SEVERITY = (Ruling.REFUTED, Ruling.DISPUTED, Ruling.UNCLEAR, Ruling.SUPPORTED)


def render_markdown(result: RunResult, *, run_id: int | None = None) -> str:
    lines = [f"# {_md(result.goal)}", ""]
    if result.plan.kind == CHECK_KIND:
        lines += _claim_lines(result)
    else:
        lines += _finding_sections(result)

    if result.sources:
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
    """The report as one self-contained page, to open in a browser or to share. A fact-check's
    page shows the checked text with each claim's sentence coloured by its ruling."""
    e = html.escape
    check = result.plan.kind == CHECK_KIND
    confidence = f"{result.confidence.level} confidence ({result.confidence.reason})"
    body = [
        f"<h1>{e(result.goal)}</h1>",
        f'<p class="meta"><strong>{"Verdict" if check else "Answer"}</strong> &mdash; '
        f"{e(confidence)}</p>",
        f"<p>{e(result.answer)}</p>",
        *(_check_html(result) if check else _findings_html(result)),
    ]
    if result.sources:
        body.append(
            "<h2>Sources</h2><table><tr><th>#</th><th>Source</th><th>Read</th><th>Date</th></tr>"
        )
        for source in result.sources:
            read = (
                f"snippet only ({source.status.replace('_', ' ')})"
                if source.snippet_only
                else "yes"
            )
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


def _findings_html(result: RunResult) -> list[str]:
    e = html.escape
    body = []
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
    return body


def _check_html(result: RunResult) -> list[str]:
    """The checked text, each claim's sentence marked with its ruling and linked to the claim's
    card, then the cards with the quotes behind each ruling."""
    body = []
    if result.checked_text:
        legend = " ".join(f'<span class="ruling {r.value}">{r.value}</span>' for r in Ruling)
        pieces = annotate(result.checked_text, result.claims)
        last = {n: i for i, (_, held) in enumerate(pieces) for n in held}
        marked = "".join(
            _marked(piece, held, tuple(n for n in held if last[n] == i), result)
            for i, (piece, held) in enumerate(pieces)
        )
        body += ["<h2>The text</h2>", f"<p>{legend}</p>"]
        body.append(f'<div class="checked">{marked}</div>')
    if result.claims:
        body.append("<h2>Claims</h2><ol>")
        body += [_claim_html(n, claim, result) for n, claim in enumerate(result.claims, start=1)]
        body.append("</ol>")
    return body


def _marked(piece: str, held: tuple[int, ...], ending: tuple[int, ...], result: RunResult) -> str:
    """A sentence of the checked text in the colour of the most serious ruling among the claims
    it holds (so green means every one held up), linked to the card of the claim that coloured
    it, and a badge for each claim whose passage ends here. Hovering shows the claims and the
    quotes that decided them."""
    e = html.escape
    if not held:
        return e(piece)
    core = piece.strip()
    start = piece.index(core)
    claims = [(n, result.claims[n - 1]) for n in held]
    worst = min((claim.ruling for _, claim in claims), key=_SEVERITY.index)
    decider = next(n for n, claim in claims if claim.ruling is worst)
    tip = "\n".join(_tip(n, claim, result) for n, claim in claims)
    badges = "".join(
        f'<a class="badge {claim.ruling.value}" href="#claim-{n}">{n}</a>'
        for n, claim in claims
        if n in ending
    )
    return (
        f'{e(piece[:start])}<a class="mark" href="#claim-{decider}">'
        f'<mark class="{worst.value}" title="{e(tip)}">{e(core)}</mark></a>'
        f"{badges}{e(piece[start + len(core) :])}"
    )


def _tip(number: int, claim: ClaimCheck, result: RunResult) -> str:
    tip = f"Claim {number} ({claim.ruling.value}): {claim.claim}"
    deciding = (*claim.refutes, *claim.supports)
    if not deciding:
        return tip
    finding = result.numbered[deciding[0] - 1]
    source = result.source(finding.source)
    where = f" ({source.site})" if source else ""
    return f'{tip} \N{EM DASH} "{finding.quote}"{where}'


def _claim_html(number: int, claim: ClaimCheck, result: RunResult) -> str:
    e = html.escape
    evidence = result.numbered
    ruling = claim.ruling.value
    parts = [
        f'<li id="claim-{number}"><p><span class="ruling {ruling}">{ruling.capitalize()}</span>'
        f"{_site_count(claim, result)}: {e(claim.claim)}</p>",
        f'<p class="meta">In the text: "{e(claim.excerpt)}"</p>',
    ]
    if claim.caveat:
        parts.append(f"<p><strong>{e(claim.caveat.capitalize())}.</strong></p>")
    if claim.unchecked:
        parts.append(f'<p class="meta">{e(_not_checked(claim))}</p>')
    for label, numbers in (("confirms", claim.supports), ("refutes", claim.refutes)):
        for n in numbers:
            finding = evidence[n - 1]
            where = _html_evidence_ref(result, finding.source)
            parts.append(f"<p>{label} [{n}] ({where})</p>")
            parts.append(f"<blockquote>{e(finding.quote)}</blockquote>")
    for n in claim.set_aside:
        finding = evidence[n - 1]
        reason = finding.note or finding.verdict.value
        parts.append(
            f'<p class="meta">set aside [{n}]: "{e(finding.quote)}" '
            f"{_html_ref(result, finding.source)} &mdash; {e(reason)}</p>"
        )
    if not (claim.supports or claim.refutes or claim.set_aside):
        parts.append(f"<p>{e(_unsettled(claim))}</p>")
    if claim.note:
        parts.append(f"<p><em>{e(claim.note)}</em></p>")
    return "".join(parts) + "</li>"


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


def save(result: RunResult, directory: Path, *, run_id: int | None = None) -> list[Path]:
    """Write ``<date>_<time>_<slug>.md`` and ``.json`` and, for a fact-check, the annotated
    page as ``.html``; returns their paths in that order."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{result.started_at:%Y-%m-%d_%H%M%S}_{slug(result.goal)}"
    renders = {".md": render_markdown, ".json": render_json}
    if result.plan.kind == CHECK_KIND:
        renders[".html"] = render_html
    paths = []
    for suffix, render in renders.items():
        path = directory / f"{stem}{suffix}"
        path.write_text(render(result, run_id=run_id), encoding="utf-8")
        paths.append(path)
    return paths


def slug(text: str, limit: int = 60) -> str:
    return _SLUG_JUNK.sub("-", text.lower()).strip("-")[:limit].rstrip("-") or "run"


def _headline(label: str, result: RunResult) -> list[str]:
    confidence = result.confidence
    return [
        f"**{label}** \N{EM DASH} {confidence.level} confidence ({confidence.reason})",
        "",
        _md(result.answer),
        "",
    ]


def _finding_sections(result: RunResult) -> list[str]:
    lines = _headline("Answer", result)
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
    return lines


def _claim_lines(result: RunResult) -> list[str]:
    """A fact-check: each claim's ruling with the evidence numbered as `scout rate` counts it."""
    lines = _headline("Verdict", result)
    if result.claims:
        lines += ["## Claims", ""]
    evidence = result.numbered
    for number, claim in enumerate(result.claims, start=1):
        indent = " " * len(f"{number}. ")
        ruling = f"**{claim.ruling.value.capitalize()}**{_site_count(claim, result)}"
        lines += [
            f"{number}. {ruling}: {_inline(claim.claim)}",
            "",
            f'{indent}In the text: "{_inline(claim.excerpt)}"',
            "",
        ]
        if claim.caveat:
            lines += [f"{indent}**{_md(claim.caveat.capitalize())}.**", ""]
        if claim.unchecked:
            lines += [f"{indent}{_not_checked(claim)}", ""]
        for label, numbers in (("confirms", claim.supports), ("refutes", claim.refutes)):
            for n in numbers:
                finding = evidence[n - 1]
                where = _evidence_ref(result, finding.source)
                lines.append(f'{indent}- {label} [{n}]: "{_inline(finding.quote)}" ({where})')
        for n in claim.set_aside:
            finding = evidence[n - 1]
            where = _source_ref(result, finding.source)
            reason = finding.note or finding.verdict.value
            lines.append(
                f'{indent}- set aside [{n}]: "{_inline(finding.quote)}" ({where}) '
                f"\N{EM DASH} {_md(reason)}"
            )
        if not (claim.supports or claim.refutes or claim.set_aside):
            lines.append(f"{indent}- {_md(_unsettled(claim))}")
        if claim.note:
            lines += ["", f"{indent}_{_inline(claim.note)}_"]
        lines.append("")
    return lines


def _unsettled(claim: ClaimCheck) -> str:
    """Why a claim has no evidence at all: what went wrong for it, when something did."""
    if claim.problems:
        return "; ".join(claim.problems)
    return "no quote on the pages settles it"


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


def _evidence_ref(result: RunResult, index: int) -> str:
    """The source link, with the site and the page's date."""
    source = result.source(index)
    if source is None:
        return f"source {index}"
    date = source.freshest_date
    parts = [_source_ref(result, index), _md(source.site), date.isoformat() if date else None]
    return ", ".join(part for part in parts if part)


def _site_count(claim: ClaimCheck, result: RunResult) -> str:
    """How many sites settle a supported or refuted claim, as " (2 sites)"."""
    if claim.ruling not in (Ruling.SUPPORTED, Ruling.REFUTED):
        return ""
    count = len(sites((*claim.supports, *claim.refutes), result.numbered, result.sources))
    return f" ({count} site{'s' if count > 1 else ''})"


def _not_checked(claim: ClaimCheck) -> str:
    return f"Not checked: {', '.join(claim.unchecked)}, which the passage also states"


def _md(text: str) -> str:
    """Web and model text, shown as written: it brings no links, images or HTML of its own."""
    return _MARKUP.sub(r"\\\1", text)


def _inline(text: str) -> str:
    """Web or model text on one line, so that it stays inside its list item."""
    return _md(" ".join(text.split()))


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


def _html_evidence_ref(result: RunResult, index: int) -> str:
    """The source link, with the site and the page's date."""
    source = result.source(index)
    if source is None:
        return f"source {index}"
    date = source.freshest_date
    link = _html_link(source.url, f"source {index}")
    parts = [link, html.escape(source.site), date.isoformat() if date else None]
    return ", ".join(part for part in parts if part)
