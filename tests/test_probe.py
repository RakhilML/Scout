from dataclasses import replace

import pytest
import responses

from scout.app import App
from scout.llm.exchange import ExchangeBackend
from scout.llm.openai_compat import OpenAICompatBackend
from scout.llm.probe import AGENT_CONTEXT_TOKENS, DEFAULT_CONTEXT_TOKENS, Capabilities, probe
from scout.llm.structured import StructuredMode
from scout.settings import Settings

BASE = "http://llm.local:1234/v1"
MODELS = "http://llm.local:1234/api/v1/models"
CHAT = f"{BASE}/chat/completions"


def lmstudio(model: str, *, context: int = 16384, options=("low", "medium", "high")) -> dict:
    return {
        "models": [
            {
                "key": model,
                "loaded_instances": [
                    {"id": model, "config": {"context_length": context, "parallel": 4}}
                ],
                "max_context_length": 131072,
                "capabilities": {"reasoning": {"allowed_options": list(options)}},
            }
        ]
    }


def chat(content: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def test_exchange_backends_get_agent_defaults(tmp_path):
    found = probe(ExchangeBackend(tmp_path), test_structured=True)
    assert found == Capabilities(
        model="exchange",
        context_tokens=AGENT_CONTEXT_TOKENS,
        structured_mode=StructuredMode.PROMPT,
        note="answered through exchange files",
    )


@responses.activate
def test_gpt_oss_uses_prompt_mode_without_testing():
    responses.add(responses.GET, MODELS, json=lmstudio("openai/gpt-oss-20b", context=8192))
    found = probe(OpenAICompatBackend(BASE, "openai/gpt-oss-20b"), test_structured=True)
    assert found.structured_mode is StructuredMode.PROMPT
    assert (found.context_tokens, found.loaded) == (8192, True)
    assert found.reasoning_options == ("low", "medium", "high")
    assert "garbles schema-constrained output" in found.note
    assert not any(call.request.url == CHAT for call in responses.calls)


@responses.activate
def test_schema_mode_is_chosen_when_a_test_request_works():
    responses.add(responses.GET, MODELS, json=lmstudio("qwen3-8b"))
    responses.add(responses.POST, CHAT, json=chat('{"ok": true, "word": "scout"}'))
    found = probe(OpenAICompatBackend(BASE, "qwen3-8b"), test_structured=True)
    assert found.structured_mode is StructuredMode.SCHEMA
    assert found.context_tokens == 16384


@pytest.mark.parametrize(
    "reply",
    [
        {
            "status": 400,
            "json": {"error": "'response_format.type' must be 'json_schema' or 'text'"},
        },
        {"json": chat("sure! ok")},
    ],
)
@responses.activate
def test_prompt_mode_when_the_schema_test_fails(reply):
    responses.add(responses.GET, MODELS, json=lmstudio("qwen3-8b"))
    responses.add(responses.POST, CHAT, **reply)
    responses.add(responses.POST, CHAT, **reply)  # the repair attempt
    assert (
        probe(OpenAICompatBackend(BASE, "qwen3-8b"), test_structured=True).structured_mode
        is StructuredMode.PROMPT
    )


@responses.activate
def test_servers_without_model_details_get_a_safe_default():
    responses.add(responses.GET, MODELS, status=404)
    found = probe(OpenAICompatBackend(BASE, "llama3"))
    assert (found.context_tokens, found.loaded, found.reasoning_options) == (
        DEFAULT_CONTEXT_TOKENS,
        None,
        (),
    )
    assert "assuming 8192 tokens" in found.note


@responses.activate
def test_context_override_wins():
    responses.add(responses.GET, MODELS, json=lmstudio("qwen3-8b"))
    assert (
        probe(OpenAICompatBackend(BASE, "qwen3-8b"), context_override=4096).context_tokens == 4096
    )


def test_capabilities_round_trip():
    caps = Capabilities("m", 8192, StructuredMode.SCHEMA, ("low",), True, "note")
    assert Capabilities.from_dict(caps.to_dict()) == caps


def test_a_new_context_setting_is_not_hidden_by_the_cached_probe(tmp_path):
    settings = Settings(
        data_dir=tmp_path, reports_dir=tmp_path / "reports", llm=f"exchange:{tmp_path / 'x'}"
    )
    with App(settings) as app:
        assert app.capabilities().context_tokens == AGENT_CONTEXT_TOKENS
    with App(replace(settings, context_tokens=4096)) as app:
        assert app.capabilities().context_tokens == 4096
