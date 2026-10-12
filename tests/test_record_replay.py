"""A session recorded with record:DIR replays offline with replay:DIR, on any later day: the
same requests, so the same answers and the same result, with no model server."""

from __future__ import annotations

import functools
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest
import responses
from click.testing import CliRunner

from scout import app as app_module
from scout.app import App
from scout.cli import main
from scout.errors import AnswerPending, ConfigError
from scout.llm.base import Completion, CompletionRequest, Message
from scout.llm.exchange import SESSION_FILE, ExchangeBackend, ExchangeMode, read_session
from scout.llm.probe import Capabilities
from scout.llm.structured import StructuredMode
from scout.research.pipeline import Researcher
from scout.research.results import RunResult
from scout.settings import Settings
from tests.helpers import NOW, FakeFetcher, FakeSearch

BASE = "http://llm.local:1234/v1"
MODELS = "http://llm.local:1234/api/v1/models"
CHAT = f"{BASE}/chat/completions"
MODEL = "qwen3-8b"
REASONING = ("low", "medium", "high")
# A small window: the replay holds the same page text only if it plans for the same one.
CONTEXT = 4096

GOAL = "When was Python 3.13 released?"
LATER_GOAL = "What does Python 3.13 add?"
PAGE = "https://docs.example/3.13"
QUOTE = "Python 3.13 was released on October 7, 2024."
TEXT = " ".join(
    [QUOTE, *(f"Python 3.13 release note {n} changes module number {n}." for n in range(150))]
)
SEARCH = {
    query: [(PAGE, "What's new in Python 3.13", "Python 3.13")]
    for query in ("python 3.13 release date", "python 3.13 new features")
}
PLAN = {"queries": ["python 3.13 release date"], "kind": "release", "recency": "any"}
LATER_PLAN = {"queries": ["python 3.13 new features"], "kind": "release", "recency": "any"}
EXTRACTION = {
    "answer": "Python 3.13 came out on October 7, 2024 [1].",
    "findings": [{"claim": "Released October 7, 2024", "quote": QUOTE, "source": 1}],
}
REFUSED = {"status": 400, "json": {"error": "'response_format.type' must be 'json_schema'"}}
OVERFLOW = {"status": 400, "json": {"error": {"message": "maximum context length is 4096 tokens"}}}


def chat(content: dict | str, *, in_reasoning: bool = False) -> dict:
    text = content if isinstance(content, str) else json.dumps(content)
    message = {"content": "", "reasoning_content": text} if in_reasoning else {"content": text}
    return {"json": {"choices": [{"message": message, "finish_reason": "stop"}]}}


def lm_studio() -> dict:
    instance = {"id": MODEL, "config": {"context_length": CONTEXT}}
    return {
        "models": [
            {
                "key": MODEL,
                "loaded_instances": [instance],
                "capabilities": {"reasoning": {"allowed_options": list(REASONING)}},
            }
        ]
    }


def settings(tmp_path, llm: str, name: str, **changes) -> Settings:
    data = tmp_path / name  # apart: the replay keeps nothing the recording learned
    return Settings(
        llm=llm,
        llm_base_url=BASE,
        llm_model=MODEL,
        data_dir=data,
        reports_dir=data / "reports",
        **changes,
    )


def research(app: App, goal: str = GOAL, *, deep: bool = False) -> RunResult:
    app.fetcher.close()
    app.search, app.fetcher = FakeSearch(SEARCH), FakeFetcher({PAGE: TEXT})
    researcher = app.researcher()
    result = researcher.run_deep(goal, rounds=2) if deep else researcher.run(goal)
    app.learn_from(researcher)
    return result


def session_of(folder) -> dict:
    return json.loads((folder / SESSION_FILE).read_text(encoding="utf-8"))


def days_after(day: str, days: int) -> datetime:
    return datetime.combine(date.fromisoformat(day) + timedelta(days=days), time(9), UTC)


def same(replayed: RunResult, recorded: RunResult) -> bool:
    """The same result, but for when it ran."""
    times = {"started_at": recorded.started_at, "finished_at": recorded.finished_at}
    return replace(replayed, **times) == recorded


@pytest.fixture(autouse=True)
def runs_on(monkeypatch, clock):
    """Runs tell the time by *clock*, which a test moves to another day."""
    monkeypatch.setattr(app_module, "Researcher", functools.partial(Researcher, clock=clock))


