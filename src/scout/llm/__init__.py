"""Language-model backends and structured generation."""

from __future__ import annotations

from pathlib import Path

from scout.errors import ConfigError
from scout.llm.base import Backend
from scout.llm.exchange import ExchangeBackend, ExchangeMode
from scout.llm.openai_compat import OpenAICompatBackend
from scout.llm.routing import RoutedBackend
from scout.settings import Settings

_KINDS = ("openai", "exchange", "replay", "record")


def make_backend(settings: Settings) -> Backend:
    """Build the backend named by ``SCOUT_LLM``: openai | exchange:DIR | replay:DIR | record:DIR."""
    kind = _kind(settings)
    if kind == "openai":
        return _served(settings)
    argument = settings.llm.partition(":")[2].strip()
    if not argument:
        raise ConfigError(f"SCOUT_LLM={settings.llm!r} needs a folder, e.g. {kind}:./exchange")
    directory = Path(argument).expanduser().resolve()
    if kind == "record":
        return ExchangeBackend(directory, mode=ExchangeMode.RECORD, inner=_served(settings))
    mode = ExchangeMode.ANSWER if kind == "exchange" else ExchangeMode.REPLAY
    return ExchangeBackend(directory, mode=mode)


def named_server(settings: Settings) -> OpenAICompatBackend | None:
    """The model server that ``SCOUT_LLM`` sends requests to (openai, record:DIR), to ask what
    it offers: that needs no model id. None for a backend that asks no server."""
    if _kind(settings) not in ("openai", "record"):
        return None
    return _server(settings, settings.llm_model)


def model_server(backend: Backend) -> OpenAICompatBackend | None:
    """The model server behind *backend*, through any wrappers; None when there is none."""
    if isinstance(backend, ExchangeBackend | RoutedBackend):
        return model_server(backend.inner) if backend.inner is not None else None
    return backend if isinstance(backend, OpenAICompatBackend) else None


def _kind(settings: Settings) -> str:
    kind = settings.llm.partition(":")[0].strip().lower()
    if kind not in _KINDS:
        raise ConfigError(
            f"unknown SCOUT_LLM value {settings.llm!r}: "
            "use openai, exchange:DIR, replay:DIR or record:DIR"
        )
    return kind


def _served(settings: Settings) -> Backend:
    """The model server's backend, routing planning to SCOUT_PLANNER_MODEL when it is set."""
    reader = _openai(settings, settings.llm_model)
    if not settings.planner_model or settings.planner_model == settings.llm_model:
        return reader
    return RoutedBackend(reader, {"plan": _openai(settings, settings.planner_model)})


def _openai(settings: Settings, model: str) -> OpenAICompatBackend:
    if not model:
        raise ConfigError(
            "LM_STUDIO_MODEL is not set; run `scout models` to see what the server offers"
        )
    return _server(settings, model)


def _server(settings: Settings, model: str) -> OpenAICompatBackend:
    return OpenAICompatBackend(
        settings.llm_base_url,
        model,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
        ttl=settings.model_ttl,
    )
