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
from scout.research.results import RunResult
from scout.settings import load_settings
from scout.store import Store
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
