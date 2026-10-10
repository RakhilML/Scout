import json
from dataclasses import replace

import yaml
from click.testing import CliRunner

from scout.cli import main
from scout.report import (
    note_name,
    render_html,
    render_json,
    render_markdown,
    render_note,
    save,
    slug,
)
from scout.research.results import ClaimCheck, Finding, RunResult, Source, Verdict
from scout.settings import load_settings
from scout.store import Store
from tests.helpers import AUDIT_RESULT, CHECK_RESULT, CITE_RESULT, GONE
from tests.helpers import SAMPLE_RESULT as RESULT


def test_markdown_separates_trusted_findings_from_the_rest():
    text = render_markdown(RESULT, run_id=7)
    assert text.startswith("# cheapest RTX 5090 | today\n")
    assert (
        "**Answer** \N{EM DASH} medium confidence (1 of 3 findings trusted across 1 site(s))"
        in text
    )
    findings = text.split("## Findings")[1].split("## Not used")[0]
    assert "1. Shop sells it for $1,999 ([source 1](https://shop.example/5090))" in findings
    assert "   > Now $1,999 at Shop." in findings
    assert "$800" not in findings
    not_used = text.split("## Not used")[1].split("## Sources")[0]
    assert "far from the typical price of 1999 USD" in not_used
    assert "2. Shop Z sells it for $800" in not_used  # numbering goes on: `scout rate` uses it
    assert "3. It ships free" in not_used
    assert "quote not found in the source" in not_used


def test_markdown_sources_table_and_footer():
    text = render_markdown(RESULT, run_id=7)
    assert "| 1 | [RTX 5090 \\| Shop](https://shop.example/5090)" in text
    assert "2024-05-20 (updated 2026-07-27)" in text
    assert "| snippet only (blocked) |" in text
    unread = replace(RESULT.sources[1], text="")
    assert "| no (blocked) |" in render_markdown(replace(RESULT, sources=(unread,)))
    assert "## Notes\n\n- 1 of 2 pages could not be read" in text
    assert "run 7" in text
    assert "42s" in text


def test_json_round_trips_and_carries_metadata():
    data = json.loads(render_json(RESULT, run_id=7))
    assert data["run_id"] == 7
    assert "scout_version" in data
    del data["run_id"], data["scout_version"]
    assert RunResult.from_dict(data) == RESULT


def test_save_writes_both_files(tmp_path):
    markdown, data = save(RESULT, tmp_path, run_id=3)
    assert markdown.name == "2026-09-25_120000_cheapest-rtx-5090-today.md"
    assert data.with_suffix(".md") == markdown
    assert "run 3" in markdown.read_text(encoding="utf-8")
    assert json.loads(data.read_text(encoding="utf-8"))["run_id"] == 3


def test_slug():
    assert slug("What's new in Python 3.13?") == "what-s-new-in-python-3-13"
    assert slug("!!!") == "run"
    assert len(slug("word " * 50)) <= 60


def test_html_is_a_self_contained_escaped_page():
    hostile = replace(
        RESULT,
        goal="<script>alert(1)</script> RTX",
        sources=(replace(RESULT.sources[0], url="javascript:alert(1)"), *RESULT.sources[1:]),
    )
    page = render_html(hostile, run_id=7)
    assert page.startswith("<!doctype html>")
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; RTX" in page
    assert "javascript:" not in page  # a hostile link is shown as text, not linked
    assert "<blockquote>Now $1,999 at Shop.</blockquote>" in render_html(RESULT)
    assert '<a href="https://shop.example/5090">(source 1)</a>' in render_html(RESULT)


def test_notes_have_front_matter_and_safe_names():
    note = render_note(RESULT, run_id=7)
    _, front, body = note.split("---\n", 2)
    meta = yaml.safe_load(front)
    assert meta == {
        "goal": "cheapest RTX 5090 | today",
        "date": "2026-09-25",
        "confidence": "medium",
        "run": 7,
        "sources": ["https://shop.example/5090"],
        "tags": ["scout"],
    }
    assert body.lstrip().startswith("# cheapest RTX 5090")
    assert note_name(RESULT, run_id=7) == "2026-09-25 cheapest RTX 5090 today (run 7).md"
    tricky = replace(RESULT, goal=r'C:\temp/"what?" #1 [draft]')
    assert note_name(tricky) == "2026-09-25 C temp what 1 draft.md"


