"""Files as a model API: an agent, a person or a recorded session answers each request.

Each request is keyed by a hash of (model, messages, schema, temperature). The reply lives in
``responses/<key>.txt``, exactly as the model would have written it.

* ``answer`` mode: a missing reply makes the backend write ``requests/<key>.md`` and raise
  ``AnswerPending``. Re-running the same command (pages and searches come from the cache, so the
  prompts are identical) picks the answer up and continues to the next step.
* ``replay`` mode: a missing reply is an error. Used by tests and offline re-runs.
* ``record`` mode: a real backend answers and every exchange is saved, turning a live session
  into a replayable fixture. With the reply goes its reasoning (``<key>.reasoning.txt``: some
  servers put a reasoning model's whole answer there); in its place, the error the server gave
  (``<key>.error.json``), which a run may go on from (a smaller prompt, prompt mode, a deep run
  that stops looking). The other modes raise a recorded error again, so a replay takes the
  same turns; record mode asks the server again.

A request depends on more than the question: the context window and the page size set how much
page text it holds, the structured mode puts the schema in the request (json_schema) or in the
messages (prompt), and the prompts give today's date. So record mode also keeps
``session.json``: the model name, context window, page size and reasoning levels the requests
were built with, the structured modes they used, and the day of the session. It is written with
the first request recorded. In a folder that has it, the other modes key requests by the
recorded model name, App builds its runs with the recorded values and the session's day, so a
replay on any later day builds the very requests that were recorded. Without it, the model name
is "exchange".
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from scout.errors import AnswerPending, ConfigError, LLMError, UnsupportedResponseFormat
from scout.files import FileLock, write_atomic
from scout.llm.base import Backend, Completion, CompletionRequest
from scout.llm.structured import StructuredMode

SESSION_FILE = "session.json"


class ExchangeMode(StrEnum):
    ANSWER = "answer"
    REPLAY = "replay"
    RECORD = "record"


@dataclass(frozen=True, slots=True)
class Session:
    """What the recorded requests in a folder were built with."""

    model: str
    context_tokens: int | None = None  # None: recorded without a capabilities probe
    max_page_chars: int | None = None  # None: not recorded; the settings' value applies
    structured_modes: tuple[StructuredMode, ...] = ()  # of the recorded requests
    reasoning_options: tuple[str, ...] = ()
    today: date | None = None  # the day its runs are analyzed for; None: each run's own

    @property
    def structured_mode(self) -> StructuredMode:
        """The mode a replay starts in: json_schema if any recorded request used it. A run
        falls back from it to prompt mode, never the other way."""
        if StructuredMode.SCHEMA in self.structured_modes:
            return StructuredMode.SCHEMA
        return StructuredMode.PROMPT

    def builds_like(self, other: Session) -> bool:
        """Whether *other*'s requests are built as this session's are: for the same model,
        context window and page size. Others do not replay with these."""
        return (self.model, self.context_tokens, self.max_page_chars) == (
            other.model,
            other.context_tokens,
            other.max_page_chars,
        )

    def using(self, *modes: StructuredMode) -> Session:
        used = {*modes, *self.structured_modes}
        return replace(self, structured_modes=tuple(m for m in StructuredMode if m in used))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "context_tokens": self.context_tokens,
            "max_page_chars": self.max_page_chars,
            "structured_modes": [mode.value for mode in self.structured_modes],
            "reasoning_options": list(self.reasoning_options),
            "today": self.today.isoformat() if self.today is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        context, chars, today = (data.get(k) for k in ("context_tokens", "max_page_chars", "today"))
        return cls(
            model=str(data["model"]),
            context_tokens=None if context is None else int(context),
            max_page_chars=None if chars is None else int(chars),
            structured_modes=tuple(StructuredMode(m) for m in data.get("structured_modes", [])),
            reasoning_options=tuple(str(option) for option in data.get("reasoning_options", [])),
            today=None if today is None else date.fromisoformat(today),
        )


def read_session(directory: Path) -> Session | None:
    """The session file of an exchange folder; None when it has none."""
    path = directory / SESSION_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return Session.from_dict(json.loads(text))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f"{path} is not a readable session file: {exc}") from exc


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
        model: str | None = None,
    ) -> None:
        if (mode is ExchangeMode.RECORD) != (inner is not None):
            raise ValueError("an inner backend is required in record mode and only there")
        self._directory = directory
        self._mode = mode
        self._inner = inner
        on_file = read_session(directory)
        # A recorder's session is the one it records into, chosen by keep_session().
        self._session = on_file if inner is None else None
        self._model = model or (on_file.model if on_file is not None else "exchange")
        self._lock = threading.Lock()

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

    @property
    def session(self) -> Session | None:
        """What the requests in the folder were recorded with, or (recording) will be; None
        when nothing says."""
        return self._session

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()

    def keep_session(
        self,
        *,
        context_tokens: int,
        reasoning_options: tuple[str, ...],
        today: date,
        max_page_chars: int | None = None,
    ) -> None:
        """Record mode: note what the next requests are built with. Nothing is written until
        a request is recorded: a probe alone (scout check) leaves the folder as it was.

        The day is chosen once and then kept, because runs ask with it: the day of a session on
        file that recorded requests built the same way (it goes on), else *today*. A change of
        model, context window or page size starts a session anew."""
        ours = Session(
            self.model,
            context_tokens=context_tokens,
            max_page_chars=max_page_chars,
            reasoning_options=tuple(reasoning_options),
            today=today,
        )
        with self._lock:
            kept = self._session
            if kept is not None and kept.builds_like(ours):
                ours = replace(
                    ours, structured_modes=kept.structured_modes, today=kept.today or today
                )
            elif (theirs := _continued(read_session(self._directory), ours)) is not None:
                ours = replace(
                    ours, structured_modes=theirs.structured_modes, today=theirs.today or today
                )
            self._session = ours

    def complete(self, request: CompletionRequest) -> Completion:
        key = request_key(self.model, request)
        responses = self._directory / "responses"
        # A recorder asks again for a request that failed: the error may be gone.
        answered = _answered(responses, key, self.model, errors=self._inner is None)
        if answered is not None:
            return answered

        if self._inner is not None:
            try:
                completion = self._inner.complete(request)
            except AnswerPending:
                raise
            except LLMError as exc:
                self._record(key, request, exc)
                raise
            self._record(key, request, completion)
            return completion
        if request.schema is not None and self._also_prompted():
            # A run recorded after another fell back starts in prompt mode, and asked nothing
            # in json_schema mode: falling back here too leads to the answers it recorded.
            raise UnsupportedResponseFormat(
                f"no recorded json_schema answer for the {request.purpose} request {key}; "
                "the recorded session also asked in prompt mode"
            )
        if self._mode is ExchangeMode.REPLAY:
            raise LLMError(f"no recorded answer for the {request.purpose} request {key}")
        request_path = self._directory / "requests" / f"{key}.md"
        write_atomic(request_path, render_request(key, request, self.model))
        responses.mkdir(parents=True, exist_ok=True)  # ready for the answer
        raise AnswerPending(request_path)

    def _record(self, key: str, request: CompletionRequest, outcome: Completion | LLMError) -> None:
        # The mode is noted first: a noted mode with no answer does a replay no harm, but an
        # answer whose mode is not noted could be out of its reach.
        self._note_mode(request)
        responses = self._directory / "responses"
        write_atomic(
            self._directory / "requests" / f"{key}.md", render_request(key, request, self.model)
        )
        error = responses / f"{key}.error.json"
        if isinstance(outcome, LLMError):
            record = {"error": type(outcome).__name__, "message": str(outcome)}
            write_atomic(error, json.dumps(record, indent=2) + "\n")
            return
        reasoning = responses / f"{key}.reasoning.txt"
        if outcome.reasoning:
            write_atomic(reasoning, outcome.reasoning)
        else:
            reasoning.unlink(missing_ok=True)  # left by a recording cut short
        write_atomic(responses / f"{key}.txt", outcome.text)  # it marks the answer
        error.unlink(missing_ok=True)  # the server answers now

    def _also_prompted(self) -> bool:
        return self._session is not None and StructuredMode.PROMPT in self._session.structured_modes

    def _note_mode(self, request: CompletionRequest) -> None:
        """Add the request's mode to the session file. Another process (the daemon, a command)
        may record into the folder too: the file is read again and merged under a lock. A
        session there of another day cannot take this recorder's requests: they give its day."""
        # Scout asks only for structured replies (llm/structured.py): no schema is prompt mode.
        mode = StructuredMode.SCHEMA if request.schema is not None else StructuredMode.PROMPT
        path = self._directory / SESSION_FILE
        with (
            self._lock,
            FileLock(path.with_name(f"{SESSION_FILE}.lock"), busy="the session file is in use"),
        ):
            ours = self._session
            if ours is None or ours.model != self.model:
                ours = Session(self.model)
            on_file = read_session(self._directory)
            theirs = _continued(on_file, ours)
            if theirs is not None:
                if ours.today is None:
                    ours = replace(ours, today=theirs.today)
                elif theirs.today not in (None, ours.today):
                    raise ConfigError(
                        f"the session in {self._directory} was recorded on {theirs.today}, and "
                        f"this run asks with {ours.today}: record into a new folder"
                    )
                ours = ours.using(*theirs.structured_modes)
            session = ours.using(mode)
            if session != on_file:
                write_atomic(path, json.dumps(session.to_dict(), indent=2) + "\n")
            self._session = session