@pytest.mark.parametrize(
    ("replies", "modes"),
    [
        pytest.param([chat(PLAN), chat(EXTRACTION)], ["json_schema"], id="json_schema works"),
        pytest.param(
            [chat(PLAN, in_reasoning=True), chat(EXTRACTION)],
            ["json_schema"],
            id="answered in the reasoning",
        ),
        pytest.param(
            [REFUSED, chat(PLAN), chat(EXTRACTION)],
            ["json_schema", "prompt"],
            id="refused at the first request",
        ),
        pytest.param(
            [chat(PLAN), REFUSED, chat(EXTRACTION)],
            ["json_schema", "prompt"],
            id="refused in the middle",
        ),
        pytest.param(
            [chat(PLAN), chat("no JSON"), chat("still no JSON"), chat(EXTRACTION)],
            ["json_schema", "prompt"],
            id="garbled in the middle",
        ),
        pytest.param(
            [chat(PLAN), OVERFLOW, chat(EXTRACTION)], ["json_schema"], id="overflowed in the middle"
        ),
        pytest.param(
            [chat(PLAN), OVERFLOW, chat("no JSON"), chat("still no JSON"), chat(EXTRACTION)],
            ["json_schema", "prompt"],
            id="overflowed, then garbled",
        ),
    ],
)
def test_a_recorded_run_replays_offline_on_a_later_day(tmp_path, clock, replies, modes):
    folder = tmp_path / "fixture"
    with responses.RequestsMock() as server:  # each reply must be asked for
        server.add(responses.GET, MODELS, json=lm_studio())
        server.add(responses.POST, CHAT, **chat({"ok": True, "word": "scout"}))
        for reply in replies:
            server.add(responses.POST, CHAT, **reply)
        with App(settings(tmp_path, f"record:{folder}", "recorded")) as app:
            checked = app.capabilities(refresh=True, test_structured=True)  # scout check
            assert not folder.exists()  # written with the first request recorded
            recorded = research(app)
    assert checked.structured_mode is StructuredMode.SCHEMA
    day = session_of(folder)["today"]
    for request in (folder / "requests").glob("*.md"):  # the day the run asked with
        assert f"Today is {day}." in request.read_text(encoding="utf-8")

    clock.now = days_after(day, 3)
    with responses.RequestsMock() as offline:
        with App(settings(tmp_path, f"replay:{folder}", "replayed")) as app:
            replayed = research(app)
            found = app.capabilities()
        assert not offline.calls
    assert replayed.started_at == clock.now
    assert same(replayed, recorded)
    assert [finding.claim for finding in replayed.trusted] == ["Released October 7, 2024"]
    assert found == Capabilities(
        model=MODEL,
        context_tokens=CONTEXT,
        structured_mode=StructuredMode(modes[0]),
        reasoning_options=REASONING,
        note=f"from the recorded session of {day} ({folder / SESSION_FILE})",
    )
    assert session_of(folder) == {
        "model": MODEL,
        "context_tokens": CONTEXT,
        "max_page_chars": 6000,
        "structured_modes": modes,
        "reasoning_options": list(REASONING),
        "today": day,
    }


def test_a_folder_keeps_its_day_so_a_later_recording_replays_too(tmp_path, clock):
    folder = tmp_path / "fixture"
    recording = settings(tmp_path, f"record:{folder}", "recorded")
    with responses.RequestsMock() as server:
        server.add(responses.GET, MODELS, json=lm_studio())
        for reply in (PLAN, EXTRACTION, LATER_PLAN, EXTRACTION):
            server.add(responses.POST, CHAT, **chat(reply))
        with App(recording) as app:
            first = research(app)
        day = session_of(folder)["today"]
        clock.now = days_after(day, 2)
        with App(recording) as app:
            later = research(app, LATER_GOAL)
    assert session_of(folder)["today"] == day

    clock.now = days_after(day, 9)
    with responses.RequestsMock() as offline:
        with App(settings(tmp_path, f"replay:{folder}", "replayed")) as app:
            assert same(research(app), first)
            assert same(research(app, LATER_GOAL), later)
        assert not offline.calls


def test_a_replay_reads_as_much_of_each_page_as_the_recording_did(tmp_path):
    folder = tmp_path / "fixture"
    with responses.RequestsMock() as server:
        server.add(responses.GET, MODELS, json=lm_studio())
        for reply in (PLAN, EXTRACTION):
            server.add(responses.POST, CHAT, **chat(reply))
        # One chunk of the page, where the window holds three.
        recording = settings(tmp_path, f"record:{folder}", "recorded", max_source_chars=1000)
        with App(recording) as app:
            recorded = research(app)
    assert session_of(folder)["max_page_chars"] == 1000

    with responses.RequestsMock() as offline:
        with App(settings(tmp_path, f"replay:{folder}", "replayed")) as app:  # 6000 by default
            assert same(research(app), recorded)
        assert not offline.calls


