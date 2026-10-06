"""Different models for different steps: a small, fast one can plan while a stronger one reads."""

from __future__ import annotations

from collections.abc import Mapping

from scout.llm.base import Backend, Completion, CompletionRequest


class RoutedBackend:
    """Sends each request to the backend for its purpose; everything else to the default one.
    Results name the default model: it is the one that reads the pages."""

    def __init__(self, default: Backend, routes: Mapping[str, Backend]) -> None:
        self._default = default
        self._routes = dict(routes)

    @property
    def model(self) -> str:
        return self._default.model

    @property
    def inner(self) -> Backend:
        return self._default

    def complete(self, request: CompletionRequest) -> Completion:
        return self._routes.get(request.purpose, self._default).complete(request)

    def close(self) -> None:
        for backend in {id(b): b for b in (self._default, *self._routes.values())}.values():
            backend.close()
