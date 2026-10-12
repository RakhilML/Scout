import pytest
from pydantic import BaseModel

from scout.errors import (
    AnswerPending,
    ConfigError,
    ContextOverflow,
    LLMError,
    LLMUnavailable,
    UnsupportedResponseFormat,
)
from scout.llm import make_backend, named_server
from scout.llm.base import Completion, CompletionRequest, Message
from scout.llm.exchange import ExchangeBackend, ExchangeMode, request_key
from scout.llm.openai_compat import OpenAICompatBackend
from scout.llm.structured import StructuredMode, generate
from scout.settings import Settings
from tests.helpers import ScriptedBackend

REQUEST = CompletionRequest(
    messages=(Message("system", "Extract facts."), Message("user", "Source 1: ...")),
    purpose="extract",
    schema={"type": "object"},
)


def test_answer_mode_writes_a_readable_request_then_uses_the_answer(tmp_path):
    backend = ExchangeBackend(tmp_path)
    with pytest.raises(AnswerPending) as pending:
        backend.complete(REQUEST)

    request_file = pending.value.request_path
    key = request_file.stem
    text = request_file.read_text(encoding="utf-8")
    assert request_file == tmp_path / "requests" / f"{key}.md"
    assert (tmp_path / "responses").is_dir()  # the answer can be written straight away
    assert f"responses/{key}.txt" in text
    assert "===== SYSTEM =====\nExtract facts." in text
    assert "===== JSON SCHEMA =====" in text
    assert b"\r\n" not in request_file.read_bytes()  # the same file on every system

    (tmp_path / "responses" / f"{key}.txt").write_text('{"facts": []}\n', encoding="utf-8")
    assert backend.complete(REQUEST) == Completion(text='{"facts": []}', model="exchange")


def test_replay_mode_never_writes_requests(tmp_path):
    with pytest.raises(LLMError, match="no recorded answer"):
        ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY).complete(REQUEST)
    assert not (tmp_path / "requests").exists()


class FixedBackend:
    model = "local-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        return Completion(text="recorded reply", model=self.model)


def test_record_mode_saves_exchanges_that_replay_mode_can_serve(tmp_path):
    inner = FixedBackend()
    recorder = ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=inner)
    assert recorder.complete(REQUEST).text == "recorded reply"
    assert recorder.complete(REQUEST).text == "recorded reply"
    assert inner.calls == 1

    replay = ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY)  # the session names the model
    assert replay.model == "local-model"
    assert replay.complete(REQUEST).text == "recorded reply"


class Unwritable(FixedBackend):
    def complete(self, request: CompletionRequest) -> Completion:
        return Completion(text='{"word": "half \ud800"}', model=self.model)  # cannot be UTF-8


def test_a_recording_cut_short_leaves_no_answer_to_be_read_later(tmp_path):
    recorder = ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=Unwritable())
    with pytest.raises(UnicodeEncodeError):
        recorder.complete(REQUEST)
    assert not any((tmp_path / "responses").iterdir())  # so the server is asked again


class Reasoner(FixedBackend):
    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1  # the whole answer in the reasoning, as some servers give it
        return Completion(text="", reasoning='{"facts": []}\r\n', model=self.model)


def test_a_recording_keeps_the_reasoning_a_reply_may_hold_its_answer_in(tmp_path):
    live = ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=Reasoner()).complete(REQUEST)
    replayed = ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY).complete(REQUEST)
    assert (replayed.text, replayed.reasoning) == (live.text, live.reasoning)


class Failing(FixedBackend):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        ContextOverflow("the prompt does not fit: 5000 > 4096"),
        UnsupportedResponseFormat("the server rejected the output format"),
        LLMError("HTTP 500 from the model server: it crashed"),
        LLMUnavailable("cannot reach the model server at http://llm.local"),
    ],
)
def test_a_recording_keeps_model_errors_that_replay_and_exchange_raise_again(tmp_path, error):
    with pytest.raises(type(error)):
        ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=Failing(error)).complete(REQUEST)
    for mode in (ExchangeMode.REPLAY, ExchangeMode.ANSWER):
        with pytest.raises(LLMError) as raised:
            ExchangeBackend(tmp_path, mode=mode).complete(REQUEST)
        assert (type(raised.value), str(raised.value)) == (type(error), str(error))


