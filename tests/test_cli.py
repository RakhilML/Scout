import io
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from scout.app import App, accept_language
from scout.cli import EXIT_ANSWER_PENDING, main
from scout.cli._common import prepare_streams
from scout.errors import AnswerPending, SearchError
from scout.settings import load_settings
from scout.store import Store
from tests.helpers import CHECK_RESULT, NOW, FakeResearcher
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
    assert (asked, checker.check_options) == ([{"max_results": 2}], {"max_claims": 3})
    shown = invoke("factcheck", "--help").output
    assert ("-n, --pages" in shown, "-n, --claims" in shown, "--claims" in shown) == (
        True,
        False,
        True,
    )
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
