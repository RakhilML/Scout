"""The current time, in UTC: the default clock of everything that tests run on a fake one."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
