"""Scout's MCP tools, called the way an assistant calls them: through an MCP client session."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import responses

from scout.app import App
from scout.errors import AnswerPending, SearchError
from scout.monitor.diff import Change, Delta, Fact
from scout.monitor.rules import parse_rule, triggers
from scout.monitor.runner import run_watch
from scout.monitor.watches import WatchBook
from scout.research.citations import relinked
from scout.research.results import Finding, Source, Verdict
from scout.settings import Settings
from scout.web.archive import Copy, Snapshot
from tests.helpers import (
    AUDIT_RESULT,
    CHECK_RESULT,
    CHECKED_TEXT,
    CITE_RESULT,
    CITED_TEXT,
    GONE,
    HAS_JIT,
    NOW,
    RELEASED_2023,
    SAMPLE_RESULT,
    FakeFetcher,
    FakeResearcher,
    FakeSearch,
    ScriptedBackend,
)

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
    placed = replace(SAMPLE_RESULT.findings[0], anchor="text=Now%20%241%2C999%20at%20Shop.")
    result = replace(SAMPLE_RESULT, findings=(placed, *SAMPLE_RESULT.findings[1:]))
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(result))
    researched, recalled = call(
        app, ("research", {"goal": "cheapest RTX 5090"}), ("recall", {"question": "rtx shop"})
    )
    brief = researched.structured_content
    assert brief["confidence"] == "medium"
    (finding,) = brief["findings"]
    at_quote = "https://shop.example/5090#:~:text=Now%20%241%2C999%20at%20Shop."
    assert finding == {
        "claim": "Shop sells it for $1,999",
        "quote": "Now $1,999 at Shop.",
        "url": "https://shop.example/5090",
        "site": "shop.example",
        "date": "2026-07-27",
        "link": at_quote,
    }
    assert [item["claim"] for item in brief["set_aside"]] == [
        "Shop Z sells it for $800",
        "It ships free",
    ]
    (fact,) = recalled.structured_content["facts"]
    assert (fact["run_id"], fact["link"]) == (brief["run_id"], at_quote)


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


def test_watch_changes_links_each_alert_to_its_quote(app):
    placed = Fact(
        key="text|shop.example|free",
        claim="It ships free",
        quote="Free shipping.",
        url="https://shop.example/5090",
        seen=NOW,
        anchor="text=Free%20shipping.",
    )
    unplaced = replace(placed, key="text|shop.example|fast", quote="Fast.", anchor=None)
    deltas = [Delta(Change.NEW, placed), Delta(Change.NEW, unplaced)]
    call(app, ("watch_add", {"name": "gpu", "goal": "cheapest RTX 5090", "alerts": ["new"]}))
    fired = triggers([parse_rule("new")], deltas, baseline=False)
    app.store.record("gpu", SAMPLE_RESULT, deltas, raised=fired)

    (changes,) = call(app, ("watch_changes", {"name": "gpu"}))
    assert [(alert["url"], alert["link"]) for alert in changes.structured_content["alerts"]] == [
        ("https://shop.example/5090", "https://shop.example/5090"),
        ("https://shop.example/5090", "https://shop.example/5090#:~:text=Free%20shipping."),
    ]


def test_read_only_tools_say_so(app):
    async def tools():
        async with mcp.Client(build(app)) as client:
            return (await client.list_tools()).tools

    read_only = {tool.name for tool in asyncio.run(tools()) if tool.annotations.read_only_hint}
    assert read_only == {"recall", "report", "watch_list", "watch_changes", "watch_trend"}


def test_fact_check_returns_each_ruling_with_its_evidence_and_remembers_it(app, monkeypatch):
    jit = replace(CHECK_RESULT.findings[1], anchor="text=Python%203.13%20adds")
    result = replace(
        CHECK_RESULT, findings=(CHECK_RESULT.findings[0], jit, *CHECK_RESULT.findings[2:])
    )
    checker, asked = FakeResearcher(result), []
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
    assert asked == [{"public_only": True, "max_results": 3, "archive": False, "find_moved": False}]
    assert checker.check_options == {"max_claims": 6, "cited": False}
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
                "link": "https://www.python.org/downloads/release/python-3130/",
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
    assert [(quote["site"], quote["link"]) for quote in supported["supports"]] == [
        (
            "docs.python.org",
            "https://docs.python.org/3/whatsnew/3.13.html#:~:text=Python%203.13%20adds",
        ),
        ("realpython.com", "https://realpython.com/python313-new-features/"),
    ]
    assert recalled.structured_content["facts"][0]["run_id"] == brief["run_id"]


def test_a_cite_check_says_what_each_cited_page_said(app, monkeypatch):
    checker = FakeResearcher(CITE_RESULT)
    monkeypatch.setattr(App, "researcher", lambda self, **options: checker)
    (checked,) = call(app, ("fact_check", {"text": CITED_TEXT, "cited": True}))
    brief = checked.structured_content
    assert checker.check_options == {"max_claims": 6, "cited": True}
    assert (brief["cited"], brief["summary"]) == (True, CITE_RESULT.answer)
    release, _, jit, ios = brief["claims"]
    assert (release["label"], release["ruling"], release["cites"]) == (
        "contradicted",
        "refuted",
        [{"n": 1, "url": "https://www.python.org/downloads/release/python-3130/", "read": True}],
    )
    assert release["refutes"][0]["quote"] == "Python 3.13.0 was released on October 7, 2024."
    assert (jit["label"], jit["problems"]) == ("not found", [])
    assert ios == {
        **ios,
        "label": "unreadable",
        "cites": [{"n": 4, "url": "https://example.org/gone", "read": False}],
        "problems": ["could not read [4] example.org (not found: HTTP 404)"],
    }


@responses.activate
def test_a_cite_check_over_mcp_never_connects_to_a_private_address(app, monkeypatch):
    public, private = "http://93.184.216.34/python-3.13", "http://127.0.0.1:8080/x"
    page = "Python 3.13 was released on October 7, 2024. " * 12
    responses.add(
        responses.GET,
        public,
        body=f"<html><body><article><p>{page}</p></article></body></html>",
        content_type="text/html",
    )
    released = "Python 3.13 was released on October 7, 2024."
    listed = [
        {"claim": released, "excerpt": f"{released[:-1]} [1].", "query": "q"},
        {"claim": "The build server has 3 users.", "excerpt": "It has 3 users [2].", "query": "q"},
    ]
    support = {"stance": "supports", "quote": released, "source": 1, "says": released}
    model = ScriptedBackend(
        {"claims": [{"claims": listed}], "judge": [{"evidence": [support], "note": ""}]}
    )
    monkeypatch.setattr("scout.app.make_backend", lambda settings: model)
    app.search = FakeSearch({})
    text = f"{released}[1] It has 3 users.[2]\n\n[1] {public}\n[2] {private}"
    (checked,) = call(app, ("fact_check", {"text": text, "cited": True}))

    brief = checked.structured_content
    assert [(c["label"], c["cites"]) for c in brief["claims"]] == [
        ("backed", [{"n": 1, "url": public, "read": True}]),
        ("unreadable", [{"n": 2, "url": private, "read": False}]),
    ]
    assert brief["claims"][1]["problems"] == [
        "could not read [2] 127.0.0.1 (refused: it is on a private network (127.0.0.1))"
    ]
    assert [request.request.url for request in responses.calls] == [public]
    assert model.purposes() == ["claims", "judge"]
    assert app.search.calls == []


@responses.activate
def test_an_assistant_can_cite_check_a_web_address_on_the_public_internet_only(app, monkeypatch):
    page, release, private = (
        "http://93.184.216.34/review",
        "http://93.184.216.35/python-3.13",
        "http://127.0.0.1:8080/x",
    )
    released = "Python 3.13 was released on October 7, 2024."
    review = (
        f'<p>{released[:-1]}, says <a href="{release}">the release page</a>. Its build server has '
        f'3 users, per <a href="{private}">the dashboard</a>.</p><p>'
        + "Python 3.13 brought many changes that this review goes through one by one. " * 6
        + "</p>"
    )
    for url, body in ((page, review), (release, f"<p>{released * 12}</p>")):
        responses.add(
            responses.GET,
            url,
            body=f"<html><body><article>{body}</article></body></html>",
            content_type="text/html",
        )
    listed = [
        {
            "claim": released,
            "excerpt": f"{released[:-1]}, says the release page [1].",
            "query": "q",
        },
        {
            "claim": "The build server has 3 users.",
            "excerpt": "Its build server has 3 users, per the dashboard [2].",
            "query": "q",
        },
    ]
    support = {"stance": "supports", "quote": released, "source": 1, "says": released}
    model = ScriptedBackend(
        {"claims": [{"claims": listed}], "judge": [{"evidence": [support], "note": ""}]}
    )
    monkeypatch.setattr("scout.app.make_backend", lambda settings: model)
    checked, refused = call(
        app,
        ("fact_check", {"text": page, "cited": True}),
        ("fact_check", {"text": "http://127.0.0.1:9/x", "cited": True}),
    )

    brief = checked.structured_content
    assert [(c["label"], c["cites"]) for c in brief["claims"]] == [
        ("backed", [{"n": 1, "url": release, "read": True}]),
        ("unreadable", [{"n": 2, "url": private, "read": False}]),
    ]
    assert brief["claims"][1]["problems"] == [
        "could not read [2] 127.0.0.1 (refused: it is on a private network (127.0.0.1))"
    ]
    assert refused.is_error
    assert "will not read http://127.0.0.1:9/x" in refused.content[0].text
    assert [request.request.url for request in responses.calls] == [page, release]


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


def test_a_claim_watch_reports_how_each_claims_ruling_moved(app, monkeypatch):
    released = replace(CHECK_RESULT.findings[0], anchor="text=Python%203.13.0%20was%20released")
    checker = FakeResearcher(replace(CHECK_RESULT, findings=(released, *CHECK_RESULT.findings[1:])))
    monkeypatch.setattr(App, "researcher", lambda self, **options: checker)
    added, first, second, listed = call(
        app,
        ("watch_add", {"name": "py", "goal": CHECKED_TEXT, "check": True}),
        ("watch_run", {"name": "py"}),
        ("watch_run", {"name": "py"}),
        ("watch_list", {}),
    )
    assert (added.structured_content["check"], added.structured_content["alerts"]) == (
        True,
        ["changed"],
    )
    assert WatchBook(app.settings.watches_path).get("py").check
    assert checker.checked == [CHECKED_TEXT, CHECKED_TEXT]

    baseline = first.structured_content
    assert (baseline["first_run"], baseline["changes"]) == (True, {"new": 2})
    assert baseline["claims"] == [
        {
            "claim": RELEASED_2023,
            "ruling": "refuted",
            "was": None,
            "change": "new",
            "quote": "Python 3.13.0 was released on October 7, 2024.",
            "url": "https://www.python.org/downloads/release/python-3130/",
            "link": "https://www.python.org/downloads/release/python-3130/"
            "#:~:text=Python%203.13.0%20was%20released",
            "gone": False,
            "noticed": [],
        },
        {
            "claim": HAS_JIT,
            "ruling": "supported",
            "was": None,
            "change": "new",
            "quote": "Python 3.13 adds an experimental just-in-time (JIT) compiler.",
            "url": "https://docs.python.org/3/whatsnew/3.13.html",
            "link": "https://docs.python.org/3/whatsnew/3.13.html",
            "gone": False,
            "noticed": [],
        },
    ]
    again = second.structured_content
    assert again["changes"] == {"same": 2}
    assert [(c["ruling"], c["was"], c["change"]) for c in again["claims"]] == [
        ("refuted", "refuted", "same"),
        ("supported", "supported", "same"),
    ]
    assert [watch["check"] for watch in listed.structured_content["watches"]] == [True]


def test_a_citation_watch_reports_only_what_moved_once_it_has_run(app, monkeypatch):
    sources = "\n".join(f"[{page.index}] {page.url}" for page in AUDIT_RESULT.sources)
    added, listed, refused = call(
        app,
        ("watch_add", {"name": "notes", "goal": f"{CITED_TEXT}\n\n{sources}", "cited": True}),
        ("watch_list", {}),
        ("watch_run", {"name": "notes"}),
    )
    view = added.structured_content
    assert (view["check"], view["cited"], view["alerts"]) == (True, True, ["changed", "dead"])
    assert [watch["cited"] for watch in listed.structured_content["watches"]] == [True]
    assert refused.is_error
    assert "citation watch notes has not run yet: its first run audits" in refused.content[0].text
    assert "run `scout watch run notes` or let `scout daemon` run it" in refused.content[0].text

    checker = FakeResearcher(AUDIT_RESULT)
    monkeypatch.setattr(App, "researcher", lambda self, **options: checker)
    run_watch(app, WatchBook(app.settings.watches_path).get("notes"))  # from the CLI or daemon
    released, _, faster, ios = AUDIT_RESULT.claims
    said = "It runs on iOS as a tier 3 platform."
    checker.outcome = replace(
        AUDIT_RESULT,
        sources=(
            *AUDIT_RESULT.sources[:3],
            replace(AUDIT_RESULT.sources[3], status="ok", snippet_only=False, text=said),
        ),
        findings=(
            *AUDIT_RESULT.findings,
            Finding(
                claim=said, quote=said, source=4, verdict=Verdict.VERIFIED, anchor="text=It%20runs"
            ),
        ),
        claims=(released, faster, replace(ios, supports=(3,), problems=())),
    )
    (ran,) = call(app, ("watch_run", {"name": "notes"}))

    view = ran.structured_content
    options = checker.check_options
    assert (options["cited"], options["audit"], options["reuse"].goal) == (
        True,
        True,
        AUDIT_RESULT.goal,
    )
    assert (view["first_run"], view["model_asked"]) == (False, True)
    assert view["labels"] == {"contradicted": 1, "not found": 1, "backed": 1}
    assert view["claims"] == [
        {
            "claim": ios.claim,
            "label": "backed",
            "was": None,
            "change": "new",
            "quote": said,
            "url": GONE,
            "link": f"{GONE}#:~:text=It%20runs",
            "cites": [GONE],
            "gone": False,
        }
    ]
    assert view["pages"] == [
        {
            "url": GONE,
            "state": "read",
            "was": "not found: HTTP 404",
            "why": None,
            "archive": None,
            "archived": None,
        }
    ]
    assert view["left"] == 0  # a claim missing once is kept, in case it comes back
    assert view["alerts"] == []


COPY = f"https://web.archive.org/web/20241102083000/{GONE}"
LINK = f"{COPY}#:~:text=It%20runs"
ON_IOS = "It runs on iOS as a tier 3 platform."
IOS = CITE_RESULT.claims[3]
# CITE_RESULT with its dead [4] judged on a copy that backs what the text cites it for.
ARCHIVED_RESULT = replace(
    CITE_RESULT,
    sources=(
        *CITE_RESULT.sources,
        Source(5, COPY, "Python on phones", "example.org", "ok", "q", copy_of=4),
    ),
    findings=(
        *CITE_RESULT.findings,
        Finding(ON_IOS, ON_IOS, 5, Verdict.VERIFIED, anchor="text=It%20runs"),
    ),
    claims=(
        *CITE_RESULT.claims[:3],
        replace(IOS, archived=replace(IOS, problems=(), supports=(3,), pages=(5,))),
    ),
)


def test_a_cite_check_can_judge_dead_pages_on_their_archived_copies(app, monkeypatch):
    asked = []
    monkeypatch.setattr(
        App,
        "researcher",
        lambda self, **options: asked.append(options) or FakeResearcher(ARCHIVED_RESULT),
    )
    checked, refused = call(
        app,
        ("fact_check", {"text": CITED_TEXT, "cited": True, "archive": True}),
        ("fact_check", {"text": CITED_TEXT, "archive": True}),
    )

    assert asked == [{"public_only": True, "max_results": 3, "archive": True, "find_moved": False}]
    release, *_, dead = checked.structured_content["claims"]
    assert (release["archived"], dead["label"]) == (None, "unreadable")
    assert dead["archived"] == {
        "label": "backed",
        "copies": [{"of": 4, "url": COPY, "taken": "2024-11-02", "read": True}],
        "moved": [],
        "supports": [
            {
                "quote": ON_IOS,
                "url": COPY,
                "site": "example.org",
                "date": None,
                "link": LINK,
            }
        ],
        "refutes": [],
        "set_aside": [],
        "problems": [],
    }
    (link,) = checked.structured_content["dead_links"]
    assert (link["n"], link["state"], link["backs"], link["link"]) == (4, "replace", [4], LINK)
    assert "fixed_text" not in checked.structured_content
    assert refused.is_error
    assert refused.content[0].text.endswith(": archive applies to cited checks")


def test_an_assistant_gets_its_text_back_with_dead_citations_fixed(app, monkeypatch):
    asked = []
    monkeypatch.setattr(
        App,
        "researcher",
        lambda self, **options: asked.append(options) or FakeResearcher(ARCHIVED_RESULT),
    )
    text = f"{CITED_TEXT}\n\n[4] {GONE}\n"
    fixed, uncited, page = call(
        app,
        ("fact_check", {"text": text, "cited": True, "fix": True}),
        ("fact_check", {"text": text, "fix": True}),
        ("fact_check", {"text": "https://blog.example.com/post", "cited": True, "fix": True}),
    )

    assert asked == [{"public_only": True, "max_results": 3, "archive": True, "find_moved": False}]
    view = fixed.structured_content
    assert view["fixed_text"] == relinked(text, {GONE: LINK}).text
    assert view["fixed_text"] == text.replace(f"[4] {GONE}", f"[4] {LINK}")
    assert [(link["n"], link["state"]) for link in view["dead_links"]] == [(4, "replace")]
    assert uncited.is_error
    assert uncited.content[0].text.endswith(": fix applies to cited checks")
    assert page.is_error
    assert page.content[0].text.endswith(": fix rewrites a text: pass the text itself")


NEW = "https://python.example/ios"
NEW_LINK = f"{NEW}#:~:text=It%20runs"
# ARCHIVED_RESULT with its dead [4] found live at NEW, which still holds the copy's quote.
MOVED_RESULT = replace(
    ARCHIVED_RESULT,
    sources=(
        *ARCHIVED_RESULT.sources,
        Source(6, NEW, "Python on phones", "python.example", "ok", "q", moved_from=4),
    ),
    findings=(
        *ARCHIVED_RESULT.findings,
        Finding(ON_IOS, ON_IOS, 6, Verdict.VERIFIED, anchor="text=It%20runs"),
    ),
    claims=(
        *CITE_RESULT.claims[:3],
        replace(IOS, archived=replace(IOS, problems=(), supports=(3, 4), pages=(5,))),
    ),
)


def test_an_assistant_can_have_dead_citations_cite_where_their_pages_moved(app, monkeypatch):
    asked = []
    monkeypatch.setattr(
        App,
        "researcher",
        lambda self, **options: asked.append(options) or FakeResearcher(MOVED_RESULT),
    )
    text = f"{CITED_TEXT}\n\n[4] {GONE}\n"
    fixed, uncited = call(
        app,
        ("fact_check", {"text": text, "cited": True, "fix": True, "find_moved": True}),
        ("fact_check", {"text": text, "find_moved": True}),
    )

    assert asked == [{"public_only": True, "max_results": 3, "archive": True, "find_moved": True}]
    view = fixed.structured_content
    archived = view["claims"][3]["archived"]
    assert archived["moved"] == [{"of": 4, "url": NEW}]
    assert [copy["of"] for copy in archived["copies"]] == [4]
    assert [quote["link"] for quote in archived["supports"]] == [LINK, NEW_LINK]
    (link,) = view["dead_links"]
    assert (link["state"], link["moved"], link["link"]) == ("moved", NEW, NEW_LINK)
    assert view["fixed_text"] == text.replace(f"[4] {GONE}", f"[4] {NEW_LINK}")
    assert uncited.is_error
    assert uncited.content[0].text.endswith(": find_moved applies to cited checks")


def test_a_citation_watch_run_gives_a_dead_pages_archived_copy(app, monkeypatch):
    sources = "\n".join(f"[{page.index}] {page.url}" for page in AUDIT_RESULT.sources)
    call(app, ("watch_add", {"name": "notes", "goal": f"{CITED_TEXT}\n\n{sources}", "cited": True}))
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(AUDIT_RESULT))
    first = run_watch(app, WatchBook(app.settings.watches_path).get("notes"))
    read = Fact(key=f"page|{GONE}", claim=ON_IOS, quote=ON_IOS, url=GONE, seen=NOW, value="read")
    gone = replace(read, value="not found: HTTP 404", missed=True)
    taken = datetime(2024, 11, 2, 8, 30, tzinfo=UTC)
    copy = Copy(Snapshot(GONE, taken), holds=True, anchor="text=It%20runs")
    died = replace(first, pages=(Delta(Change.CHANGED, gone, read),), copies={read.key: copy})
    monkeypatch.setattr("scout.server.run_watch", lambda app, watch, **options: died)
    (ran,) = call(app, ("watch_run", {"name": "notes"}))

    assert ran.structured_content["pages"] == [
        {
            "url": GONE,
            "state": "dead",
            "was": "read",
            "why": "not found: HTTP 404",
            "archive": "archived",
            "archived": {
                "taken": "2024-11-02",
                "url": COPY,
                "link": f"{COPY}#:~:text=It%20runs",
                "holds": True,
                "read": True,
            },
        }
    ]
    unanswered = replace(died, copies={})
    monkeypatch.setattr("scout.server.run_watch", lambda app, watch, **options: unanswered)
    (ran,) = call(app, ("watch_run", {"name": "notes"}))
    (page,) = ran.structured_content["pages"]
    assert (page["archive"], page["archived"]) == ("not looked up", None)


def test_a_quote_a_claim_watch_noticed_links_to_its_place_too(app, monkeypatch):
    monkeypatch.setattr(App, "researcher", lambda self, **options: FakeResearcher(CHECK_RESULT))
    url = "https://docs.python.org/3/whatsnew/3.13.html"
    seen = Fact(
        key="evidence|x",
        claim=HAS_JIT,
        value="supports",
        quote="Python 3.13 ships a JIT.",
        url=url,
        seen=CHECK_RESULT.started_at,
        noticed=True,
        anchor="text=ships%20a%20JIT",
    )
    monkeypatch.setattr("scout.server.noticed_for", lambda key, deltas, standing: [seen])
    (ran,) = call(
        app,
        ("watch_add", {"name": "py", "goal": CHECKED_TEXT, "check": True}),
        ("watch_run", {"name": "py"}),
    )[1:]
    (noticed, *_) = ran.structured_content["claims"][0]["noticed"]
    assert noticed["link"] == f"{url}#:~:text=ships%20a%20JIT"
