"""
Custom exceptions for Scout.
"""


class ScoutError(Exception):
    """Base exception for all Scout errors."""


class ConfigError(ScoutError):
    """Raised when .env config is missing or invalid."""


class SearchError(ScoutError):
    """Raised when DuckDuckGo search fails completely."""


class LLMError(ScoutError):
    """Raised when the LLM call fails after all retries."""


class LLMConnectionError(LLMError):
    """Cannot reach LM Studio endpoint."""


class LLMEmptyResponseError(LLMError):
    """LLM returned empty content and empty reasoning."""


class SchedulerError(ScoutError):
    """Raised for job management failures."""


class JobNotFoundError(SchedulerError):
    """Job ID not found."""


class ReporterError(ScoutError):
    """Raised when saving a report fails."""
