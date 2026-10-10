import io
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner
from rich.console import Console

from scout.app import App, accept_language
from scout.cli import EXIT_ANSWER_PENDING, main
from scout.cli._common import linked, prepare_streams
from scout.errors import AnswerPending, LLMUnavailable, SearchError
from scout.research.citations import NONE_LINKED
from scout.settings import load_settings
from scout.store import Store
from tests.helpers import (
    AUDIT_RESULT,
    CHECK_RESULT,
    CITE_RESULT,
    CITED_TEXT,
    NOW,
    FakeResearcher,
    ScriptedBackend,
)
from tests.helpers import SAMPLE_RESULT as RESULT


def use_researcher(monkeypatch, outcome):
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(outcome))


def invoke(*args):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_redirected_output_is_utf8_whatever_the_code_page(monkeypatch):
    # What Windows gives a program whose output goes to a file: the ANSI code page.
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    prepare_streams()
    text = "RTX 5090 \N{EM DASH} \N{DEVANAGARI LETTER KA}"
    sys.stdout.write(text)
    sys.stdout.flush()
    assert stdout.buffer.getvalue() == text.encode("utf-8")


def test_version():
    assert "scout, version" in invoke("--version").output


def test_check_reports_the_exchange_backend(workspace):
    result = invoke("check")
    assert result.exit_code == 0
    assert "exchange" in result.output
    assert "structured mode prompt" in result.output


def test_run_saves_a_report_that_history_and_show_can_find(workspace, monkeypatch):
    use_researcher(monkeypatch, RESULT)
    result = invoke("run", "cheapest RTX 5090")
    assert result.exit_code == 0, result.output
    assert "Shop sells it for $1,999" in result.output
    reports = list((workspace / "data" / "reports").glob("*.md"))
    assert len(reports) == 1

    listed = invoke("history")
    assert "cheapest RTX 5090" in listed.output
    assert "1/3" in listed.output

    shown = invoke("show", "1", "--json")
    assert json.loads(shown.output)["goal"] == RESULT.goal


def test_ask_and_show_give_where_each_quote_is_on_its_page(workspace):
    placed = replace(RESULT.findings[0], anchor="text=Now%20%241%2C999%20at%20Shop.")
    with Store(load_settings().db_path) as store:
        run_id = store.add_run(replace(RESULT, findings=(placed, *RESULT.findings[1:])))

    (fact,) = json.loads(invoke("ask", "shop sells", "--json").output)
    assert fact["link"] == "https://shop.example/5090#:~:text=Now%20%241%2C999%20at%20Shop."
    assert "   shop.example: found " in invoke("ask", "shop sells").output
    shown = json.loads(invoke("show", str(run_id), "--json").output)
    assert [finding["anchor"] for finding in shown["findings"]] == [placed.anchor, None, None]


def test_a_terminal_link_goes_only_to_a_web_page_and_cannot_be_broken_out_of():
    url = "https://x.example/a b[c]\x1b]8;;https://evil.example\x07#:~:text=Now"
    assert linked("site", url) == (
        "[link=https://x.example/a%20b%5Bc%5D%1B%5D8;;https://evil.example%07#:~:text=Now]"
        "site[/link]"
    )
    terminal = Console(file=io.StringIO(), force_terminal=True, legacy_windows=False)
    terminal.print(linked("site", url))
    printed = terminal.file.getvalue()
    assert "\x1b]8;id=" in printed
    assert "evil.example\x07" not in printed
    assert linked("site", "javascript:alert(1)") == "site"


def test_run_json_and_no_save(workspace, monkeypatch):
    use_researcher(monkeypatch, RESULT)
    result = invoke("run", "goal", "--json", "--no-save")
    assert json.loads(result.output)["run_id"] is None
    assert not (workspace / "data" / "reports").exists()
    assert "No runs yet" in invoke("history").output

    saved = json.loads(invoke("run", "goal", "--json").output)["saved"]
    assert [Path(path).suffix for path in saved] == [".md", ".json"]


def test_pending_answer_exits_with_a_resumable_code(workspace, monkeypatch):
    use_researcher(monkeypatch, AnswerPending(Path("exchange/requests/abc.md")))
    result = invoke("run", "goal")
    assert result.exit_code == EXIT_ANSWER_PENDING
    assert "abc.md" in result.output