def test_export_writes_each_run_once(workspace):
    with Store(load_settings().db_path) as store:
        store.add_run(RESULT)
    vault = workspace / "vault"
    runner = CliRunner()
    assert "Wrote 1 file(s)" in runner.invoke(main, ["export", str(vault)]).output
    assert "1 were already there" in runner.invoke(main, ["export", str(vault)]).output
    runner.invoke(main, ["export", str(vault), "--format", "html", "--run", "1"])
    assert sorted(path.suffix for path in vault.iterdir()) == [".html", ".md"]
    assert runner.invoke(main, ["export", str(vault), "--run", "9"]).exit_code == 2


def test_markdown_shows_web_text_as_text():
    hostile = replace(
        RESULT,
        answer="See ![x](https://evil.example/t.png) <script>alert(1)</script>",
        findings=(replace(RESULT.findings[0], claim="[Click](javascript:alert(1)) for $1,999"),),
        sources=(replace(RESULT.sources[0], url="https://shop.example/a (b)"),),
    )
    text = render_markdown(hostile)
    assert r"See !\[x\](https://evil.example/t.png) \<script\>alert(1)\</script\>" in text
    assert (
        r"1. \[Click\](javascript:alert(1)) for $1,999 ([source 1](<https://shop.example/a%20(b)>))"
        in text
    )


def test_lines_under_a_finding_stay_in_its_list_item():
    shop = RESULT.findings[0]
    many = replace(RESULT, findings=tuple(replace(shop, claim=f"Fact {n}") for n in range(1, 11)))
    text = render_markdown(many)
    assert "9. Fact 9 ([source 1](https://shop.example/5090))\n   > Now $1,999" in text
    assert "10. Fact 10 ([source 1](https://shop.example/5090))\n    > Now $1,999" in text


def test_a_url_cannot_split_the_sources_table_or_plant_a_link():
    url = "https://evil.example/a|[Official site](https://phish.example/login)|"
    text = render_markdown(replace(RESULT, sources=(replace(RESULT.sources[0], url=url),)))
    assert "](https://phish.example/login)" not in text
    assert "https://evil.example/a%7C%5BOfficial%20site%5D(https://phish.example/login)%7C" in text


def test_a_fact_check_shows_each_claims_ruling_with_its_numbered_evidence():
    text = render_markdown(CHECK_RESULT, run_id=14)
    verdict = (
        "**Verdict** \N{EM DASH} medium confidence (2 of 2 claims settled, 1 by two or more sites)"
    )
    assert f"{verdict}\n\nOf 2 claims: 1 supported, 1 refuted." in text
    assert "## Findings" not in text
    assert "## Not used" not in text
    claims = text.split("## Claims")[1].split("## Sources")[0]
    assert "1. **Refuted** (1 site): Python 3.13 was released on October 7, 2023." in claims
    assert '   In the text: "Python 3.13 was released on October 7, 2023."' in claims
    release = "https://www.python.org/downloads/release/python-3130/"
    assert (
        '   - refutes [1]: "Python 3.13.0 was released on October 7, 2024." '
        f"([source 1]({release}), python.org, 2024-10-07)"
    ) in claims
    assert (
        '   - set aside [4]: "Python 3.13 was released on October 7, 2024." '
        "([source 2](https://docs.python.org/3/whatsnew/3.13.html)) "
        "\N{EM DASH} the quote does not contain 2023"
    ) in claims
    assert "   _The sources give October 7, 2024._" in claims
    assert "2. **Supported** (2 sites): Python 3.13 added an experimental JIT compiler." in claims
    assert (
        '   - confirms [3]: "Python 3.13 ships an experimental JIT compiler." '
        "([source 3](https://realpython.com/python313-new-features/), realpython.com)"
    ) in claims

    refuted, supported = CHECK_RESULT.claims
    # Sites are counted by the domain they belong to: python.org and docs.python.org are one.
    one_site = replace(CHECK_RESULT, claims=(replace(supported, supports=(1, 2)),))
    assert "1. **Supported** (1 site): " in render_markdown(one_site)

    unchecked = replace(CHECK_RESULT, claims=(replace(refuted, unchecked=("2024",)),))
    assert (
        '   In the text: "Python 3.13 was released on October 7, 2023."\n\n'
        "   Not checked: 2024, which the passage also states\n"
    ) in render_markdown(unchecked)

    unsettled = replace(CHECK_RESULT, claims=(replace(supported, supports=(), note=None),))
    text = render_markdown(unsettled)
    assert "1. **Unclear**: Python 3.13 added an experimental JIT compiler." in text
    assert "   - no quote on the pages settles it" in text

    nothing = replace(CHECK_RESULT, sources=(), findings=(), claims=(), checked_text="")
    assert "## Sources" not in render_markdown(nothing)
    assert "<h2>Sources</h2>" not in render_html(nothing)


