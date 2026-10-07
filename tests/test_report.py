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
from scout.research.results import ClaimCheck, RunResult
from scout.settings import load_settings
from scout.store import Store
from tests.helpers import CHECK_RESULT
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
