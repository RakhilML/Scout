"""Scout's MCP tools, called the way an assistant calls them: through an MCP client session."""

import asyncio
from pathlib import Path

import pytest

from scout.app import App
from scout.errors import AnswerPending, SearchError
from scout.settings import Settings
from tests.helpers import CHECK_RESULT, SAMPLE_RESULT, FakeFetcher, FakeResearcher

mcp = pytest.importorskip("mcp")
from scout.server import build  # noqa: E402  (needs the optional mcp package)


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=tmp_path, reports_dir=tmp_path / "reports", llm="exchange:x")
    with App(settings) as app:
        yield app


def call(app, *steps):
    """Run (tool, arguments) calls in one client session; return their results."""

    async def session():
        async with mcp.Client(build(app)) as client:
            return [await client.call_tool(tool, arguments) for tool, arguments in steps]

    return asyncio.run(session())


def test_research_returns_evidence_and_remembers_it(app, monkeypatch):
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(SAMPLE_RESULT))
    researched, recalled = call(
        app, ("research", {"goal": "cheapest RTX 5090"}), ("recall", {"question": "rtx shop"})
    )
    brief = researched.structured_content
    assert brief["confidence"] == "medium"
    (finding,) = brief["findings"]
    assert finding == {
        "claim": "Shop sells it for $1,999",
        "quote": "Now $1,999 at Shop.",
        "url": "https://shop.example/5090",
        "site": "shop.example",
        "date": "2026-07-27",
    }
    assert [item["claim"] for item in brief["set_aside"]] == [
        "Shop Z sells it for $800",
        "It ships free",
    ]
    (fact,) = recalled.structured_content["facts"]
    assert fact["run_id"] == brief["run_id"]


def test_failures_come_back_as_messages_the_model_can_act_on(app, monkeypatch):
    failure = SearchError("no search results for: rtx")
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(failure))
    researched, reported, watched = call(
        app,
        ("research", {"goal": "rtx"}),
        ("report", {"run_id": 42}),
        ("watch_add", {"name": "gpu", "goal": "rtx", "alerts": ["when it is cheap"]}),
    )
    assert researched.is_error
    assert "no search results for: rtx" in researched.content[0].text
    assert "no run with id 42" in reported.content[0].text
    assert "cannot understand the alert rule" in watched.content[0].text


def test_watches_can_be_added_listed_and_inspected(app):
    run_id = app.store.add_run(SAMPLE_RESULT)
    added, listed, changes, reported = call(
        app,
        ("watch_add", {"name": "gpu", "goal": "cheapest RTX 5090", "alerts": ["below 1800"]}),
        ("watch_list", {}),
        ("watch_changes", {"name": "gpu"}),
        ("report", {"run_id": run_id}),
    )
    assert added.structured_content["schedule"] == "every 1d"
    (watch,) = listed.structured_content["watches"]
    assert (watch["name"], watch["alerts"], watch["last_run"]) == ("gpu", ["below 1800"], None)
    assert changes.structured_content == {"alerts": []}
    assert reported.content[0].text.startswith("# cheapest RTX 5090 | today")


def test_read_only_tools_say_so(app):
    async def tools():
        async with mcp.Client(build(app)) as client:
            return (await client.list_tools()).tools

    read_only = {tool.name for tool in asyncio.run(tools()) if tool.annotations.read_only_hint}
    assert read_only == {"recall", "report", "watch_list", "watch_changes", "watch_trend"}


def test_fact_check_returns_each_ruling_with_its_evidence_and_remembers_it(app, monkeypatch):
    checker, asked = FakeResearcher(CHECK_RESULT), []
    monkeypatch.setattr(App, "researcher", lambda self, **options: asked.append(options) or checker)
    checked, recalled = call(
        app,
        ("fact_check", {"text": "Python 3.13 was released on October 7, 2023."}),
        ("recall", {"question": "python 3.13 jit"}),
    )
    brief = checked.structured_content
    assert (brief["summary"], brief["confidence"]) == (
        "Of 2 claims: 1 supported, 1 refuted.",
        "medium",
    )
    assert brief["confidence_reason"] == CHECK_RESULT.confidence.reason
    assert asked == [{"public_only": True, "max_results": 3}]
    assert checker.check_options == {"max_claims": 6}
    refuted, supported = brief["claims"]
    assert refuted == {
        "claim": "Python 3.13 was released on October 7, 2023.",
        "in_text": "Python 3.13 was released on October 7, 2023.",
        "ruling": "refuted",
        "note": "The sources give October 7, 2024.",
        "supports": [],
        "refutes": [
            {
                "quote": "Python 3.13.0 was released on October 7, 2024.",
                "url": "https://www.python.org/downloads/release/python-3130/",
                "site": "python.org",
                "date": "2024-10-07",
            }
        ],
        "set_aside": [
            {
                "quote": "Python 3.13 was released on October 7, 2024.",
                "why": "the quote does not contain 2023",
            }
        ],
        "unchecked": [],
    }
    assert supported["ruling"] == "supported"
    assert [quote["site"] for quote in supported["supports"]] == [
        "docs.python.org",
        "realpython.com",
    ]
    assert recalled.structured_content["facts"][0]["run_id"] == brief["run_id"]


def test_a_fact_check_waiting_for_an_answer_names_the_request_file(app, monkeypatch):
    pending = AnswerPending(Path("exchange/requests/abc.md"))
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(pending))
    (checked,) = call(app, ("fact_check", {"text": "Python 3.13 added a JIT."}))
    assert checked.is_error
    assert "abc.md" in checked.content[0].text


def test_a_fact_check_never_reads_a_private_address(app):
    app.public_fetcher = FakeFetcher({})
    urls = ["http://127.0.0.1:9/x", "http://169.254.169.254/latest/meta-data/"]
    checks = call(app, *(("fact_check", {"text": url}) for url in urls))
    for url, checked in zip(urls, checks, strict=True):
        assert checked.is_error
        assert f"will not read {url}: it is on a private network (" in checked.content[0].text
    assert app.public_fetcher.fetched == []


def test_assistants_read_only_the_public_internet(app, monkeypatch):
    asked, runs = [], []
    monkeypatch.setattr(
        App,
        "researcher",
        lambda self, **options: asked.append(options) or FakeResearcher(SAMPLE_RESULT),
    )

    def run_watch(app, watch, **options):
        runs.append(options["public_only"])
        raise SearchError("offline")

    monkeypatch.setattr("scout.server.run_watch", run_watch)
    call(
        app,
        ("research", {"goal": "cheapest RTX 5090"}),
        ("watch_add", {"name": "gpu", "goal": "cheapest RTX 5090"}),
        ("watch_run", {"name": "gpu"}),
    )
    assert [options["public_only"] for options in asked] == [True]
    assert runs == [True]