def test_a_fact_checks_page_marks_each_claim_in_the_text_it_checked():
    page = render_html(CHECK_RESULT, run_id=14)
    assert "<h2>The text</h2>" in page
    assert (
        '<div class="checked"><a class="mark" href="#claim-1"><mark class="refuted" '
        'title="Claim 1 (refuted): Python 3.13 was released on October 7, 2023. \N{EM DASH} '
        '&quot;Python 3.13.0 was released on October 7, 2024.&quot; (python.org)">'
        "Python 3.13 was released on October 7, 2023.</mark></a>"
        '<a class="badge refuted" href="#claim-1">1</a> <a class="mark" href="#claim-2">'
        '<mark class="supported" '
    ) in page
    assert (
        '<li id="claim-2"><p><span class="ruling supported">Supported</span> (2 sites): '
        "Python 3.13 added an experimental JIT compiler.</p>"
    ) in page
    assert (
        '<p>confirms [3] (<a href="https://realpython.com/python313-new-features/">source 3</a>, '
        "realpython.com)</p><blockquote>Python 3.13 ships an experimental JIT compiler."
        "</blockquote>"
    ) in page
    assert (
        'set aside [4]: "Python 3.13 was released on October 7, 2024." '
        '<a href="https://docs.python.org/3/whatsnew/3.13.html">(source 2)</a> '
        "&mdash; the quote does not contain 2023"
    ) in page
    assert "<script" not in page

    # One sentence, two rulings: the most serious colours it, and each badge keeps its own.
    sentence = "Python 3.13 was released on October 7, 2023 and added an experimental JIT compiler."
    both = tuple(replace(claim, excerpt=sentence) for claim in CHECK_RESULT.claims)
    page = render_html(replace(CHECK_RESULT, checked_text=sentence, claims=both))
    assert page.count("<mark ") == 1
    assert '<div class="checked"><a class="mark" href="#claim-1"><mark class="refuted" ' in page
    assert (
        '</mark></a><a class="badge refuted" href="#claim-1">1</a>'
        '<a class="badge supported" href="#claim-2">2</a></div>'
    ) in page

    refuted, supported = CHECK_RESULT.claims
    unchecked = replace(CHECK_RESULT, claims=(replace(refuted, unchecked=("2024",)), supported))
    assert '<p class="meta">Not checked: 2024, which the passage also states</p>' in render_html(
        unchecked
    )

    older = render_html(replace(CHECK_RESULT, checked_text=""))  # stored before the text was
    assert "The text" not in older
    assert '<li id="claim-1">' in older


def test_a_fact_checks_page_shows_hostile_text_as_text():
    hostile = replace(
        CHECK_RESULT,
        checked_text=f"See </div></text><img src=x onerror=alert(1)> {CHECK_RESULT.checked_text}",
        sources=(replace(CHECK_RESULT.sources[0], url="javascript:alert(1)"),),
    )
    page = render_html(hostile)
    assert "<img" not in page
    assert "See &lt;/div&gt;&lt;/text&gt;&lt;img src=x onerror=alert(1)&gt; Python 3.13" in page
    assert "javascript:" not in page


