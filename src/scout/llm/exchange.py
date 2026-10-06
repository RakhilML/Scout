"""Files as a model API: an agent, a person or a recorded session answers each request.

Each request is keyed by a hash of (model, messages, schema, temperature). The reply lives in
``responses/<key>.txt``, exactly as the model would have written it.

* ``answer`` mode: a missing reply makes the backend write ``requests/<key>.md`` and raise
  ``AnswerPending``. Re-running the same command (pages and searches come from the cache, so the
  prompts are identical) picks the answer up and continues to the next step.
* ``replay`` mode: a missing reply is an error. Used by tests and offline re-runs.
* ``record`` mode: a real backend answers and every exchange is saved, turning a live session
  into a replayable fixture.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path

from scout.errors import AnswerPending, LLMError
from scout.llm.base import Backend, Completion, CompletionRequest


class ExchangeMode(StrEnum):
    ANSWER = "answer"
    REPLAY = "replay"
    RECORD = "record"


def request_key(model: str, request: CompletionRequest) -> str:
    payload = {
        "model": model,
        "messages": [[message.role, message.content] for message in request.messages],
        "schema": request.schema,
        "temperature": request.temperature,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


class ExchangeBackend:
    def __init__(
        self,
        directory: Path,
        *,
        mode: ExchangeMode = ExchangeMode.ANSWER,
        inner: Backend | None = None,
        model: str = "exchange",
    ) -> None:
        if (mode is ExchangeMode.RECORD) != (inner is not None):
            raise ValueError("an inner backend is required in record mode and only there")
        self._directory = directory
        self._mode = mode
        self._inner = inner
        self._model = model

    @property
    def model(self) -> str:
        return self._inner.model if self._inner is not None else self._model

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def inner(self) -> Backend | None:
        """The real backend being recorded, in record mode."""
        return self._inner

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()

    def complete(self, request: CompletionRequest) -> Completion:
        key = request_key(self.model, request)
        answer_path = self._directory / "responses" / f"{key}.txt"
        if answer_path.is_file():
            return Completion(
                text=answer_path.read_text(encoding="utf-8").strip(), model=self.model
            )

        request_path = self._directory / "requests" / f"{key}.md"
        if self._inner is not None:
            completion = self._inner.complete(request)
            _write(request_path, render_request(key, request, self.model))
            _write(answer_path, completion.text)
            return completion
        if self._mode is ExchangeMode.REPLAY:
            raise LLMError(f"no recorded answer for the {request.purpose} request {key}")
        _write(request_path, render_request(key, request, self.model))
        answer_path.parent.mkdir(parents=True, exist_ok=True)  # ready for the answer
        raise AnswerPending(request_path)


def render_request(key: str, request: CompletionRequest, model: str) -> str:
    """A request as Markdown that a person or an agent can read and answer."""
    lines = [
        f"# Model request {key}",
        "",
        f"- step: {request.purpose}",
        f"- model: {model}",
        f"- temperature: {request.temperature}",
        f"- answer file: responses/{key}.txt",
        "",
        "Write the model's reply, and nothing else, to the answer file. Use only the information",
        "in the messages below.",
    ]
    if request.schema is not None:
        lines += ["The reply must be one JSON object that satisfies the schema at the end."]
    for message in request.messages:
        lines += ["", f"===== {message.role.upper()} =====", message.content]
    if request.schema is not None:
        lines += ["", "===== JSON SCHEMA =====", json.dumps(request.schema, indent=2)]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
