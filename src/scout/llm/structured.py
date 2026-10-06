"""Validated structured output from any backend, with one repair attempt.

Two modes, chosen per model by the capability probe:

* ``json_schema``: the server constrains decoding to the schema (fast and reliable where it works);
* ``prompt``: the schema goes into the prompt and the reply is parsed and validated here. This is
  the safe default: some servers garble schema-constrained output of reasoning models such as
  gpt-oss.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from scout.errors import StructuredOutputError
from scout.llm.base import Backend, Completion, CompletionRequest, Message

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredMode(StrEnum):
    SCHEMA = "json_schema"
    PROMPT = "prompt"


class _InvalidReply(ValueError):
    pass


def generate(
    backend: Backend,
    messages: Sequence[Message],
    output: type[T],
    *,
    mode: StructuredMode,
    purpose: str,
    temperature: float = 0.2,
    reasoning_effort: str | None = None,
) -> T:
    """Ask for an instance of *output*; retry once with the validation error if the reply is bad."""
    schema = output.model_json_schema()
    request = CompletionRequest(
        messages=tuple(
            messages if mode is StructuredMode.SCHEMA else _describe_schema(messages, schema)
        ),
        purpose=purpose,
        schema=schema if mode is StructuredMode.SCHEMA else None,
        schema_name=output.__name__,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )
    completion = backend.complete(request)
    try:
        return _parse(completion, output)
    except _InvalidReply as exc:
        problem = str(exc)
    log.info("%s reply was invalid (%s); asking the model to fix it", purpose, problem)

    repair = replace(
        request,
        messages=(
            *request.messages,
            Message("assistant", completion.text),
            Message(
                "user",
                f"That reply was not valid: {problem}. "
                "Reply again with only the corrected JSON object.",
            ),
        ),
    )
    try:
        return _parse(backend.complete(repair), output)
    except _InvalidReply as exc:
        raise StructuredOutputError(f"{purpose}: the model's reply was not valid ({exc})") from None


def extract_json_object(text: str) -> str | None:
    """The first balanced ``{...}`` in *text*, ignoring braces inside JSON strings."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _describe_schema(messages: Sequence[Message], schema: dict) -> list[Message]:
    instruction = (
        "Reply with a single JSON object and nothing else (no prose, no code fences). "
        "It must match this JSON schema:\n" + json.dumps(schema, separators=(",", ":"))
    )
    if messages and messages[0].role == "system":
        first = messages[0]
        return [Message("system", f"{first.content}\n\n{instruction}"), *messages[1:]]
    return [Message("system", instruction), *messages]


def _parse(completion: Completion, output: type[T]) -> T:
    # Some servers put a reasoning model's whole answer in the reasoning field; look there last.
    candidates = (completion.text, completion.reasoning or "")
    raw = next((found for text in candidates if (found := extract_json_object(text))), None)
    if raw is None:
        raise _InvalidReply("it contained no JSON object")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _InvalidReply(f"the JSON is malformed ({exc.msg} at character {exc.pos})") from exc
    try:
        return output.model_validate(data)
    except ValidationError as exc:
        raise _InvalidReply(_summarize(exc)) from exc


def _summarize(error: ValidationError, limit: int = 3) -> str:
    problems = [
        f"{'.'.join(str(part) for part in item['loc']) or 'value'}: {item['msg']}"
        for item in error.errors()[:limit]
    ]
    more = error.error_count() - limit
    return "; ".join(problems) + (f" (and {more} more)" if more > 0 else "")
