import json

import pytest
import requests
import responses

from scout.errors import (
    ContextOverflow,
    LLMAuthError,
    LLMError,
    LLMUnavailable,
    UnsupportedResponseFormat,
)
from scout.llm.base import CompletionRequest, Message
from scout.llm.openai_compat import ModelInfo, OpenAICompatBackend

BASE = "http://llm.local:1234/v1"
CHAT = f"{BASE}/chat/completions"
REQUEST = CompletionRequest(messages=(Message("system", "be brief"), Message("user", "hi")))


def reply(content="hello", **message_fields):
    return {
        "model": "openai/gpt-oss-20b",
        "choices": [
            {
                "message": {"role": "assistant", "content": content, **message_fields},
                "finish_reason": "stop",
            }
        ],
    }


def backend(**kwargs) -> OpenAICompatBackend:
    return OpenAICompatBackend(BASE, "openai/gpt-oss-20b", **kwargs)


@responses.activate
def test_request_payload_is_minimal_and_authenticated():
    responses.add(responses.POST, CHAT, json=reply())
    completion = backend(api_key="tok").complete(REQUEST)

    sent = responses.calls[0].request
    body = json.loads(sent.body)
    assert sent.headers["Authorization"] == "Bearer tok"
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert "max_tokens" not in body
    assert "response_format" not in body
    assert "ttl" not in body  # an LM Studio extension: only sent when asked for
    assert completion.text == "hello"
    assert completion.finish_reason == "stop"


@responses.activate
def test_schema_requests_use_json_schema_format():
    responses.add(responses.POST, CHAT, json=reply("{}"))
    schema = {"type": "object", "properties": {}}
    backend().complete(
        CompletionRequest(
            messages=REQUEST.messages, schema=schema, schema_name="Plan", reasoning_effort="low"
        )
    )
    body = json.loads(responses.calls[0].request.body)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Plan", "schema": schema, "strict": True},
    }
    assert body["reasoning_effort"] == "low"


@responses.activate
def test_a_model_ttl_is_passed_on():
    responses.add(responses.POST, CHAT, json=reply())
    backend(ttl=600).complete(REQUEST)
    assert json.loads(responses.calls[0].request.body)["ttl"] == 600


@responses.activate
def test_no_authorization_header_without_a_key():
    responses.add(responses.POST, CHAT, json=reply())
    backend().complete(REQUEST)
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_reasoning_is_separated_and_inline_think_blocks_are_removed():
    responses.add(
        responses.POST, CHAT, json=reply("<think>hmm</think>\nAnswer", reasoning_content="why")
    )
    completion = backend().complete(REQUEST)
    assert (completion.text, completion.reasoning) == ("Answer", "why")

    responses.replace(responses.POST, CHAT, json=reply("<think>cut off mid-thought", reasoning="r"))
    completion = backend().complete(REQUEST)
    assert (completion.text, completion.reasoning) == ("", "r")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"error": "Unauthorized"}, LLMAuthError),
        (503, {"error": {"message": "No models loaded"}}, LLMUnavailable),
        (
            400,
            {
                "error": "request (9123 tokens) exceeds the available context size "
                "(8192 tokens), try increasing it"
            },
            ContextOverflow,
        ),
        (
            400,
            {
                "error": "Trying to keep the first 15857 tokens when context the overflows. "
                "However, the model is loaded with context length of only 4096 tokens, "
                "which is not enough."
            },
            ContextOverflow,
        ),
        (
            400,
            {"error": "'response_format.type' must be 'json_schema' or 'text'"},
            UnsupportedResponseFormat,
        ),
        (404, {"error": {"message": "Model not found"}}, LLMError),
    ],
)
@responses.activate
def test_server_errors_become_typed_exceptions(status, body, error):
    responses.add(responses.POST, CHAT, status=status, json=body)
    with pytest.raises(error):
        backend().complete(REQUEST)


@responses.activate
def test_auth_errors_explain_how_to_fix_them():
    responses.add(responses.POST, CHAT, status=401, json={"error": "Unauthorized"})
    with pytest.raises(LLMAuthError, match="LM_STUDIO_API_KEY"):
        backend().complete(REQUEST)


@pytest.mark.parametrize(
    ("exception", "error"),
    [(requests.ConnectionError(), LLMUnavailable), (requests.ReadTimeout(), LLMError)],
)
@responses.activate
def test_network_failures(exception, error):
    responses.add(responses.POST, CHAT, body=exception)
    with pytest.raises(error):
        backend().complete(REQUEST)


@responses.activate
def test_list_models():
    responses.add(
        responses.GET, f"{BASE}/models", json={"data": [{"id": "a"}, {"id": "b"}, {"x": 1}]}
    )
    assert backend().list_models() == ["a", "b"]


# The example response from LM Studio's REST docs, with gpt-oss loaded next to an unloaded model.
LMSTUDIO_MODELS = {
    "models": [
        {
            "type": "llm",
            "key": "openai/gpt-oss-20b",
            "loaded_instances": [
                {"id": "openai/gpt-oss-20b", "config": {"context_length": 8192, "parallel": 4}}
            ],
            "max_context_length": 131072,
            "capabilities": {
                "trained_for_tool_use": True,
                "reasoning": {"allowed_options": ["low", "medium", "high"], "default": "medium"},
            },
        },
        {"type": "llm", "key": "deepseek-r1", "loaded_instances": [], "max_context_length": 131072},
    ]
}


@responses.activate
def test_model_info_reads_lm_studio_native_api():
    responses.add(responses.GET, "http://llm.local:1234/api/v1/models", json=LMSTUDIO_MODELS)
    assert backend().model_info() == ModelInfo(
        id="openai/gpt-oss-20b",
        loaded=True,
        context_length=8192,
        max_context_length=131072,
        parallel=4,
        reasoning_options=("low", "medium", "high"),
    )


@responses.activate
def test_model_info_for_unloaded_or_unknown_models_and_other_servers():
    responses.add(responses.GET, "http://llm.local:1234/api/v1/models", json=LMSTUDIO_MODELS)
    info = OpenAICompatBackend(BASE, "deepseek-r1").model_info()
    assert info is not None
    assert (info.loaded, info.context_length, info.reasoning_options) == (False, None, ())
    assert OpenAICompatBackend(BASE, "missing").model_info() is None

    responses.replace(responses.GET, "http://llm.local:1234/api/v1/models", status=404)
    assert backend().model_info() is None
