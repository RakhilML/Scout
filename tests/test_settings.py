from pathlib import Path

import pytest

from scout.errors import ConfigError
from scout.settings import DEFAULT_LLM_BASE_URL, load_settings


@pytest.fixture
def places(tmp_path):
    cwd, home = tmp_path / "work", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    return cwd, home


def test_defaults_without_any_configuration(places):
    cwd, home = places
    settings = load_settings(cwd=cwd, home=home, environ={})
    assert settings.env_file is None
    assert (settings.llm, settings.llm_base_url, settings.llm_model) == (
        "openai",
        DEFAULT_LLM_BASE_URL,
        "",
    )
    assert settings.data_dir == home / ".scout"
    assert settings.reports_dir == home / ".scout" / "reports"
    assert settings.db_path == home / ".scout" / "scout.db"


def test_env_file_values_and_environment_precedence(places):
    cwd, home = places
    (cwd / ".env").write_text(
        "LM_STUDIO_BASE_URL=http://gpu-box:1234/v1/\nLM_STUDIO_MODEL=openai/gpt-oss-20b\n"
        "SCOUT_MAX_RESULTS=8\nSCOUT_REGION=in-en\n",
        encoding="utf-8",
    )
    settings = load_settings(cwd=cwd, home=home, environ={"SCOUT_MAX_RESULTS": "3"})
    assert settings.env_file == (cwd / ".env").resolve()
    assert settings.llm_base_url == "http://gpu-box:1234/v1"
    assert settings.llm_model == "openai/gpt-oss-20b"
    assert settings.max_results == 3  # the environment wins over the file
    assert settings.region == "in-en"


def test_relative_paths_resolve_against_the_env_file(places):
    cwd, home = places
    config = home / "config"
    config.mkdir()
    (config / "scout.env").write_text(
        "SCOUT_DATA_DIR=data\nSCOUT_OUTPUT_DIR=./out\n", encoding="utf-8"
    )
    settings = load_settings(
        cwd=cwd, home=home, environ={"SCOUT_ENV_FILE": str(config / "scout.env")}
    )
    assert settings.data_dir == (config / "data").resolve()
    assert settings.reports_dir == (config / "out").resolve()


def test_home_env_file_is_the_fallback(places):
    cwd, home = places
    (home / ".scout").mkdir()
    (home / ".scout" / ".env").write_text("LM_STUDIO_MODEL=from-home\n", encoding="utf-8")
    assert load_settings(cwd=cwd, home=home, environ={}).llm_model == "from-home"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("SCOUT_MAX_RESULTS", "six", "SCOUT_MAX_RESULTS"),
        ("SCOUT_MAX_RESULTS", "0", "greater than zero"),
        ("SCOUT_FETCH_RETRIES", "-1", "must not be negative"),
        ("SCOUT_LLM_TIMEOUT", "fast", "SCOUT_LLM_TIMEOUT"),
    ],
)
def test_invalid_values_are_reported_by_name(places, key, value, message):
    cwd, home = places
    with pytest.raises(ConfigError, match=message):
        load_settings(cwd=cwd, home=home, environ={key: value})


def test_missing_explicit_env_file_is_an_error(places):
    cwd, home = places
    with pytest.raises(ConfigError, match="missing file"):
        load_settings(cwd=cwd, home=home, environ={"SCOUT_ENV_FILE": str(Path(cwd, "nope.env"))})