def test_a_deep_run_that_a_server_error_stopped_replays_the_same(tmp_path):
    folder = tmp_path / "fixture"
    crashed = {"status": 500, "json": {"error": "the model crashed"}}
    with responses.RequestsMock() as server:
        server.add(responses.GET, MODELS, json=lm_studio())
        for reply in (chat(PLAN), chat(EXTRACTION), crashed):
            server.add(responses.POST, CHAT, **reply)
        with App(settings(tmp_path, f"record:{folder}", "recorded")) as app:
            recorded = research(app, deep=True)
    assert recorded.warnings[-1] == (
        "stopped looking further: HTTP 500 from the model server: the model crashed"
    )

    with responses.RequestsMock() as offline:
        with App(settings(tmp_path, f"replay:{folder}", "replayed")) as app:
            assert same(research(app, deep=True), recorded)
        assert not offline.calls


def test_a_run_recorded_in_prompt_mode_after_another_fell_back_replays_too(tmp_path):
    folder = tmp_path / "fixture"
    with responses.RequestsMock() as server:
        server.add(responses.GET, MODELS, json=lm_studio())
        server.add(responses.POST, CHAT, **chat({"ok": True, "word": "scout"}))
        for reply in (chat(PLAN), REFUSED, chat(EXTRACTION), chat(LATER_PLAN), chat(EXTRACTION)):
            server.add(responses.POST, CHAT, **reply)
        with App(settings(tmp_path, f"record:{folder}", "recorded")) as app:
            app.capabilities(refresh=True, test_structured=True)
            first = research(app)  # falls back to prompt mode, and Scout remembers it
            later = research(app, LATER_GOAL)  # so this one asks in prompt mode only

    # A replay starts in json_schema mode; for the later run it finds no json_schema request.
    with responses.RequestsMock() as offline:
        with App(settings(tmp_path, f"replay:{folder}", "replayed")) as app:
            assert same(research(app), first)
            assert same(research(app, LATER_GOAL), later)
        assert not offline.calls


@responses.activate
def test_a_check_while_recording_leaves_the_folder_as_it_was(workspace, monkeypatch):
    responses.add(responses.GET, MODELS, json=lm_studio())
    responses.add(responses.POST, CHAT, **chat({"ok": True, "word": "scout"}))
    folder = workspace / "answers"
    monkeypatch.setenv("SCOUT_LLM", f"record:{folder}")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", BASE)
    monkeypatch.setenv("LM_STUDIO_MODEL", MODEL)
    for unset in ("SCOUT_PLANNER_MODEL", "SCOUT_CONTEXT_TOKENS"):
        monkeypatch.delenv(unset, raising=False)
    recording = CliRunner().invoke(main, ["check"], catch_exceptions=False)
    assert recording.exit_code == 0, recording.output
    assert "4,096 tokens" in recording.output
    assert not folder.exists()

    monkeypatch.setenv("SCOUT_LLM", f"exchange:{folder}")
    answering = CliRunner().invoke(main, ["check"], catch_exceptions=False)
    assert "exchange: unknown" in answering.output  # not the model the check probed
    assert "32,768 tokens" in answering.output
    assert "answered through exchange files" in answering.output


def test_a_run_answered_on_a_session_resumes_from_its_pinned_start_on_the_session_day(
    tmp_path, clock
):
    folder = tmp_path / "answers"
    folder.mkdir()
    session = {"model": "agent", "context_tokens": 32768, "today": "2026-09-01"}
    (folder / SESSION_FILE).write_text(json.dumps(session), encoding="utf-8")
    answering = settings(tmp_path, f"exchange:{folder}", "data")
    for reply in (PLAN, EXTRACTION):
        with App(answering) as app, pytest.raises(AnswerPending) as pending:
            research(app)
        request = pending.value.request_path
        assert "Today is 2026-09-01." in request.read_text(encoding="utf-8")
        answer = folder / "responses" / f"{request.stem}.txt"
        answer.write_text(json.dumps(reply), encoding="utf-8")
        clock.advance(6 * 3600)  # past midnight: the session's day still holds

    with App(answering) as app:
        result = research(app)
    assert result.started_at == NOW  # pinned when it first started
    assert result.model == "agent"
    assert [finding.claim for finding in result.trusted] == ["Released October 7, 2024"]


