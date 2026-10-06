"""Chat completions against any OpenAI-compatible server: LM Studio, Ollama, llama.cpp, vLLM."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import requests

from scout.errors import (
    ContextOverflow,
    LLMAuthError,
    LLMError,
    LLMUnavailable,
    UnsupportedResponseFormat,
)
from scout.llm.base import Completion, CompletionRequest

_CONNECT_TIMEOUT = 10.0
_INFO_TIMEOUT = 10.0
# Reasoning that some chat templates leave inline (DeepSeek, Qwen); may be unterminated if cut off.
_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)
# Wordings of "the prompt is longer than the loaded context" across LM Studio, llama.cpp, OpenAI.
_CONTEXT_OVERFLOW = re.compile(
    r"context.{0,60}(?:overflow|exceed|not enough|too long)|exceeds? the available context"
    r"|maximum context length|context_length_exceeded",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """What the server says about a model (LM Studio's native REST API exposes this)."""

    id: str
    loaded: bool
    context_length: int | None = None  # the context the model is loaded with
    max_context_length: int | None = None
    parallel: int | None = None  # concurrent predictions the loaded instance allows
    reasoning_options: tuple[str, ...] = ()  # e.g. ("low", "medium", "high"); empty = no reasoning


class OpenAICompatBackend:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout: float = 300.0,
        ttl: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        # LM Studio unloads a model it loaded on demand after this many idle seconds.
        self._ttl = ttl
        self._session = session or requests.Session()

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._session.close()

    def complete(self, request: CompletionRequest) -> Completion:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "stream": False,
        }
        if request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        if self._ttl is not None:
            payload["ttl"] = self._ttl
        if request.schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.schema,
                    "strict": True,
                },
            }
        return _completion_from(self._request("POST", "/chat/completions", json=payload))

    def list_models(self) -> list[str]:
        data = self._request("GET", "/models", timeout=_INFO_TIMEOUT)
        return [
            item["id"] for item in data.get("data", []) if isinstance(item, dict) and "id" in item
        ]

    def model_info(self) -> ModelInfo | None:
        """Details from LM Studio's ``/api/v1/models``; None on servers that don't offer it."""
        root = self._base_url.removesuffix("/v1")
        try:
            resp = self._session.get(
                f"{root}/api/v1/models", headers=self._headers(), timeout=_INFO_TIMEOUT
            )
        except requests.RequestException:
            return None
        if resp.status_code in (401, 403):
            raise LLMAuthError(_auth_message(self._base_url, resp.status_code))
        if not resp.ok:
            return None
        try:
            entries = resp.json().get("models", [])
        except (ValueError, AttributeError):
            return None
        return _find_model(entries, self._model)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _request(
        self, method: str, path: str, *, json: Any = None, timeout: float | None = None
    ) -> dict[str, Any]:
        url = self._base_url + path
        read_timeout = timeout or self._timeout
        try:
            resp = self._session.request(
                method,
                url,
                json=json,
                headers=self._headers(),
                timeout=(_CONNECT_TIMEOUT, read_timeout),
            )
        except requests.Timeout as exc:
            raise LLMError(f"no reply from {self._base_url} within {read_timeout:g}s") from exc
        except requests.ConnectionError as exc:
            raise LLMUnavailable(f"cannot reach the model server at {self._base_url}") from exc
        except requests.RequestException as exc:  # a malformed URL, a broken reply stream
            raise LLMError(f"request to {url} failed: {exc}") from exc
        if not resp.ok:
            self._raise_for(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"{url} did not return JSON") from exc
        if not isinstance(data, dict):
            raise LLMError(f"{url} returned unexpected JSON: {str(data)[:200]}")
        return data

    def _raise_for(self, resp: requests.Response) -> None:
        code = resp.status_code
        detail = _error_detail(resp)
        if code in (401, 403):
            raise LLMAuthError(_auth_message(self._base_url, code))
        if code == 503:
            raise LLMUnavailable(f"the model server has no model ready (HTTP 503): {detail}")
        if _CONTEXT_OVERFLOW.search(detail):
            raise ContextOverflow(f"the prompt does not fit the model's context window: {detail}")
        if "response_format" in detail:
            raise UnsupportedResponseFormat(f"the server rejected the output format: {detail}")
        if code == 404 or "not found" in detail.lower():
            raise LLMError(f"model {self._model!r} is not available: {detail}")
        raise LLMError(f"HTTP {code} from the model server: {detail}")


def _completion_from(data: dict[str, Any]) -> Completion:
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError(f"unexpected reply shape: {str(data)[:200]}") from exc
    content = message.get("content") or ""
    if isinstance(content, list):  # content given as typed parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    reasoning = message.get("reasoning_content") or message.get("reasoning") or None
    return Completion(
        text=_THINK_BLOCK.sub("", content).strip(),
        reasoning=reasoning,
        finish_reason=choice.get("finish_reason"),
        model=data.get("model"),
    )


def _find_model(entries: Any, model: str) -> ModelInfo | None:
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        instances = [i for i in entry.get("loaded_instances") or [] if isinstance(i, dict)]
        ids = {entry.get("key"), *(instance.get("id") for instance in instances)}
        if model not in ids:
            continue
        config = instances[0].get("config", {}) if instances else {}
        reasoning = (entry.get("capabilities") or {}).get("reasoning") or {}
        options = reasoning.get("allowed_options", []) if isinstance(reasoning, dict) else []
        return ModelInfo(
            id=model,
            loaded=bool(instances),
            context_length=_int_or_none(config.get("context_length")),
            max_context_length=_int_or_none(entry.get("max_context_length")),
            parallel=_int_or_none(config.get("parallel")),
            reasoning_options=tuple(str(option) for option in options),
        )
    return None


def _error_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason or ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or data)[:300]


def _auth_message(base_url: str, code: int) -> str:
    return (
        f"{base_url} rejected the request's credentials (HTTP {code}). If LM Studio has "
        "'Require Authentication' on, create a token under Developer > Server Settings "
        "and set LM_STUDIO_API_KEY to it."
    )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
