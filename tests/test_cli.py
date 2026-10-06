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
from tests.helpers import NOW, FakeResearcher
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
