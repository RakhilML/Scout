"""What a model can do: its context window, reasoning levels, and which structured mode works."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from scout.errors import StructuredOutputError, UnsupportedResponseFormat
from scout.llm import model_server
from scout.llm.base import Backend, Message
from scout.llm.structured import StructuredMode, generate

# LM Studio's context length when it loads a model on demand (0.4.16 and later; 4096 before).
DEFAULT_CONTEXT_TOKENS = 8192
# Context assumed for backends that are not model servers (an agent answering exchange files).
AGENT_CONTEXT_TOKENS = 32768
# Model families whose schema-constrained output LM Studio is known to garble (open bug reports
# about gpt-oss's Harmony tokens); they always use prompt mode.
_SCHEMA_TROUBLE = ("gpt-oss",)


@dataclass(frozen=True, slots=True)
class Capabilities:
    model: str
    context_tokens: int
    structured_mode: StructuredMode
    reasoning_options: tuple[str, ...] = ()
    loaded: bool | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "context_tokens": self.context_tokens,
            "structured_mode": self.structured_mode.value,
            "reasoning_options": list(self.reasoning_options),
            "loaded": self.loaded,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capabilities:
        return cls(
            model=data["model"],
            context_tokens=data["context_tokens"],
            structured_mode=StructuredMode(data["structured_mode"]),
            reasoning_options=tuple(data.get("reasoning_options", [])),
            loaded=data.get("loaded"),
            note=data.get("note", ""),
        )


class _Ping(BaseModel):
    ok: bool
    word: str


def probe(
    backend: Backend, *, context_override: int | None = None, test_structured: bool = False
) -> Capabilities:
    """Ask the server about the model; optionally try one tiny schema-constrained request."""
    server = model_server(backend)
    if server is None:
        return Capabilities(
            model=backend.model,
            context_tokens=context_override or AGENT_CONTEXT_TOKENS,
            structured_mode=StructuredMode.PROMPT,
            note="answered through exchange files",
        )

    info = server.model_info()
    notes = []
    if context_override:
        context, notes = context_override, ["context from SCOUT_CONTEXT_TOKENS"]
    elif info is not None and info.context_length:
        context = info.context_length
    else:
        context = DEFAULT_CONTEXT_TOKENS
        notes.append(f"context not reported; assuming {DEFAULT_CONTEXT_TOKENS} tokens")

    mode = StructuredMode.PROMPT
    if any(family in server.model.lower() for family in _SCHEMA_TROUBLE):
        notes.append("prompt mode: this model family garbles schema-constrained output")
    elif test_structured:
        mode = _schema_mode_if_it_works(server)
        notes.append(f"{mode.value} mode (tested)")

    return Capabilities(
        model=server.model,
        context_tokens=context,
        structured_mode=mode,
        reasoning_options=info.reasoning_options if info is not None else (),
        loaded=info.loaded if info is not None else None,
        note="; ".join(notes),
    )


def _schema_mode_if_it_works(backend: Backend) -> StructuredMode:
    messages = [Message("user", 'Reply with ok set to true and word set to "scout".')]
    try:
        ping = generate(
            backend, messages, _Ping, mode=StructuredMode.SCHEMA, purpose="probe", temperature=0.0
        )
    except (UnsupportedResponseFormat, StructuredOutputError):
        return StructuredMode.PROMPT
    return (
        StructuredMode.SCHEMA
        if ping.ok and ping.word.strip().lower() == "scout"
        else StructuredMode.PROMPT
    )
