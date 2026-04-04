"""
Configuration loader for Scout.
Reads from .env in cwd, then ~/.scout/.env, then environment variables.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from exceptions import ConfigError


# ─── ENV LOADING ─────────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load .env file. Tries cwd first, then ~/.scout/.env."""
    cwd_env = Path.cwd() / ".env"
    home_env = Path.home() / ".scout" / ".env"

    if cwd_env.exists():
        load_dotenv(cwd_env, override=False)
    elif home_env.exists():
        load_dotenv(home_env, override=False)
    else:
        load_dotenv(override=False)  # tries default locations


# ─── CONFIG DATACLASS ─────────────────────────────────────────────────────────

@dataclass
class ScoutConfig:
    # LM Studio settings
    lm_studio_base_url: str          # e.g. http://localhost:1234/v1
    lm_studio_model: str             # e.g. openai/gpt-oss-120b
    lm_studio_api_key: str           # default: lm-studio
    lm_studio_timeout: int           # seconds to wait for LLM response

    # Search settings
    max_results: int                 # DDG results to fetch
    max_page_chars: int              # chars to keep per page
    fetch_timeout: int               # HTTP timeout for page fetching
    fetch_retries: int               # retry count on failed fetches
    user_agent: str                  # HTTP User-Agent for fetching

    # Output settings
    output_dir: str                  # default output folder

    # Scheduler / data settings
    data_dir: Path                   # ~/.scout by default (stores jobs.db)

    def __post_init__(self):
        # Normalize base_url — strip trailing slash
        self.lm_studio_base_url = self.lm_studio_base_url.rstrip("/")

    @property
    def lm_studio_completions_url(self) -> str:
        return f"{self.lm_studio_base_url}/chat/completions"

    @property
    def lm_studio_models_url(self) -> str:
        return f"{self.lm_studio_base_url}/models"

    def describe(self) -> str:
        lines = [
            f"  LM Studio URL : {self.lm_studio_base_url}",
            f"  Model         : {self.lm_studio_model}",
            f"  Max results   : {self.max_results}",
            f"  Max page chars: {self.max_page_chars}",
            f"  Fetch timeout : {self.fetch_timeout}s",
            f"  LLM timeout   : {self.lm_studio_timeout}s",
            f"  Data dir      : {self.data_dir}",
        ]
        return "\n".join(lines)


# ─── LOADER ──────────────────────────────────────────────────────────────────

def load_config() -> ScoutConfig:
    """Load and validate config from environment. Raises ConfigError on missing required keys."""
    _load_env()

    base_url = os.environ.get("LM_STUDIO_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise ConfigError(
            "LM_STUDIO_BASE_URL is not set.\n"
            "Create a .env file in this directory. See .env.example for reference."
        )

    model = os.environ.get("LM_STUDIO_MODEL", "").strip()
    if not model:
        raise ConfigError(
            "LM_STUDIO_MODEL is not set.\n"
            "Create a .env file in this directory. See .env.example for reference."
        )

    data_dir_str = os.environ.get("SCOUT_DATA_DIR", str(Path.home() / ".scout"))
    data_dir = Path(data_dir_str).expanduser().resolve()

    return ScoutConfig(
        lm_studio_base_url=base_url,
        lm_studio_model=model,
        lm_studio_api_key=os.environ.get("LM_STUDIO_API_KEY", "lm-studio").strip(),
        lm_studio_timeout=int(os.environ.get("SCOUT_LLM_TIMEOUT", "300")),
        max_results=int(os.environ.get("SCOUT_MAX_RESULTS", "6")),
        max_page_chars=int(os.environ.get("SCOUT_MAX_PAGE_CHARS", "4000")),
        fetch_timeout=int(os.environ.get("SCOUT_REQUEST_TIMEOUT", "12")),
        fetch_retries=int(os.environ.get("SCOUT_FETCH_RETRIES", "2")),
        user_agent=os.environ.get(
            "SCOUT_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        output_dir=os.environ.get("SCOUT_OUTPUT_DIR", "./reports").strip(),
        data_dir=data_dir,
    )


def ensure_dirs(cfg: Optional[ScoutConfig] = None) -> None:
    """Create ~/.scout/ directory and output dir if they don't exist."""
    if cfg is None:
        try:
            cfg = load_config()
        except ConfigError:
            # At least create ~/.scout
            (Path.home() / ".scout").mkdir(parents=True, exist_ok=True)
            return

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    output = Path(cfg.output_dir).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    output.mkdir(parents=True, exist_ok=True)