def test_expected_errors_are_one_line_messages(workspace, monkeypatch):
    use_researcher(monkeypatch, SearchError("no search results for: goal"))
    result = invoke("run", "goal")
    assert result.exit_code == 1
    assert "error: no search results for: goal" in result.output

    use_researcher(monkeypatch, SearchError("reply [/] unusable [type=missing]"))
    odd = invoke("run", "goal")
    assert (odd.exit_code, "reply [/] unusable [type=missing]" in odd.output) == (1, True)

    path = "C:/Users/someone/AppData/Local/Temp/" + "x" * 80 + "/locks/gpu.lock"
    use_researcher(monkeypatch, SearchError(f"watch 'gpu' is already running ({path})"))
    assert path in invoke("run", "goal").output  # never wrapped: it can be copied


def test_show_unknown_run_and_models_without_a_server(workspace):
    assert invoke("show", "42").exit_code == 2
    models = invoke("models")
    assert models.exit_code == 2
    assert "SCOUT_LLM=openai" in models.output


@pytest.mark.parametrize(
    ("region", "header"),
    [
        ("us-en", "en-US,en;q=0.9"),
        ("in-en", "en-IN,en;q=0.9"),
        ("de-de", "de-DE,de;q=0.9"),
        ("wt-wt", "en;q=0.9"),
    ],
)
def test_accept_language_follows_the_search_region(region, header):
    assert accept_language(region) == header


def test_prune_drops_old_caches(workspace):
    with Store(load_settings().db_path) as store:
        store.put_search("old", [], NOW - timedelta(days=30))
    assert "Removed 1 old cache entry." in CliRunner().invoke(main, ["prune"]).output
    assert "Removed 0 old cache entries." in CliRunner().invoke(main, ["prune"]).output


def use_checker(monkeypatch, outcome) -> FakeResearcher:
    checker = FakeResearcher(outcome)
    monkeypatch.setattr(App, "researcher", lambda self, **options: checker)
    return checker


def test_factcheck_prints_the_rulings_and_keeps_the_check(workspace, monkeypatch):
    checker = use_checker(monkeypatch, CHECK_RESULT)
    result = invoke("factcheck", "Python 3.13 was released on October 7, 2023.")
    assert result.exit_code == 0, result.output
    assert "Refuted (1 site)" in result.output
    assert "Supported (2 sites)" in result.output
    assert checker.checked == ["Python 3.13 was released on October 7, 2023."]
    reports = workspace / "data" / "reports"
    assert len(list(reports.glob("*.md"))) == 1
    (page,) = reports.glob("*.html")
    assert f"Annotated page: {page}" in result.output
    assert '<mark class="refuted"' in page.read_text(encoding="utf-8")

    assert "fact-check:" in invoke("history").output
    assert "In the text" in invoke("show", "1").output


def test_factcheck_reads_pages_and_claims_as_asked(workspace, monkeypatch):
    checker = FakeResearcher(CHECK_RESULT)
    asked = []
    monkeypatch.setattr(App, "researcher", lambda self, **options: asked.append(options) or checker)
    assert invoke("factcheck", "some text", "-n", "2", "--claims", "3").exit_code == 0
    assert (asked, checker.check_options) == (
        [{"max_results": 2, "public_only": False, "archive": False, "find_moved": False}],
        {"max_claims": 3, "cited": False, "audit": False},
    )
    shown = " ".join(invoke("factcheck", "--help").output.split())
    assert ("-n, --pages" in shown, "-n, --claims" in shown, "--claims" in shown) == (
        True,
        False,
        True,
    )
    assert "Pages to read per claim (default 3)." in shown
    assert invoke("factcheck", "some text", "-n", "11").exit_code == 2  # pages, not claims


def test_factcheck_reads_a_file_or_standard_input(workspace, monkeypatch):
    checker = use_checker(monkeypatch, CHECK_RESULT)
    text = "Python 3.13 added a JIT \N{EM DASH} really.\n"
    (workspace / "answer.txt").write_bytes(text.encode("utf-8"))
    (workspace / "bom.txt").write_bytes(text.encode("utf-8-sig"))
    (workspace / "unicode.txt").write_bytes(text.encode("utf-16"))  # PowerShell's `>`
    for name in ("answer.txt", "bom.txt", "unicode.txt"):
        assert invoke("factcheck", "-f", name, "--no-save").exit_code == 0
    piped = CliRunner().invoke(main, ["factcheck", "-f", "-", "--json"], input="From a pipe.")
    assert [claim["ruling"] for claim in json.loads(piped.output)["claims"]] == [
        "refuted",
        "supported",
    ]
    assert checker.checked == [text, text, text, "From a pipe."]


