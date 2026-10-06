"""Scout's MCP tools, called the way an assistant calls them: through an MCP client session."""

import asyncio

import pytest

from scout.app import App
from scout.errors import SearchError
from scout.settings import Settings
from tests.helpers import SAMPLE_RESULT, FakeResearcher

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