def test_recorded_capabilities_win_over_the_settings_and_check_names_the_session(
    workspace, monkeypatch
):
    folder = workspace / "fixture"
    folder.mkdir()
    (folder / SESSION_FILE).write_text(
        json.dumps(
            {
                "model": MODEL,
                "context_tokens": CONTEXT,
                "structured_modes": ["json_schema"],
                "reasoning_options": ["low"],
                "today": "2026-09-01",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SCOUT_LLM", f"replay:{folder}")
    monkeypatch.setenv("SCOUT_CONTEXT_TOKENS", "32000")
    checked = CliRunner().invoke(main, ["check"], catch_exceptions=False)
    assert checked.exit_code == 0, checked.output
    assert f"{MODEL}: unknown" in checked.output
    assert "4,096 tokens" in checked.output
    assert "structured mode json_schema" in checked.output
    assert "from the recorded session of 2026-09-01" in checked.output


class Server:
    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, request: CompletionRequest) -> Completion:
        return Completion(text="{}", model=self.model)

    def close(self) -> None:
        """Nothing to release."""


def asked(backend: ExchangeBackend, *, schema: bool) -> None:
    request = CompletionRequest(
        messages=(Message("user", "hi"),), schema={"type": "object"} if schema else None
    )
    backend.complete(request)


def test_a_session_keeps_its_modes_and_day_until_the_model_or_window_changes(tmp_path):
    first, later = date(2026, 9, 1), date(2026, 9, 9)
    recorder = ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=Server("a"))
    recorder.keep_session(context_tokens=8192, reasoning_options=(), today=first)
    asked(recorder, schema=False)
    asked(recorder, schema=True)
    recorder.keep_session(context_tokens=8192, reasoning_options=("low",), today=later)
    assert recorder.session.structured_modes == (StructuredMode.SCHEMA, StructuredMode.PROMPT)
    assert recorder.session.structured_mode is StructuredMode.SCHEMA
    assert (recorder.session.reasoning_options, recorder.session.today) == (("low",), first)

    again = ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=Server("a"))
    again.keep_session(context_tokens=4096, reasoning_options=(), today=later)
    # The older requests no longer replay: a new session, of its own day.
    assert (again.session.structured_modes, again.session.today) == ((), later)
    other = ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=Server("b"))
    asked(other, schema=False)
    assert (other.session.model, other.session.structured_modes) == ("b", (StructuredMode.PROMPT,))
    assert ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY).model == "b"


def recorder(folder, day: date) -> ExchangeBackend:
    backend = ExchangeBackend(folder, mode=ExchangeMode.RECORD, inner=Server("a"))
    backend.keep_session(context_tokens=8192, reasoning_options=(), today=day)
    return backend


def test_two_recorders_on_one_folder_keep_what_the_other_recorded(tmp_path):
    """A daemon and a command recording into one folder: each reads the session file again
    before it adds to it."""
    daemon, command = recorder(tmp_path, date(2026, 9, 1)), recorder(tmp_path, date(2026, 9, 1))
    asked(command, schema=True)
    asked(daemon, schema=False)
    session = read_session(tmp_path)
    assert session.structured_modes == (StructuredMode.SCHEMA, StructuredMode.PROMPT)
    assert ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY).session == session


def test_a_recorder_keeps_its_day_and_refuses_a_session_of_another(tmp_path):
    """Two recorders start a session in an empty folder, one before midnight and one after.
    The second asks with its own day, so its requests cannot join the first one's session."""
    before, after = recorder(tmp_path, date(2026, 9, 1)), recorder(tmp_path, date(2026, 9, 2))
    asked(before, schema=True)
    with pytest.raises(ConfigError, match="record into a new folder"):
        asked(after, schema=False)
    assert read_session(tmp_path) == before.session
    assert after.session.today == date(2026, 9, 2)  # still the day its runs ask with

    joining = recorder(tmp_path, date(2026, 9, 9))  # a later recorder goes on with the session
    assert joining.session.today == date(2026, 9, 1)
    asked(joining, schema=False)
    assert read_session(tmp_path).today == date(2026, 9, 1)


def test_a_session_file_that_recorded_nothing_does_not_set_the_day(tmp_path):
    """As an earlier version left it after scout check."""
    probed = {"model": "a", "context_tokens": 8192, "structured_modes": [], "today": "2026-08-01"}
    (tmp_path / SESSION_FILE).write_text(json.dumps(probed), encoding="utf-8")
    recording = recorder(tmp_path, date(2026, 9, 1))
    assert recording.session.today == date(2026, 9, 1)
    asked(recording, schema=False)
    assert read_session(tmp_path).today == date(2026, 9, 1)


@pytest.mark.parametrize(
    "text",
    ['{"context_tokens": 4096}', '{"model": "m", "structured_', "[]", '{"model": "m", "today": 9}'],
)
def test_an_unreadable_session_file_is_a_clear_error(tmp_path, text):
    (tmp_path / SESSION_FILE).write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="not a readable session file"):
        ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY)