def _continued(on_file: Session | None, ours: Session) -> Session | None:
    """The session on file, if it recorded requests built as *ours* are. One that recorded
    nothing (a probe, by an earlier version) or was built otherwise does not bind ours."""
    if on_file is None or not on_file.structured_modes or not on_file.builds_like(ours):
        return None
    return on_file


def _answered(responses: Path, key: str, model: str, *, errors: bool) -> Completion | None:
    """The answer to request *key* in *responses*, exactly as recorded, with its reasoning;
    None when there is none. With *errors*, a recorded error is raised again."""
    answer = responses / f"{key}.txt"
    if answer.is_file():
        reasoning = responses / f"{key}.reasoning.txt"
        return Completion(
            # Exactly: a repair request holds the reply, and a CR in it changes the key.
            text=_exact(answer).strip(),
            reasoning=_exact(reasoning) if reasoning.is_file() else None,
            model=model,
        )
    error = responses / f"{key}.error.json"
    if errors and error.is_file():
        raise _recorded_error(error)
    return None


def _exact(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as file:
        return file.read()


def _recorded_error(path: Path) -> LLMError:
    kinds = _error_kinds()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return kinds[data["error"]](str(data["message"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"{path} is not a readable error record: {exc}") from exc


def _error_kinds() -> dict[str, type[LLMError]]:
    """Every model error by its name, but AnswerPending: a server never gives that one."""
    kinds: dict[str, type[LLMError]] = {}
    found: list[type[LLMError]] = [LLMError]
    while found:
        kind = found.pop()
        if kind is not AnswerPending:
            kinds[kind.__name__] = kind
        found.extend(kind.__subclasses__())
    return kinds


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
