"""Language-model backends and structured generation."""

from __future__ import annotations

from pathlib import Path

from scout.errors import ConfigError
from scout.llm.base import Backend
from scout.llm.exchange import ExchangeBackend, ExchangeMode
from scout.llm.openai_compat import OpenAICompatBackend
from scout.llm.routing import RoutedBackend
from scout.settings import Settings


def make_backend(settings: Settings) -> Backend:
    """Build the backend named by ``SCOUT_LLM``: openai | exchange:DIR | replay:DIR | record:DIR."""
    kind, _, argument = settings.llm.partition(":")
    kind = kind.strip().lower()
    if kind == "openai":
        return _served(settings)
    if kind in ("exchange", "replay", "record"):
        if not argument.strip():
            raise ConfigError(f"SCOUT_LLM={settings.llm!r} needs a folder, e.g. {kind}:./exchange")
        directory = Path(argument.strip()).expanduser().resolve()
        if kind == "record":
            return ExchangeBackend(directory, mode=ExchangeMode.RECORD, inner=_served(settings))
        mode = ExchangeMode.ANSWER if kind == "exchange" else ExchangeMode.REPLAY
        return ExchangeBackend(directory, mode=mode)
    raise ConfigError(
        f"unknown SCOUT_LLM value {settings.llm!r}: "
        "use openai, exchange:DIR, replay:DIR or record:DIR"
    )


def model_server(backend: Backend) -> OpenAICompatBackend | None:
    """The model server behind *backend*, through any wrappers; None when there is none."""
    if isinstance(backend, ExchangeBackend | RoutedBackend):
        return model_server(backend.inner) if backend.inner is not None else None
    return backend if isinstance(backend, OpenAICompatBackend) else None


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
    return OpenAICompatBackend(
        settings.llm_base_url,
        model,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
        ttl=settings.model_ttl,
    )
