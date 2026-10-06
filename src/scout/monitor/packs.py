"""Packs: ready-made watches for common needs, filled in with a few words.

A pack is a YAML file with a description, the parameters it takes (a parameter whose help starts
with "optional" may be left out) and watch templates in which ``{parameter}`` is replaced. A list
item naming a parameter that was left out is dropped: ``price below {below}`` goes away when no
limit is given. ``{slug}`` is the first parameter's value, made fit for a watch name.

Scout ships a few packs; YAML files in ``data_dir/packs`` add more (or replace them by name).
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from scout.errors import ConfigError
from scout.monitor.watches import Watch

_NOT_NAME = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    description: str
    params: dict[str, str]  # name -> help
    watches: tuple[dict[str, Any], ...]  # templates

    @property
    def required(self) -> list[str]:
        return [name for name, text in self.params.items() if not text.startswith("optional")]

    def expand(self, values: Mapping[str, str]) -> list[Watch]:
        """The pack's watches for these parameter values (checked like any watch)."""
        unknown = sorted(set(values) - set(self.params))
        if unknown:
            raise ConfigError(f"pack {self.name} has no parameter {', '.join(unknown)}")
        given = {name: value.strip() for name, value in values.items() if value.strip()}
        missing = [name for name in self.required if name not in given]
        if missing:
            raise ConfigError(f"pack {self.name} needs {', '.join(f'{m}=...' for m in missing)}")
        first = given.get(next(iter(self.params)), self.name)
        slug = _NOT_NAME.sub("-", first.lower()).strip("-")[:30].strip("-") or self.name
        return [
            Watch.from_dict(_fill(template, {**given, "slug": slug})) for template in self.watches
        ]


def available(user_dir: Path | None = None) -> dict[str, Pack]:
    """Every pack by name: the ones Scout ships, then the user's (which win on a name clash)."""
    found: dict[str, Pack] = {}
    for item in sorted(resources.files("scout.packs").iterdir(), key=lambda i: i.name):
        if item.name.endswith(".yaml"):
            name = item.name.removesuffix(".yaml")
            found[name] = _load(name, item.read_text(encoding="utf-8"), where=f"pack {name}")
    if user_dir is not None and user_dir.is_dir():
        for path in sorted(user_dir.glob("*.yaml")):
            found[path.stem] = _load(path.stem, path.read_text(encoding="utf-8"), where=str(path))
    return found


def _load(name: str, text: str, *, where: str) -> Pack:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{where} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("watches"), list):
        raise ConfigError(f"{where} needs a list of watches")
    params = data.get("params") or {}
    if not isinstance(params, dict) or not params:
        raise ConfigError(f"{where} needs params (name: help)")
    return Pack(
        name=name,
        description=str(data.get("description", "")),
        params={str(key): str(value) for key, value in params.items()},
        watches=tuple(data["watches"]),
    )


def _fill(template: Any, values: Mapping[str, str]) -> Any:
    """Replace {parameter} in every string; drop list items naming a parameter not given."""
    if isinstance(template, str):
        try:
            return template.format_map(values)
        except KeyError as exc:
            raise ConfigError(f"this pack needs {exc.args[0]}=... here: {template!r}") from exc
    if isinstance(template, list):
        return [_fill(item, values) for item in template if not _names_missing(item, values)]
    if isinstance(template, dict):
        return {key: _fill(value, values) for key, value in template.items()}
    return template


def _names_missing(item: Any, values: Mapping[str, str]) -> bool:
    if not isinstance(item, str):
        return False
    fields = {field for _, field, _, _ in string.Formatter().parse(item) if field}
    return not fields <= set(values)