def test_save_writes_the_annotated_page_of_a_fact_check(tmp_path):
    paths = save(CHECK_RESULT, tmp_path, run_id=14)
    assert [path.suffix for path in paths] == [".md", ".json", ".html"]
    assert '<mark class="refuted"' in paths[2].read_text(encoding="utf-8")
    assert [path.suffix for path in save(RESULT, tmp_path)] == [".md", ".json"]


def test_a_cite_check_heads_each_claim_with_what_the_pages_it_cites_said():
    text = render_markdown(CITE_RESULT, run_id=21)
    assert (
        "**Verdict** \N{EM DASH} medium confidence (2 of 4 cited claims settled by the pages they "
        "cite; 1 cited page could not be read)\n\n"
        "Of 4 cited claims: 2 contradicted, 1 not found, 1 unreadable."
    ) in text
    claims = text.split("## Claims")[1].split("## Sources")[0]
    assert "1. **Contradicted by [1]** (1 site): Python 3.13 was released on October 7" in claims
    # [n] is the text's citation: findings are numbered in words
    assert '   - refutes (finding 1): "Python 3.13.0 was released on October 7, 2024."' in claims
    assert "2. **Contradicted by [2]** (1 site): Python 3.13 removed the global" in claims
    assert (
        "3. **Not found in [3]**: Python 3.13's JIT makes it 40% faster than Python 3.12.\n\n"
        '   In the text: "Its JIT makes it 40% faster than 3.12 \\[3\\]."\n\n'
        "   - no quote on the page it cites states or contradicts it"
    ) in claims
    assert (
        "4. **Could not read [4]**: Python 3.13 runs on iOS as a tier 3 platform.\n\n"
        '   In the text: "It runs on iOS as a tier 3 platform \\[4\\]."\n\n'
        "   - could not read \\[4\\] example.org (not found: HTTP 404)"
    ) in claims
    sources = text.split("## Sources")[1]
    assert "| 1 | [Python Release Python 3.13.0](" in sources
    assert "| yes | 2024-10-07 | cited as \\[1\\] |" in sources
    assert f"| [{GONE}]({GONE}) \N{EM DASH} example.org | no (not found) |  | cited as" in sources

    first, second = CITE_RESULT.claims[:2]
    backed = replace(
        CITE_RESULT, claims=(replace(first, refutes=(), supports=(1, 2), pages=(1, 2)),)
    )
    assert "1. **Backed by [1, 2]** (1 site): " in render_markdown(backed)
    disputed = replace(CITE_RESULT, claims=(replace(first, supports=(2,), pages=(1, 2)),))
    assert "1. **Its sources disagree ([2] backs it, [1] contradicts it)**: " in render_markdown(
        disputed
    )
    both = replace(second, pages=(2, 3, 4), refutes=())
    assert "1. **Not found in [2, 3]**: " in render_markdown(replace(CITE_RESULT, claims=(both,)))


def test_a_cite_checks_page_speaks_of_what_the_cited_pages_said():
    page = render_html(CITE_RESULT)
    assert (
        '<p><span class="ruling supported">backed</span> '
        '<span class="ruling refuted">contradicted</span> '
        '<span class="ruling disputed">disputed</span> '
        '<span class="ruling unclear">not found, unreadable or not judged</span></p>'
    ) in page
    assert (
        '<mark class="refuted" title="Claim 1 (contradicted): Python 3.13 was released on '
        "October 7, 2023. \N{EM DASH} &quot;Python 3.13.0 was released on October 7, 2024.&quot; "
        '(python.org)">Python 3.13 was released on October 7, 2023 [1].</mark>'
    ) in page
    assert (
        '<mark class="unclear" title="Claim 4 (unreadable): Python 3.13 runs on iOS as a tier 3 '
        'platform.">It runs on iOS as a tier 3 platform [4].</mark>'
    ) in page
    assert page.count("<mark ") == 4  # "See the docs [2] for more." holds no claim
    assert (
        '<li id="claim-3"><p><span class="ruling unclear">Not found in [3]</span>: '
        "Python 3.13&#x27;s JIT"
    ) in page
    assert "<p>could not read [4] example.org (not found: HTTP 404)</p>" in page
    assert "<td>no (not found)</td>" in page


