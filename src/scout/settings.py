"""Runtime settings from the environment and an optional ``.env`` file."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from dotenv import dotenv_values

from scout.errors import ConfigError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_LLM_BASE_URL = "http://localhost:1234/v1"

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class Settings:
    # Language model. `llm` picks the backend: "openai" (any OpenAI-compatible server such as
    # LM Studio or Ollama, configured by the LM_STUDIO_* variables), "exchange:DIR", "record:DIR"
    # or "replay:DIR".
    llm: str = "openai"
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 300.0
    planner_model: str = ""  # a smaller model for planning searches; the main model if empty
    model_ttl: int | None = None  # seconds a model loaded on demand stays loaded (LM Studio)
    context_tokens: int | None = None  # overrides what the server reports

    # Search and fetching. `search` is "ddgs" or "searxng:<instance URL>".
    search: str = "ddgs"
    render: str = ""  # "playwright" reads pages that need JavaScript in a headless browser
    max_results: int = 6
    max_source_chars: int = 6000
    fetch_timeout: float = 12.0
    fetch_retries: int = 1
    user_agent: str = DEFAULT_USER_AGENT
    region: str = "us-en"
    archive: str = "wayback"  # where copies of dead cited pages are looked up, or "off"

    # Storage.
    data_dir: Path = Path.home() / ".scout"
    reports_dir: Path = Path.home() / ".scout" / "reports"
    env_file: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scout.db"

    @property
    def watches_path(self) -> Path:
        return self.data_dir / "watches.yaml"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "scout.log"

    def feed_path(self, watch: str) -> Path:
        """The Atom feed of a watch's alerts, rewritten after each of its runs."""
        return self.data_dir / "feeds" / f"{watch}.xml"


def load_settings(
    *,
    cwd: Path | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Read settings; real environment variables override values from the ``.env`` file.

    The ``.env`` file is ``$SCOUT_ENV_FILE``, else ``./.env``, else ``~/.scout/.env``. Relative
    paths inside it are resolved against the file's own folder, so the result does not depend
    on where Scout is started from.
    """
    environ = os.environ if environ is None else environ
    cwd = cwd or Path.cwd()
    home = home or Path.home()

    env_file = _find_env_file(cwd, home, environ)
    values: dict[str, str] = {}
    if env_file is not None:
        values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    values.update(environ)
    base_dir = env_file.parent if env_file else cwd

    def get(key: str, parse: Callable[[str], _T], default: _T) -> _T:
        raw = values.get(key, "").strip()
        if not raw:
            return default
        try:
            return parse(raw)
        except ValueError as exc:
            raise ConfigError(f"{key}={raw!r} is not valid: {exc}") from exc

    def path(key: str, default: Path) -> Path:
        chosen = get(key, Path, default).expanduser()
        return chosen if chosen.is_absolute() else (base_dir / chosen).resolve()

    data_dir = path("SCOUT_DATA_DIR", home / ".scout")
    return Settings(
        llm=get("SCOUT_LLM", str, "openai"),
        llm_base_url=get("LM_STUDIO_BASE_URL", str, DEFAULT_LLM_BASE_URL).rstrip("/"),
        llm_model=get("LM_STUDIO_MODEL", str, ""),
        llm_api_key=get("LM_STUDIO_API_KEY", str, ""),
        llm_timeout=get("SCOUT_LLM_TIMEOUT", _positive_float, 300.0),
        planner_model=get("SCOUT_PLANNER_MODEL", str, ""),
        model_ttl=get("SCOUT_MODEL_TTL", _positive_int, None),
        context_tokens=get("SCOUT_CONTEXT_TOKENS", _positive_int, None),
        max_results=get("SCOUT_MAX_RESULTS", _positive_int, 6),
        max_source_chars=get("SCOUT_MAX_PAGE_CHARS", _positive_int, 6000),
        fetch_timeout=get("SCOUT_REQUEST_TIMEOUT", _positive_float, 12.0),
        fetch_retries=get("SCOUT_FETCH_RETRIES", _non_negative_int, 1),
        user_agent=get("SCOUT_USER_AGENT", str, DEFAULT_USER_AGENT),
        region=get("SCOUT_REGION", str, "us-en"),
        search=get("SCOUT_SEARCH", str, "ddgs"),
        render=get("SCOUT_RENDER", str, ""),
        archive=get("SCOUT_ARCHIVE", str, "wayback"),
        data_dir=data_dir,
        reports_dir=path("SCOUT_OUTPUT_DIR", data_dir / "reports"),
        env_file=env_file,
    )


def _find_env_file(cwd: Path, home: Path, environ: Mapping[str, str]) -> Path | None:
    explicit = environ.get("SCOUT_ENV_FILE", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"SCOUT_ENV_FILE points to a missing file: {candidate}")
        return candidate.resolve()
    for candidate in (cwd / ".env", home / ".scout" / ".env"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise ValueError("must be greater than zero")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise ValueError("must be greater than zero")
    return value


def _non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise ValueError("must not be negative")
    return value