def test_factcheck_refuses_a_file_that_is_not_text(workspace, monkeypatch):
    checker = use_checker(monkeypatch, CHECK_RESULT)
    (workspace / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01\xff\xfe")
    result = invoke("factcheck", "-f", "photo.png")
    assert result.exit_code == 2
    assert "could not read photo.png as text: save it as UTF-8" in result.output
    assert checker.checked == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "not both"),
        (["some text", "-f", "answer.txt"], "not both"),
        (["  "], "nothing to check"),
    ],
)
def test_factcheck_needs_exactly_one_text(workspace, args, message):
    (workspace / "answer.txt").write_text("text", encoding="utf-8")
    result = invoke("factcheck", *args)
    assert result.exit_code == 2
    assert message in result.output


def test_factcheck_cited_says_what_each_cited_page_said_and_keeps_the_check(workspace, monkeypatch):
    checker = use_checker(monkeypatch, CITE_RESULT)
    (workspace / "answer.md").write_text(CITED_TEXT, encoding="utf-8")
    result = invoke("factcheck", "--cited", "-f", "answer.md")
    assert result.exit_code == 0, result.output
    shown = " ".join(result.output.split())
    for heading in ("Contradicted by [1] (1 site)", "Not found in [3]", "Could not read [4]"):
        assert heading in shown
    assert (checker.checked, checker.check_options) == (
        [CITED_TEXT],
        {"max_claims": 6, "cited": True, "audit": False},
    )
    reports = workspace / "data" / "reports"
    assert sorted(path.suffix for path in reports.iterdir()) == [".html", ".json", ".md"]
    (saved,) = reports.glob("*.json")
    assert json.loads(saved.read_text(encoding="utf-8"))["cited"] is True
    assert "cite-check:" in invoke("history").output


@pytest.mark.parametrize(("flags", "public_only"), [([], True), (["--allow-private"], False)])
def test_factcheck_cited_reads_public_pages_only_unless_allowed(
    workspace, monkeypatch, flags, public_only
):
    checker = FakeResearcher(CITE_RESULT)
    asked = []
    monkeypatch.setattr(App, "researcher", lambda self, **options: asked.append(options) or checker)
    assert invoke("factcheck", "--cited", *flags, CITED_TEXT).exit_code == 0
    assert asked == [
        {"max_results": 3, "public_only": public_only, "archive": False, "find_moved": False}
    ]


def test_factcheck_cited_refuses_what_does_not_apply(workspace, monkeypatch):
    checker = use_checker(monkeypatch, CITE_RESULT)
    text = "Python 3.13 is out [1](https://www.python.org/)."
    result = invoke("factcheck", "--cited", "-n", "2", text)
    assert result.exit_code == 2
    assert (
        "--pages does not apply to --cited: each claim is judged on the pages its sentence cites"
        in " ".join(result.output.split())
    )
    assert checker.checked == []


def test_factcheck_cited_checks_the_page_at_a_web_address(workspace, monkeypatch):
    checker, asked = FakeResearcher(CITE_RESULT), []
    monkeypatch.setattr(App, "researcher", lambda self, **options: asked.append(options) or checker)
    result = invoke("factcheck", "--cited", "https://example.org/answer")
    assert result.exit_code == 0, result.output
    assert (asked, checker.checked, checker.check_options) == (
        [{"max_results": 3, "public_only": True, "archive": False, "find_moved": False}],
        ["https://example.org/answer"],
        {"max_claims": 6, "cited": True, "audit": False},
    )


INTRANET = "http://10.0.0.5/page"


@pytest.mark.parametrize(
    ("flags", "requested", "message"),
    [
        ([], [], f"error: will not read {INTRANET}: it is on a private network (10.0.0.5)"),
        (["--allow-private"], [INTRANET], f"error: {NONE_LINKED}"),
    ],
)
@responses.activate
def test_factcheck_cited_reads_a_page_on_a_private_network_only_when_allowed(
    workspace, monkeypatch, flags, requested, message
):
    page = (
        "<html><body><article><p>"
        + "The runbook says to restart the queue before the cache. " * 8
        + 'See the <a href="/wiki/Cache">cache page</a>.</p></article></body></html>'
    )
    responses.add(responses.GET, INTRANET, body=page, content_type="text/html")
    backend = ScriptedBackend({})
    monkeypatch.setattr("scout.app.make_backend", lambda settings: backend)
    result = invoke("factcheck", "--cited", *flags, INTRANET)
    assert result.exit_code == 1
    assert message in " ".join(result.output.split())
    assert [call.request.url for call in responses.calls] == requested
    assert backend.requests == []


