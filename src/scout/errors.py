"""Exceptions Scout raises on purpose. Anything else escaping to the CLI is a bug."""

from __future__ import annotations

from pathlib import Path


class ScoutError(Exception):
    """Base class for expected failures that are reported without a traceback."""


class ConfigError(ScoutError):
    """Settings are missing or invalid."""


class SearchError(ScoutError):
    """No search backend produced results."""


class ExtractionError(ScoutError):
    """Downloaded content could not be turned into text."""


class RenderError(ExtractionError):
    """A headless browser could not render a page."""


class NotifyError(ScoutError):
    """A notification could not be delivered."""


class LLMError(ScoutError):
    """The language-model call failed."""


class LLMUnavailable(LLMError):
    """The model server is unreachable or has no model loaded."""


class LLMAuthError(LLMError):
    """The model server rejected the request's credentials (HTTP 401/403)."""


class ContextOverflow(LLMError):
    """The prompt does not fit the model's loaded context window."""


class UnsupportedResponseFormat(LLMError):
    """The server rejected the requested structured-output format."""


class StructuredOutputError(LLMError):
    """The model's reply could not be parsed or validated against the schema."""


class AnswerPending(LLMError):
    """An exchange backend wrote a request and is waiting for someone to answer it."""

    def __init__(self, request_path: Path) -> None:
        super().__init__(f"waiting for an answer to {request_path}")
        self.request_path = request_path