def test_a_claim_spread_over_several_pieces_gets_one_badge_and_says_why_it_is_unclear():
    sentence = "Dr. Smith said the U.S. economy grew 3% in 2023."
    unclear = ClaimCheck(
        "The U.S. economy grew 3% in 2023.", sentence, "q", problems=("not judged (no JSON)",)
    )
    result = replace(CHECK_RESULT, checked_text=sentence, claims=(unclear,), findings=())
    page = render_html(result)
    assert page.count('class="badge') == 1
    assert page.count("<mark ") == 1  # "Dr." and "U.S." end no sentence
    assert "<p>not judged (no JSON)</p>" in page
    assert "   - not judged (no JSON)" in render_markdown(result)


def test_an_audits_report_shows_what_it_did_not_check():
    text = render_markdown(AUDIT_RESULT)
    assert (
        "## Not checked\n\nCited sentences in which no claim was checked:\n\n"
        '- "See the docs \\[2\\] for more."\n'
    ) in text
    assert text.index("## Claims") < text.index("## Not checked") < text.index("## Sources")
    page = render_html(AUDIT_RESULT)
    assert (
        '<span class="skipped" title="not checked: no claim in this cited sentence was '
        'checked">See the docs [2] for more.</span>'
    ) in page
    assert page.count('<span class="skipped" title=') == 1
    assert (
        '<span class="ruling unclear">not found, unreadable or not judged</span> '
        '<span class="skipped">not checked</span></p>'
    ) in page
    assert (
        "<h2>Not checked</h2><p>Cited sentences in which no claim was checked:</p><ul>\n"
        "<li>See the docs [2] for more.</li>\n</ul>"
    ) in page

    hostile = replace(AUDIT_RESULT, skipped=("<img src=x> [2]",))
    assert r'- "\<img src=x\> \[2\]"' in render_markdown(hostile)
    assert "<li>&lt;img src=x&gt; [2]</li>" in render_html(hostile)
    for plain in (render_markdown(CITE_RESULT), render_html(CITE_RESULT)):
        assert "Not checked" not in plain
        assert 'class="skipped"' not in plain


AT_QUOTE = "text=Now%20%241%2C999%20at%20Shop."
PLACED = replace(
    RESULT, findings=(replace(RESULT.findings[0], anchor=AT_QUOTE), *RESULT.findings[1:])
)


def test_a_findings_source_link_opens_its_page_at_the_quote():
    deep = f"https://shop.example/5090#:~:{AT_QUOTE}"
    text = render_markdown(PLACED)
    assert f"1. Shop sells it for $1,999 ([source 1]({deep}))" in text
    assert "3. It ships free ([source 1](https://shop.example/5090))" in text  # not placed
    assert "| 1 | [RTX 5090 \\| Shop](https://shop.example/5090) " in text
    assert f"[source 1]({deep})" in render_note(PLACED)

    page = render_html(PLACED)
    assert f'<a href="{deep}">(source 1)</a><blockquote>' in page
    assert '<a href="https://shop.example/5090">RTX 5090 | Shop</a>' in page
    assert "opens its page at the quote, highlighted" in page
    assert "opens its page at the quote, highlighted" not in render_html(RESULT)


