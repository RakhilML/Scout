"""The language-model interface that every backend implements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    messages: tuple[Message, ...]
    purpose: str = "general"  # which pipeline step is asking; used for logs and routing
    schema: dict[str, Any] | None = None  # ask the server to constrain output to this JSON schema
    schema_name: str = "response"
    temperature: float = 0.2
    reasoning_effort: str | None = None  # "low" | "medium" | "high", for models that reason


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    reasoning: str | None = None
    finish_reason: str | None = None
    model: str | None = None


class Backend(Protocol):
    @property
    def model(self) -> str: ...

    def complete(self, request: CompletionRequest) -> Completion: ...

    def close(self) -> None: ...
