import pytest
from pydantic import BaseModel

from scout.errors import StructuredOutputError
from scout.llm.base import Completion, CompletionRequest, Message
from scout.llm.structured import StructuredMode, extract_json_object, generate


class Plan(BaseModel):
    queries: list[str]
    recency: str | None = None


class ScriptedBackend:
    """Replies with the given completions in order and records every request."""

    model = "scripted"

    def __init__(self, *replies: str | Completion) -> None:
        self.replies = [r if isinstance(r, Completion) else Completion(text=r) for r in replies]
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return self.replies.pop(0)


MESSAGES = [Message("system", "You plan searches."), Message("user", "latest RTX 5090 prices")]


def test_prompt_mode_puts_the_schema_in_the_system_message():
    backend = ScriptedBackend('{"queries": ["rtx 5090 price"]}')
    plan = generate(backend, MESSAGES, Plan, mode=StructuredMode.PROMPT, purpose="plan")
    assert plan == Plan(queries=["rtx 5090 price"])
    (request,) = backend.requests
    assert request.schema is None
    assert request.messages[0].content.startswith("You plan searches.")
    assert '"queries"' in request.messages[0].content
    assert request.messages[1:] == tuple(MESSAGES[1:])


def test_schema_mode_leaves_the_prompt_alone_and_sends_the_schema():
    backend = ScriptedBackend('{"queries": []}')
    generate(
        backend, MESSAGES, Plan, mode=StructuredMode.SCHEMA, purpose="plan", reasoning_effort="low"
    )
    (request,) = backend.requests
    assert request.messages == tuple(MESSAGES)
    assert request.schema == Plan.model_json_schema()
    assert request.schema_name == "Plan"
    assert request.reasoning_effort == "low"


def test_json_is_found_inside_prose_and_code_fences():
    backend = ScriptedBackend(
        'Sure! Here it is:\n```json\n{"queries": ["a {b}"], "recency": "week"}\n```'
    )
    plan = generate(backend, MESSAGES, Plan, mode=StructuredMode.PROMPT, purpose="plan")
    assert plan == Plan(queries=["a {b}"], recency="week")


def test_answer_in_the_reasoning_field_is_used_when_content_is_empty():
    backend = ScriptedBackend(Completion(text="", reasoning='thinking... {"queries": ["x"]}'))
    assert generate(
        backend, MESSAGES, Plan, mode=StructuredMode.PROMPT, purpose="plan"
    ).queries == ["x"]


def test_one_repair_round_with_the_validation_error():
    backend = ScriptedBackend('{"queries": "not a list"}', '{"queries": ["fixed"]}')
    plan = generate(backend, MESSAGES, Plan, mode=StructuredMode.PROMPT, purpose="plan")
    assert plan.queries == ["fixed"]
    repair = backend.requests[1]
    assert repair.messages[-2] == Message("assistant", '{"queries": "not a list"}')
    assert "queries" in repair.messages[-1].content
    assert "not valid" in repair.messages[-1].content


def test_gives_up_after_the_repair_fails():
    backend = ScriptedBackend("no json here", "{still broken")
    with pytest.raises(StructuredOutputError, match="plan"):
        generate(backend, MESSAGES, Plan, mode=StructuredMode.PROMPT, purpose="plan")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('x {"a": "}{"} y {"b": 2}', '{"a": "}{"}'),
        ('{"a": "quote \\" inside"}', '{"a": "quote \\" inside"}'),
        ("{unbalanced", None),
        ("no braces", None),
        (
            '{"outer": {"inner": [1, {"deep": true}]}} tail',
            '{"outer": {"inner": [1, {"deep": true}]}}',
        ),
    ],
)
def test_extract_json_object(text, expected):
    assert extract_json_object(text) == expected
