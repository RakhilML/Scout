"""Watches: research goals that re-run on a schedule. Kept in a YAML file you can edit by hand."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

import yaml
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from scout.errors import ConfigError
from scout.files import FileLock, write_atomic
from scout.monitor.rules import parse_rule

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_INTERVAL = re.compile(r"^(\d+)\s*(m|min|minutes?|h|hr|hours?|d|days?|w|weeks?)$", re.IGNORECASE)
_INTERVAL_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_MIN_INTERVAL_MINUTES = 15  # being polite to the sites watched, and to the GPU


@dataclass(frozen=True, slots=True)
class Watch:
    name: str
    goal: str
    every: str | None = None  # "6h", "30m", "1d" ...
    cron: str | None = None  # or a cron expression, "0 9 * * *"
    kind: str | None = None
    recency: str | None = None
    region: str | None = None
    max_results: int | None = None
    alerts: tuple[str, ...] = ()  # rules such as "price below 1800 USD"
    notify: tuple[str, ...] = ()  # Apprise URLs such as "ntfy://my-topic"
    stop_when_alerted: bool = False
    paused: bool = False

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ConfigError(
                f"watch name {self.name!r}: use 1-40 lowercase letters, digits, dashes"
            )
        if not self.goal.strip():
            raise ConfigError(f"watch {self.name!r} has no goal")
        if (self.every is None) == (self.cron is None):
            raise ConfigError(f"watch {self.name!r} needs exactly one of 'every' or 'cron'")
        trigger(self)  # validates the schedule
        for rule in self.alerts:
            parse_rule(rule)  # validates each rule

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["alerts"], data["notify"] = list(self.alerts), list(self.notify)
        defaults = {f.name: f.default for f in fields(self)}
        return {k: v for k, v in data.items() if k in ("name", "goal") or v != _plain(defaults[k])}

    @classmethod
    def from_dict(cls, data: Any) -> Watch:
        if not isinstance(data, dict):
            raise ConfigError(f"each watch must be a mapping of settings, not {data!r}")
        name = data.get("name")
        unknown = set(data) - set(_TYPES)
        if unknown:
            raise ConfigError(f"watch {name!r}: unknown setting(s) {sorted(unknown)}")
        for key, value in data.items():
            if value is not None and not _is_a(value, _TYPES[key]):
                raise ConfigError(f"watch {name!r}: {key} must be {_TYPE_NAMES[_TYPES[key]]}")
        values = dict(data)
        for key in ("alerts", "notify"):
            items = values.get(key) or []
            if not all(isinstance(item, str) for item in items):
                raise ConfigError(f"watch {name!r}: {key} must be a list of text")
            values[key] = tuple(items)
        return cls(**values)


def trigger(watch: Watch) -> BaseTrigger:
    """The APScheduler trigger for a watch's schedule."""
    if watch.cron is not None:
        try:
            return CronTrigger.from_crontab(watch.cron)
        except ValueError as exc:
            raise ConfigError(f"watch {watch.name!r}: bad cron {watch.cron!r} ({exc})") from exc
    match = _INTERVAL.match((watch.every or "").strip())
    if match is None:
        raise ConfigError(f"watch {watch.name!r}: 'every' must look like 30m, 6h, 1d or 1w")
    amount, unit = int(match[1]), _INTERVAL_UNITS[match[2][0].lower()]
    if unit == "minutes" and amount < _MIN_INTERVAL_MINUTES:
        raise ConfigError(
            f"watch {watch.name!r}: run at most every {_MIN_INTERVAL_MINUTES} minutes"
        )
    if amount <= 0:
        raise ConfigError(f"watch {watch.name!r}: 'every' must be positive")
    return IntervalTrigger(**{unit: amount})


class WatchBook:
    """The watches file: a list under the ``watches`` key."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[Watch]:
        if not self.path.exists():
            return []
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{self.path} is not valid YAML: {exc}") from exc
        entries = data.get("watches", []) if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ConfigError(f"{self.path} must contain a list under 'watches'")
        watches = [Watch.from_dict(entry) for entry in entries]
        names = [watch.name for watch in watches]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ConfigError(f"{self.path}: duplicate watch name(s) {duplicates}")
        return watches

    def save(self, watches: Sequence[Watch]) -> None:
        text = yaml.safe_dump(
            {"watches": [watch.to_dict() for watch in watches]},
            sort_keys=False,
            allow_unicode=True,
        )
        write_atomic(self.path, text)

    def get(self, name: str) -> Watch:
        for watch in self.load():
            if watch.name == name:
                return watch
        raise ConfigError(f"no watch named {name!r}")

    def add(self, watch: Watch) -> None:
        with self._editing():
            watches = self.load()
            if any(existing.name == watch.name for existing in watches):
                raise ConfigError(f"a watch named {watch.name!r} already exists")
            self.save([*watches, watch])

    def update(self, name: str, **changes: Any) -> Watch:
        with self._editing():
            watches = self.load()
            for index, watch in enumerate(watches):
                if watch.name == name:
                    watches[index] = replace(watch, **changes)
                    self.save(watches)
                    return watches[index]
        raise ConfigError(f"no watch named {name!r}")

    def remove(self, name: str) -> None:
        with self._editing():
            watches = self.load()
            kept = [watch for watch in watches if watch.name != name]
            if len(kept) == len(watches):
                raise ConfigError(f"no watch named {name!r}")
            self.save(kept)

    def _editing(self) -> FileLock:
        return FileLock(self.path.with_name(f"{self.path.name}.lock"), busy="watches file busy")


_TYPES: dict[str, type] = {
    "name": str,
    "goal": str,
    "every": str,
    "cron": str,
    "kind": str,
    "recency": str,
    "region": str,
    "max_results": int,
    "alerts": list,
    "notify": list,
    "stop_when_alerted": bool,
    "paused": bool,
}
_TYPE_NAMES = {str: "text", int: "a whole number", list: "a list", bool: "true or false"}


def _is_a(value: Any, expected: type) -> bool:
    if isinstance(value, bool):  # a bool is also an int
        return expected is bool
    return isinstance(value, expected)


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value
