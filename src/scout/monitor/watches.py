"""Watches: research goals, texts whose claims are fact-checked, or texts and pages whose
citations are audited, that re-run on a schedule. Kept in a YAML file you can edit by hand."""

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
from scout.monitor.rules import CITED_RULES, CLAIM_RULES, parse_rule
from scout.research import citations
from scout.research.factcheck import web_address
from scout.textutil import clean, shorten

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_INTERVAL = re.compile(r"^(\d+)\s*(m|min|minutes?|h|hr|hours?|d|days?|w|weeks?)$", re.IGNORECASE)
_INTERVAL_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_MIN_INTERVAL_MINUTES = 15  # being polite to the sites watched, and to the GPU
# A claim watch's text is never cut to fit the model's context (the smallest window holds about
# 1,965 characters of it), so the text a run checked tells whether the watch's text changed.
CHECK_TEXT_LIMIT = 1500
LABEL_CHARS = 120  # a long goal (a citation watch's text) where a watch is named or listed


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
    check: bool = False  # the goal is a text whose claims are fact-checked on every run
    cited: bool = False  # with check: the goal is a text or a page whose citations are audited

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
        kinds = [(rule, parse_rule(rule).kind) for rule in self.alerts]  # validates each rule
        dead = next((rule for rule, kind in kinds if kind == "dead"), None)
        if self.cited:
            why = _unfit_for_citations(self, kinds)
        elif dead is not None:
            why = f"only a citation watch alerts on {dead!r}"
        elif self.check:
            why = _unfit_for_claims(self, kinds)
        else:
            own = [rule for rule, kind in kinds if kind in CLAIM_RULES - {"changed"}]
            why = f"only a claim watch alerts on {own[0]!r}" if own else None
        if why is not None:
            raise ConfigError(f"watch {self.name!r}: {why}")

    @property
    def label(self) -> str:
        """The goal where alerts, feeds and lists name the watch: a text by its start, so that
        a notification shows its alerts, not the text a citation watch audits."""
        return shorten(" ".join(self.goal.split()), LABEL_CHARS)

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


def _unfit_for_claims(watch: Watch, kinds: Sequence[tuple[str, str]]) -> str | None:
    text = clean(watch.goal)
    if web_address(text):
        return "a claim watch checks a text, not a web address"
    if len(text) > CHECK_TEXT_LIMIT:
        return f"a claim watch checks at most {CHECK_TEXT_LIMIT} characters of text"
    if watch.kind is not None or watch.recency is not None:
        return "a claim watch has no kind or recency"
    other = next((rule for rule, kind in kinds if kind not in CLAIM_RULES), None)
    if other is not None:
        return f"a claim watch alerts on changed, supported or refuted, not {other!r}"
    return None


def _unfit_for_citations(watch: Watch, kinds: Sequence[tuple[str, str]]) -> str | None:
    """Why *watch* cannot audit citations. Its text is never cut (an audit reads it a part at a
    time), so it may be long; it reads only the pages its text cites, so it searches nothing."""
    if not watch.check:
        return "a citation watch needs check: true"
    if any(v is not None for v in (watch.kind, watch.recency, watch.region, watch.max_results)):
        return "a citation watch reads only the pages its text cites"
    other = next((rule for rule, kind in kinds if kind not in CITED_RULES), None)
    if other is not None:
        return f"a citation watch alerts on changed, backed, contradicted or dead, not {other!r}"
    text = clean(watch.goal)
    if not web_address(text) and not citations.cited(text).pages:
        return citations.NONE_CITED
    return None


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
    "check": bool,
    "cited": bool,
}
_TYPE_NAMES = {str: "text", int: "a whole number", list: "a list", bool: "true or false"}


def _is_a(value: Any, expected: type) -> bool:
    if isinstance(value, bool):  # a bool is also an int
        return expected is bool
    return isinstance(value, expected)


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value