def test_a_recorder_asks_again_for_a_request_that_failed(tmp_path):
    def record(server):
        return ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=server).complete(REQUEST)

    with pytest.raises(LLMUnavailable):
        record(Failing(LLMUnavailable("cannot reach the model server")))
    failing = Failing(LLMError("HTTP 500 from the model server"))
    with pytest.raises(LLMError, match="HTTP 500"):
        record(failing)
    assert failing.calls == 1  # asked, not answered from the record
    with pytest.raises(LLMError, match="HTTP 500"):  # what happened last
        ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY).complete(REQUEST)

    assert record(FixedBackend()).text == "recorded reply"
    assert ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY).complete(REQUEST).text == (
        "recorded reply"
    )
    assert not list((tmp_path / "responses").glob("*.error.json"))


def test_an_unreadable_error_record_is_a_clear_error(tmp_path):
    record = tmp_path / "responses" / f"{request_key('exchange', REQUEST)}.error.json"
    record.parent.mkdir()
    record.write_text('{"error": "Oops", "message": "?"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="not a readable error record"):
        ExchangeBackend(tmp_path).complete(REQUEST)


class Facts(BaseModel):
    facts: list[str]


def test_a_reply_with_a_carriage_return_replays_with_its_repair(tmp_path):
    """The repair request holds the reply it repairs: read back changed, it is another one."""
    server = ScriptedBackend(["Here they are:\r\nnot JSON", '{"facts": ["a"]}'])
    messages = [Message("user", "List the facts.")]

    def facts(backend):
        return generate(backend, messages, Facts, mode=StructuredMode.PROMPT, purpose="extract")

    live = facts(ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD, inner=server))
    assert facts(ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY)) == live


def test_record_mode_requires_an_inner_backend(tmp_path):
    with pytest.raises(ValueError, match="inner"):
        ExchangeBackend(tmp_path, mode=ExchangeMode.RECORD)


def test_keys_depend_on_everything_that_changes_the_answer():
    base = request_key("m", REQUEST)
    assert base == request_key("m", REQUEST)
    assert base != request_key("other-model", REQUEST)
    changed = CompletionRequest(messages=(Message("user", "different"),), schema={"type": "object"})
    assert base != request_key("m", changed)
    assert request_key("m", REQUEST) == request_key(
        "m",
        CompletionRequest(messages=REQUEST.messages, purpose="another step", schema=REQUEST.schema),
    )


def test_make_backend_parses_the_setting(tmp_path):
    assert isinstance(make_backend(Settings(llm="openai", llm_model="m")), OpenAICompatBackend)
    exchange = make_backend(Settings(llm=f"exchange:{tmp_path}"))
    assert isinstance(exchange, ExchangeBackend)
    assert exchange.directory == tmp_path.resolve()
    with pytest.raises(ConfigError, match="LM_STUDIO_MODEL"):
        make_backend(Settings(llm="openai"))
    with pytest.raises(ConfigError, match="needs a folder"):
        make_backend(Settings(llm="exchange:"))
    with pytest.raises(ConfigError, match="unknown"):
        make_backend(Settings(llm="claude"))


def test_the_named_server_needs_no_model_and_exists_only_for_server_backends(tmp_path):
    for llm in ("openai", f"record:{tmp_path}"):
        server = named_server(Settings(llm=llm, llm_base_url="http://llm.local/v1"))
        assert isinstance(server, OpenAICompatBackend)
        assert server.base_url == "http://llm.local/v1"
    assert named_server(Settings(llm=f"exchange:{tmp_path}")) is None
    assert named_server(Settings(llm=f"replay:{tmp_path}")) is None
    with pytest.raises(ConfigError, match="unknown"):
        named_server(Settings(llm="claude"))