def test_factcheck_cited_needs_a_text_that_cites_a_web_page(workspace):
    result = invoke("factcheck", "--cited", "Python 3.13 was released in 2024 [1].")
    assert result.exit_code == 1
    assert (
        "error: the text cites no web page: give its sources as Markdown links, bare addresses, "
        "or a numbered list of sources whose numbers the text uses as [n]"
    ) in " ".join(result.output.split())


def test_a_stored_fact_check_can_be_rated_and_exported_but_not_replayed(workspace):
    with Store(load_settings().db_path) as store:
        run_id = store.add_run(CHECK_RESULT)
        research_id = store.add_run(RESULT)
    for args in (["replay", str(run_id)], ["eval", "export", str(run_id), "case.json"]):
        refused = invoke(*args)
        assert refused.exit_code == 1
        assert f"run {run_id} is a fact-check" in refused.output

    rated = " ".join(invoke("rate", str(run_id), "1", "bad").output.split())
    assert "Rated bad: Python 3.13.0 was released on October 7, 2024." in rated
    assert rated.endswith("Scout had trusted it: it is forgotten.")  # a check makes no cases
    rated = " ".join(invoke("rate", str(research_id), "1", "bad").output.split())
    assert "it is forgotten, and eval cases made from this run will expect it untrusted." in rated

    exported = invoke("export", "pages", "--run", str(run_id), "--format", "html")
    assert exported.exit_code == 0
    (page,) = (workspace / "pages").glob("*.html")
    assert '<mark class="refuted"' in page.read_text(encoding="utf-8")


def test_factcheck_all_audits_every_cited_claim(workspace, monkeypatch):
    checker = use_checker(monkeypatch, AUDIT_RESULT)
    (workspace / "answer.md").write_text(CITED_TEXT, encoding="utf-8")
    result = invoke("factcheck", "--cited", "--all", "-f", "answer.md")
    assert result.exit_code == 0, result.output
    assert checker.check_options == {"max_claims": 6, "cited": True, "audit": True}
    shown = " ".join(result.output.split())
    assert "Audit: 4 of 5 cited sentences checked, as 4 claims" in shown
    assert (
        'Not checked Cited sentences in which no claim was checked: \N{BULLET} "See the docs [2] '
        'for more."'
    ) in shown
    (page,) = (workspace / "data" / "reports").glob("*.html")
    assert f"Annotated page: {page}" in result.output
    assert '<span class="skipped"' in page.read_text(encoding="utf-8")
    assert "citation audit:" in invoke("history").output

    data = json.loads(invoke("factcheck", "--cited", "--all", "--json", CITED_TEXT).output)
    assert (data["audit"], data["skipped"]) == (True, ["See the docs [2] for more."])
    assert [Path(path).suffix for path in data["saved"]] == [".md", ".json", ".html"]

    for args, message in (
        (
            ["--all", "Some text."],
            "--all applies to --cited only: a plain check searches the web for every claim",
        ),
        (
            ["--cited", "--all", "--claims", "3", "-f", "answer.md"],
            "--claims does not apply to --all, which checks every cited claim (at most 300)",
        ),
    ):
        refused = invoke("factcheck", *args)
        assert refused.exit_code == 2
        assert message in " ".join(refused.output.split())
    assert (
        "--all With --cited: check every cited sentence, however long the text (up to 300 "
        "claims), part by part; one model request per claim, resumable."
    ) in " ".join(invoke("factcheck", "--help").output.split())


RESUME = (
    "The audit keeps what it read and judged for a day: run the same command to continue where "
    "it stopped."
)


def test_an_interrupted_audit_says_how_to_continue(workspace, monkeypatch):
    use_checker(monkeypatch, LLMUnavailable("the model server is not reachable"))
    failed = invoke("factcheck", "--cited", "--all", CITED_TEXT)
    assert failed.exit_code == 1
    assert f"error: the model server is not reachable {RESUME}" in " ".join(failed.output.split())

    use_checker(monkeypatch, KeyboardInterrupt())
    stopped = invoke("factcheck", "--cited", "--all", CITED_TEXT)
    assert stopped.exit_code == 1
    assert RESUME in " ".join(stopped.output.split())
    assert "Aborted!" in stopped.output

    use_checker(monkeypatch, LLMUnavailable("the model server is not reachable"))
    assert "run the same command" not in invoke("factcheck", "--cited", CITED_TEXT).output
    use_checker(monkeypatch, AnswerPending(Path("exchange/requests/abc.md")))
    waiting = invoke("factcheck", "--cited", "--all", CITED_TEXT)
    assert (waiting.exit_code, RESUME in " ".join(waiting.output.split())) == (
        EXIT_ANSWER_PENDING,
        False,
    )
