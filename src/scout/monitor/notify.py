"""Sending alerts: phone push (ntfy), chat (Telegram, Discord, Slack, Teams), email and 100+ more
through Apprise URLs, e.g. ``ntfy://my-topic`` or ``tgram://bot-token/chat-id``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from scout.errors import ConfigError, NotifyError


class Notifier(Protocol):
    def send(self, title: str, body: str) -> None: ...


class AppriseNotifier:
    def __init__(self, urls: Sequence[str]) -> None:
        try:
            import apprise
        except ImportError as exc:
            raise ConfigError("notifications need Apprise: pip install 'scout[notify]'") from exc
        self._apprise = apprise.Apprise()
        self._text = apprise.NotifyFormat.TEXT
        unknown = [url for url in urls if not self._apprise.add(url)]
        if unknown:
            raise ConfigError(f"not a notification URL Apprise understands: {', '.join(unknown)}")

    def send(self, title: str, body: str) -> None:
        if not self._apprise.notify(title=title, body=body, body_format=self._text):
            raise NotifyError("no notification service accepted the message")
