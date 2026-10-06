import pytest

from scout.errors import AnswerPending, ConfigError, LLMError
from scout.llm import make_backend
from scout.llm.base import Completion, CompletionRequest, Message
from scout.llm.exchange import ExchangeBackend, ExchangeMode, request_key
from scout.llm.openai_compat import OpenAICompatBackend
from scout.settings import Settings

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

    replay = ExchangeBackend(tmp_path, mode=ExchangeMode.REPLAY, model="local-model")
    assert replay.complete(REQUEST).text == "recorded reply"


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