def test_a_fact_checks_quotes_link_to_where_they_are_on_their_pages():
    refuting, confirming, plain, aside = CHECK_RESULT.findings
    placed = replace(
        CHECK_RESULT,
        findings=(
            replace(refuting, anchor="text=Python%203.13.0%20was"),
            replace(confirming, anchor="text=Python%203.13%20adds"),
            plain,
            replace(aside, anchor="text=Python%203.13%20was"),
        ),
    )
    release = "https://www.python.org/downloads/release/python-3130/"
    whatsnew = "https://docs.python.org/3/whatsnew/3.13.html"
    text = render_markdown(placed)
    claims, sources = text.split("## Sources")
    assert (
        f"([source 1]({release}#:~:text=Python%203.13.0%20was), python.org, 2024-10-07)" in claims
    )
    assert f"([source 2]({whatsnew}#:~:text=Python%203.13%20was)) \N{EM DASH} the quote" in claims
    assert f"([source 2]({whatsnew}#:~:text=Python%203.13%20adds), docs.python.org)" in claims
    assert "([source 3](https://realpython.com/python313-new-features/), realpython.com)" in claims
    assert "#:~:" not in sources

    cards, table = render_html(placed).split("<h2>Sources</h2>")
    assert (
        f'<p>refutes [1] (<a href="{release}#:~:text=Python%203.13.0%20was">source 1</a>, '
        "python.org, 2024-10-07)</p>"
    ) in cards
    assert f'<a href="{whatsnew}#:~:text=Python%203.13%20was">(source 2)</a> &mdash;' in cards
    assert '<h2>Claims</h2><p class="meta">A source beside a quote opens its page' in cards
    assert "#:~:" not in table


def test_where_a_quote_is_round_trips_and_older_runs_load_without_it():
    data = json.loads(render_json(PLACED))
    assert [finding["anchor"] for finding in data["findings"]] == [AT_QUOTE, None, None]
    del data["run_id"], data["scout_version"]
    assert RunResult.from_dict(data) == PLACED

    for finding in data["findings"]:
        del finding["anchor"]
    older = RunResult.from_dict(data)
    assert older == RESULT
    assert render_markdown(older) == render_markdown(RESULT)


def test_an_audit_round_trips_and_older_runs_load():
    data = json.loads(render_json(AUDIT_RESULT))
    assert (data["audit"], data["skipped"]) == (True, ["See the docs [2] for more."])
    assert RunResult.from_dict(data) == AUDIT_RESULT

    older = CITE_RESULT.to_dict()
    del older["audit"], older["skipped"]
    loaded = RunResult.from_dict(older)
    assert (loaded.audit, loaded.skipped) == (False, ())
    assert render_markdown(loaded) == render_markdown(CITE_RESULT)
    assert render_html(loaded) == render_html(CITE_RESULT)


COPY = f"https://web.archive.org/web/20241102083000/{GONE}"
ON_IOS = "Python 3.13 runs on iOS as a tier 3 platform."
IOS = CITE_RESULT.claims[3]
ARCHIVED = replace(
    CITE_RESULT,
    sources=(
        *CITE_RESULT.sources,
        Source(5, COPY, "Python on phones", "example.org", "ok", "archived copy of [4]", copy_of=4),
    ),
    findings=(
        *CITE_RESULT.findings,
        Finding(ON_IOS, ON_IOS, 5, Verdict.VERIFIED, anchor="text=Python%203.13%20runs%20on%20iOS"),
    ),
    claims=(
        *CITE_RESULT.claims[:3],
        replace(IOS, archived=replace(IOS, problems=(), supports=(3,), pages=(5,))),
    ),
)


def test_a_claims_archived_reading_is_shown_beside_its_label_never_as_it():
    page = render_html(ARCHIVED)
    card = page.split('<li id="claim-4">')[1].split("</li>")[0]
    assert (
        '<p>could not read [4] example.org (not found: HTTP 404)</p><div class="archived">'
        "<p>archived copy of [4] (2024-11-02): backed</p><p>confirms (finding 3) "
        f'(<a href="{COPY}#:~:text=Python%203.13%20runs%20on%20iOS">archived copy of [4]</a>, '
        "example.org, archived 2024-11-02)</p>"
    ) in card
    assert (
        '<mark class="unclear" title="Claim 4 (unreadable): Python 3.13 runs on iOS as a tier 3 '
        'platform. \N{EM DASH} archived copy: backed">'
    ) in page
    assert '<td>5</td><td><a href="' in page
    assert "&mdash; example.org, archived copy of [4]</td><td>yes</td>" in page

    sources = render_markdown(ARCHIVED).split("## Sources")[1]
    assert "\N{EM DASH} example.org | yes |  | archived copy of \\[4\\] |" in sources
    assert render_markdown(replace(ARCHIVED, claims=CITE_RESULT.claims)).count("archived copy") == 1
