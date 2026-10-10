"""Reports: Markdown and HTML for people, JSON for machines, notes for Markdown vaults."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from pathlib import Path

import yaml

from scout import __version__
from scout.research.deadlinks import (
    CONTRADICTED,
    COPY_UNREADABLE,
    MOVED,
    NOT_ARCHIVED,
    NOT_FOUND,
    REPLACE,
    DeadLink,
    dead_links,
    summary,
)
from scout.research.factcheck import (
    CITED_LABELS,
    NOT_JUDGED,
    UNREADABLE,
    annotate,
    cited_label,
    sites,
)
from scout.research.results import CHECK_KIND, ClaimCheck, Finding, Ruling, RunResult, Source
from scout.textutil import fold, shorten
from scout.web import fragments
from scout.web.archive import snapshot_of

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
.archived { padding-left: .8rem; border-left: 3px dashed var(--line); }
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
.skipped { text-decoration: underline dotted; text-underline-offset: .2em; }
.dead { overflow-wrap: anywhere; }
"""
# A sentence holding several claims takes the colour of the first of their rulings here.
_SEVERITY = (Ruling.REFUTED, Ruling.DISPUTED, Ruling.UNCLEAR, Ruling.SUPPORTED)
_SKIPPED = "Cited sentences in which no claim was checked"
_NOT_CHECKED, _NOT_READ = "not checked", "not read"
_HIGHLIGHTS = (
    "A source beside a quote opens its page at the quote, highlighted in browsers that follow "
    "text fragments; if nothing is highlighted, search the page for the quote."
)
_WHY_UNCHECKED = {
    _NOT_CHECKED: "not checked: no claim in this cited sentence was checked",
    _NOT_READ: "not read: the audit stopped at its limit before this cited sentence",
}


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
            title = _md_link(source.url, _cell(shorten(source.title, 70)))
            lines.append(
                f"| {source.index} | {title} \N{EM DASH} {_cell(source.site)} "
                f"| {_read(source)} | {_dates(source)} | {_cell(shorten(source.query, 40))} |"
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
            site = source.site
            if source.copy_of is not None:
                site += f", archived copy of [{source.copy_of}]"
            if source.moved_from is not None:
                site += f", new address of [{source.moved_from}]"
            body.append(
                f"<tr><td>{source.index}</td><td>{_html_link(source.url, source.title)} "
                f"&mdash; {e(site)}</td><td>{e(_read(source))}</td>"
                f"<td>{e(_dates(source))}</td></tr>"
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
    if note := _highlights(result):
        body.append(note)
    trusted = len(result.trusted)
    if trusted:
        body.append("<h2>Findings</h2><ol>")
        for finding in result.numbered[:trusted]:
            evidence = (
                '<p class="meta">price published by the page as structured data</p>'
                if finding.origin == "structured-data"
                else f"<blockquote>{e(finding.quote)}</blockquote>"
            )
            body.append(f"<li>{e(finding.claim)} {_html_ref(result, finding)}{evidence}</li>")
        body.append("</ol>")
    if len(result.numbered) > trusted:
        body.append("<h2>Not used (could not be verified or looks wrong)</h2>")
        body.append(f'<ol start="{trusted + 1}">')
        for finding in result.numbered[trusted:]:
            reason = finding.note or finding.verdict.value
            ref = _html_ref(result, finding)
            body.append(f"<li>{e(finding.claim)} {ref} &mdash; {e(reason)}</li>")
        body.append("</ol>")
    return body


def _check_html(result: RunResult) -> list[str]:
    """The checked text, each claim's sentence marked with its ruling and linked to the claim's
    card, then the cards with the quotes behind each ruling."""
    body = []
    if result.checked_text:
        words = {r: CITED_LABELS[r] if result.cited else r.value for r in Ruling}
        if result.cited:
            words[Ruling.UNCLEAR] += f", {UNREADABLE} or {NOT_JUDGED}"
        legend = " ".join(f'<span class="ruling {r.value}">{words[r]}</span>' for r in Ruling)
        if result.skipped:
            legend += f' <span class="skipped">{_NOT_CHECKED}</span>'
        if result.unread:
            legend += f' <span class="skipped">{_NOT_READ}</span>'
        pieces = annotate(result.checked_text, result.claims)
        last = {n: i for i, (_, held) in enumerate(pieces) for n in held}
        why = {fold(s): _NOT_READ for s in result.unread} | {
            fold(s): _NOT_CHECKED for s in result.skipped
        }
        marked = "".join(
            _marked(piece, held, tuple(n for n in held if last[n] == i), result)
            if held or fold(piece) not in why
            else _unchecked(piece, why[fold(piece)])
            for i, (piece, held) in enumerate(pieces)
        )
        body += ["<h2>The text</h2>", f"<p>{legend}</p>"]
        body.append(f'<div class="checked">{marked}</div>')
    if result.claims:
        body.append(f"<h2>Claims</h2>{_highlights(result)}<ol>")
        body += [_claim_html(n, claim, result) for n, claim in enumerate(result.claims, start=1)]
        body.append("</ol>")
    if result.skipped:
        body.append(f"<h2>Not checked</h2><p>{_SKIPPED}:</p><ul>")
        body += [f"<li>{html.escape(sentence)}</li>" for sentence in result.skipped]
        body.append("</ul>")
    if links := dead_links(result):
        e = html.escape
        body.append(f'<h2>Dead links</h2><p>{e(summary(links))}</p><ul class="dead">')
        for link in links:
            page = _html_link(link.url, link.url)
            moved = f"moved to {_html_link(new, new)}, " if (new := _new_address(link)) else ""
            copy = f"<br>{_html_link(link.link, link.link)}" if link.link is not None else ""
            body.append(
                f"<li>[{link.n}] {page} ({e(link.why)}): {moved}{e(_advice(link))}{copy}</li>"
            )
        body.append("</ul>")
    return body


def _unchecked(piece: str, why: str) -> str:
    """A cited sentence of an audited text in which no claim was checked, or that the audit
    never read: underlined, so that a reader sees what the audit did not cover."""
    e = html.escape
    core = piece.strip()
    start = piece.index(core)
    tip = e(_WHY_UNCHECKED[why])
    return (
        f'{e(piece[:start])}<span class="skipped" title="{tip}">{e(core)}</span>'
        f"{e(piece[start + len(core) :])}"
    )


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
    label = cited_label(claim, result.sources) if result.cited else claim.ruling.value
    tip = f"Claim {number} ({label}): {claim.claim}"
    if claim.archived is not None and claim.archived.pages:
        tip += f" \N{EM DASH} archived copy: {cited_label(claim.archived, result.sources)}"
    deciding = (*claim.refutes, *claim.supports)
    if not deciding:
        return tip
    finding = result.numbered[deciding[0] - 1]
    source = result.source(finding.source)
    where = f" ({source.site})" if source else ""
    return f'{tip} \N{EM DASH} "{finding.quote}"{where}'


def _claim_html(number: int, claim: ClaimCheck, result: RunResult) -> str:
    e = html.escape
    ruling = claim.ruling.value
    parts = [
        f'<li id="claim-{number}"><p><span class="ruling {ruling}">'
        f"{e(_heading(claim, result))}</span>{_site_count(claim, result)}: {e(claim.claim)}</p>",
        f'<p class="meta">In the text: "{e(claim.excerpt)}"</p>',
    ]
    if claim.caveat:
        parts.append(f"<p><strong>{e(claim.caveat.capitalize())}.</strong></p>")
    if claim.unchecked:
        parts.append(f'<p class="meta">{e(_not_checked(claim))}</p>')
    parts += _evidence_html(claim, result)
    if not (claim.supports or claim.refutes or claim.set_aside):
        parts.append(f"<p>{e(_unsettled(claim, result))}</p>")
    if (copied := claim.archived) is not None:
        inner = []
        if copied.pages:
            moves = "".join(
                f"; [{n}] moved to {_html_link(url, url)}" for n, url in _moves(copied, result)
            )
            inner.append(f"<p>{e(_archived_head(copied, result))}{moves}</p>")
            inner += _evidence_html(copied, result)
        inner += [f"<p>{e(problem)}</p>" for problem in copied.problems]
        if copied.note:
            inner.append(f"<p><em>{e(copied.note)}</em></p>")
        parts.append(f'<div class="archived">{"".join(inner)}</div>')
    if claim.note:
        parts.append(f"<p><em>{e(claim.note)}</em></p>")
    return "".join(parts) + "</li>"


def _evidence_html(claim: ClaimCheck, result: RunResult) -> list[str]:
    """The quotes that decided a claim, then those set aside."""
    e = html.escape
    evidence = result.numbered
    parts = []
    for label, numbers in (("confirms", claim.supports), ("refutes", claim.refutes)):
        for n in numbers:
            finding = evidence[n - 1]
            where = _html_evidence_ref(result, finding)
            parts.append(f"<p>{label} {_finding(n, result)} ({where})</p>")
            parts.append(f"<blockquote>{e(finding.quote)}</blockquote>")
    for n in claim.set_aside:
        finding = evidence[n - 1]
        reason = finding.note or finding.verdict.value
        parts.append(
            f'<p class="meta">set aside {_finding(n, result)}: "{e(finding.quote)}" '
            f"{_html_ref(result, finding)} &mdash; {e(reason)}</p>"
        )
    return parts


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
    if links := dead_links(result):
        data["dead_links"] = [link.to_dict() for link in links]
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
            ref = _source_ref(result, finding)
            lines.append(f"{number}. {_md(finding.claim)} ({ref}) \N{EM DASH} {_md(reason)}")
        lines.append("")
    return lines


def _claim_lines(result: RunResult) -> list[str]:
    """A fact-check: each claim's ruling with the evidence numbered as `scout rate` counts it."""
    lines = _headline("Verdict", result)
    if result.claims:
        lines += ["## Claims", ""]
    for number, claim in enumerate(result.claims, start=1):
        indent = " " * len(f"{number}. ")
        ruling = f"**{_heading(claim, result)}**{_site_count(claim, result)}"
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
        lines += _evidence_lines(claim, result, indent)
        if not (claim.supports or claim.refutes or claim.set_aside):
            lines.append(f"{indent}- {_md(_unsettled(claim, result))}")
        if (copied := claim.archived) is not None:
            inner = indent
            if copied.pages:
                moves = "".join(
                    f"; [{n}] moved to {_autolink(url)}" for n, url in _moves(copied, result)
                )
                lines.append(f"{indent}- {_archived_head(copied, result)}{moves}")
                inner += "  "
                lines += _evidence_lines(copied, result, inner)
            lines += [f"{inner}- {_md(problem)}" for problem in copied.problems]
            if copied.note:
                lines.append(f"{inner}- _{_inline(copied.note)}_")
        if claim.note:
            lines += ["", f"{indent}_{_inline(claim.note)}_"]
        lines.append("")
    if result.skipped:
        lines += ["## Not checked", "", f"{_SKIPPED}:", ""]
        lines += [f'- "{_inline(sentence)}"' for sentence in result.skipped]
        lines.append("")
    if links := dead_links(result):
        lines += ["## Dead links", "", summary(links), ""]
        for link in links:
            why, advice = _inline(link.why), _inline(_advice(link))
            moved = f"moved to {_autolink(new)}, " if (new := _new_address(link)) else ""
            lines.append(f"- [{link.n}] {_autolink(link.url)} ({why}): {moved}{advice}")
            if link.link is not None:
                lines.append(f"  {_autolink(link.link)}")
        lines.append("")
    return lines


def _evidence_lines(claim: ClaimCheck, result: RunResult, indent: str) -> list[str]:
    """The quotes that decided a claim, then those set aside, as items of its list."""
    evidence = result.numbered
    lines = []
    for label, numbers in (("confirms", claim.supports), ("refutes", claim.refutes)):
        for n in numbers:
            finding = evidence[n - 1]
            where = _evidence_ref(result, finding)
            lines.append(
                f'{indent}- {label} {_finding(n, result)}: "{_inline(finding.quote)}" ({where})'
            )
    for n in claim.set_aside:
        finding = evidence[n - 1]
        where = _source_ref(result, finding)
        reason = finding.note or finding.verdict.value
        lines.append(
            f'{indent}- set aside {_finding(n, result)}: "{_inline(finding.quote)}" ({where}) '
            f"\N{EM DASH} {_md(reason)}"
        )
    return lines


def _archived_head(copied: ClaimCheck, result: RunResult) -> str:
    """What a claim's archived copies said: "archived copy of [4] (2024-11-02): backed"."""
    copies = [source for n in copied.pages if (source := result.source(n)) is not None]
    what = "archived copy" if len(copies) == 1 else "archived copies"
    of = ", ".join(f"[{source.copy_of}]" for source in copies)
    taken = ", ".join(_archived_on(source) for source in copies)
    return f"{what} of {of} ({taken}): {cited_label(copied, result.sources)}"


def _archived_on(source: Source) -> str:
    copy = snapshot_of(source.url)
    return copy.taken_on if copy is not None else "date unknown"


def _moves(copied: ClaimCheck, result: RunResult) -> list[tuple[int, str]]:
    """Where the dead pages moved whose new address holds a quote of a claim's archived
    reading, with their citation numbers."""
    pages = (result.source(result.numbered[n - 1].source) for n in copied.supports)
    moves = (
        (page.moved_from, page.url)
        for page in pages
        if page is not None and page.moved_from is not None
    )
    return list(dict.fromkeys(moves))


def _new_address(link: DeadLink) -> str | None:
    """Where a dead cited page moved, when it is to be cited there."""
    return link.moved.url if link.state == MOVED and link.moved is not None else None


def _advice(link: DeadLink) -> str:
    """What to do about a dead cited page, from what its archived copy, and the page it moved
    to, said. The renderers put where it moved before it."""
    copy = f"its archived copy of {_archived_on(link.copy)}" if link.copy is not None else ""
    quote = f'"{link.quote.quote}"' if link.quote is not None else ""
    if link.state in (MOVED, REPLACE):
        if link.state == MOVED:
            stop = "" if quote.endswith(('."', '!"', '?"')) else "."
            advice = (
                f"which still states {_numbered(link.backs)} word for word, as {copy} did: "
                f"{quote}{stop} Cite it instead."
            )
        else:
            advice = f"replace with {copy}, which backs {_numbered(link.backs)}: {quote}"
        if link.contradicts:
            those = "that sentence" if len(link.contradicts) == 1 else "those sentences"
            which = "Its archived copy" if link.state == MOVED else "It"
            advice += f" {which} contradicts {_numbered(link.contradicts)}: correct {those}."
        silent = tuple(n for n in link.claims if n not in (*link.backs, *link.contradicts))
        if silent:  # it is cited for these too, but does not say them
            those = "that sentence" if len(silent) == 1 else "those sentences"
            exactly = " word for word" if link.state == MOVED else ""
            advice += (
                f" It does not state {_numbered(silent)}{exactly}: check {those} or cite another "
                "source."
            )
        return advice
    if link.state == CONTRADICTED:
        return (
            f"{copy} contradicts {_numbered(link.contradicts)}: {quote}; correct the sentence or "
            "cite another source"
        )
    if link.state == NOT_FOUND:
        return f"{copy} does not state {_numbered(link.claims)}; cite another source"
    if link.state == COPY_UNREADABLE:
        return f"{copy} could not be read ({link.note})"
    if link.state == NOT_ARCHIVED:
        return f"not archived; cite another source ({_numbered(link.claims)})"
    if link.state == NOT_JUDGED:
        return f"{copy} could not be judged (the model's reply was unusable)"
    return f"not looked up ({link.note})" if link.note else "not looked up"


def _numbered(claims: tuple[int, ...]) -> str:
    """ "claim 1", "claims 1 and 2", "claims 1, 2 and 4"."""
    if len(claims) == 1:
        return f"claim {claims[0]}"
    return f"claims {', '.join(map(str, claims[:-1]))} and {claims[-1]}"


def _autolink(url: str) -> str:
    """A web address as a Markdown link to itself, whole: a bare one would lose a final "." (a
    text fragment's), and its "_" or "*" would be read as emphasis."""
    return f"<{url.translate(_URL_ESCAPES)}>"


def _heading(claim: ClaimCheck, result: RunResult) -> str:
    """A claim's ruling as its card heads it. A cite-check's says what the pages it cites
    said, by the numbers the text cites them with: "Contradicted by [2]"."""
    if not result.cited:
        return claim.ruling.value.capitalize()
    label = cited_label(claim, result.sources)
    backs, contradicts = (
        sorted({result.numbered[n - 1].source for n in numbers})
        for numbers in (claim.supports, claim.refutes)
    )
    if label == UNREADABLE:
        return f"Could not read {_cites(claim.pages)}"
    if label == NOT_JUDGED:
        return f"Not judged on {_cites(claim.pages)}"
    if claim.ruling is Ruling.UNCLEAR:
        read = [n for n in claim.pages if (page := result.source(n)) and not page.snippet_only]
        return f"Not found in {_cites(read)}"
    if claim.ruling is Ruling.DISPUTED:
        back, contradict = ("s" if len(found) == 1 else "" for found in (backs, contradicts))
        return (
            f"Its sources disagree ({_cites(backs)} back{back} it, "
            f"{_cites(contradicts)} contradict{contradict} it)"
        )
    return f"{label.capitalize()} by {_cites(backs or contradicts)}"


def _cites(numbers: Iterable[int]) -> str:
    return f"[{', '.join(map(str, numbers))}]"


def _finding(number: int, result: RunResult) -> str:
    """A finding's number, which a cite-check gives in words: its [n] are the text's citations."""
    return f"(finding {number})" if result.cited else f"[{number}]"


def _unsettled(claim: ClaimCheck, result: RunResult) -> str:
    """Why a claim has no evidence at all: what went wrong for it, when something did."""
    if claim.problems:
        return "; ".join(claim.problems)
    if result.cited:
        pages = "page it cites" if len(claim.pages) == 1 else "pages it cites"
        return f"no quote on the {pages} states or contradicts it"
    return "no quote on the pages settles it"


def _finding_lines(number: int, finding: Finding, result: RunResult) -> list[str]:
    indent = " " * len(f"{number}. ")  # what keeps the lines below inside the list item
    lines = [f"{number}. {_md(finding.claim)} ({_source_ref(result, finding)})"]
    if finding.origin == "structured-data":
        lines.append(f"{indent}_(price published by the page as structured data)_")
    else:
        lines.append(f"{indent}> {_md(finding.quote)}")
    if finding.note:
        lines.append(f"{indent}_{_md(finding.note)}_")
    return [*lines, ""]


def _read(source: Source) -> str:
    """Whether a page was read: a page that could not be read may have left its search snippet."""
    if not source.snippet_only:
        return "yes"
    status = source.status.replace("_", " ")
    return f"snippet only ({status})" if source.text else f"no ({status})"


def _dates(source: Source) -> str:
    published = source.published.isoformat() if source.published else ""
    if source.updated and source.updated != source.published:
        return f"{published} (updated {source.updated.isoformat()})".strip()
    return published


def _source_ref(result: RunResult, finding: Finding) -> str:
    """The link to a finding's page, which opens it at the quote."""
    source = result.source(finding.source)
    text = _source_name(source, finding.source)
    return _md_link(fragments.link(source.url, finding.anchor), text) if source else text


def _source_name(source: Source | None, index: int) -> str:
    """How a quote names its page: "source 3", "archived copy of [4]", or "[4] at its new
    address"."""
    if source is not None and source.copy_of is not None:
        return f"archived copy of [{source.copy_of}]"
    if source is not None and source.moved_from is not None:
        return f"[{source.moved_from}] at its new address"
    return f"source {index}"


def _source_date(source: Source) -> str | None:
    """The page's date, or the day an archived copy was taken."""
    if source.copy_of is not None:
        return f"archived {_archived_on(source)}"
    date = source.freshest_date
    return date.isoformat() if date else None


def _evidence_ref(result: RunResult, finding: Finding) -> str:
    """The source link, with the site and the page's date."""
    source = result.source(finding.source)
    if source is None:
        return f"source {finding.source}"
    parts = [_source_ref(result, finding), _md(source.site), _source_date(source)]
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


def _html_ref(result: RunResult, finding: Finding) -> str:
    source = result.source(finding.source)
    text = f"({_source_name(source, finding.source)})"
    return _html_link(fragments.link(source.url, finding.anchor), text) if source else text


def _html_evidence_ref(result: RunResult, finding: Finding) -> str:
    """The source link, with the site and the page's date."""
    source = result.source(finding.source)
    if source is None:
        return f"source {finding.source}"
    name = _source_name(source, finding.source)
    link = _html_link(fragments.link(source.url, finding.anchor), name)
    parts = [link, html.escape(source.site), _source_date(source)]
    return ", ".join(part for part in parts if part)


def _highlights(result: RunResult) -> str:
    """What a reader may not expect a source link to do, said where it first does it."""
    if not any(finding.anchor for finding in result.findings):
        return ""
    return f'<p class="meta">{_HIGHLIGHTS}</p>'
