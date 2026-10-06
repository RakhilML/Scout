"""Start the daemon at login, without administrator rights: a systemd user unit on Linux, a
LaunchAgent on macOS, a script in the Startup folder on Windows."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from scout.errors import ScoutError

Command = tuple[str, ...]
_UNIT = "scout.service"
_AGENT = "dev.scout.daemon"


@dataclass(frozen=True, slots=True)
class ServicePlan:
    files: dict[Path, str]  # written by install, deleted by uninstall
    install: tuple[Command, ...] = ()  # run after the files are written
    uninstall: tuple[Command, ...] = ()  # run before the files are deleted


def plan(
    platform: str, *, python: Path, home: Path, env_file: Path | None, appdata: Path | None = None
) -> ServicePlan:
    """What installing the daemon as a login service means on *platform* (a sys.platform)."""
    if platform == "win32":
        return _startup_script(python, home, env_file, appdata)
    if platform == "darwin":
        return _launch_agent(python, home, env_file)
    return _systemd_unit(python, home, env_file)


Runner = Callable[..., object]  # subprocess.run, or a stand-in


def install(service: ServicePlan, *, run: Runner | None = None) -> None:
    for path, text in service.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    _run_all(service.install, run, check=True)


def uninstall(service: ServicePlan, *, run: Runner | None = None) -> None:
    _run_all(service.uninstall, run, check=False)  # it may already be stopped
    for path in service.files:
        path.unlink(missing_ok=True)


def _run_all(commands: Sequence[Command], run: Runner | None, *, check: bool) -> None:
    run = run or subprocess.run
    for command in commands:
        try:
            run(list(command), check=check)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ScoutError(f"{' '.join(command)} failed: {exc}") from exc


def _startup_script(
    python: Path, home: Path, env_file: Path | None, appdata: Path | None
) -> ServicePlan:
    # pythonw runs without a console window; the daemon logs to a file.
    windowless = python.with_name("pythonw.exe")
    interpreter = windowless if windowless.exists() else python
    lines = ["@echo off", "rem Starts the Scout daemon at login. Remove: scout service uninstall"]
    if env_file is not None:
        lines.append(f'set "SCOUT_ENV_FILE={env_file}"')
    lines.append(f'start "Scout daemon" "{interpreter}" -m scout daemon')
    startup = (appdata or home / "AppData" / "Roaming") / "Microsoft/Windows/Start Menu/Programs"
    return ServicePlan(
        files={startup / "Startup" / "scout-daemon.cmd": "\r\n".join(lines) + "\r\n"}
    )


def _systemd_unit(python: Path, home: Path, env_file: Path | None) -> ServicePlan:
    environment = f'Environment="SCOUT_ENV_FILE={env_file}"\n' if env_file is not None else ""
    unit = (
        "[Unit]\n"
        "Description=Scout: scheduled web research\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f'ExecStart="{python}" -m scout daemon\n'
        f"{environment}"
        "Restart=on-failure\n"
        "RestartSec=60\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    return ServicePlan(
        files={home / ".config/systemd/user" / _UNIT: unit},
        install=(
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", _UNIT),
        ),
        uninstall=(("systemctl", "--user", "disable", "--now", _UNIT),),
    )


def _launch_agent(python: Path, home: Path, env_file: Path | None) -> ServicePlan:
    path = home / "Library/LaunchAgents" / f"{_AGENT}.plist"
    agent: dict[str, object] = {
        "Label": _AGENT,
        "ProgramArguments": [str(python), "-m", "scout", "daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
    }
    if env_file is not None:
        agent["EnvironmentVariables"] = {"SCOUT_ENV_FILE": str(env_file)}
    return ServicePlan(
        files={path: plistlib.dumps(agent).decode("utf-8")},
        install=(("launchctl", "load", "-w", str(path)),),
        uninstall=(("launchctl", "unload", "-w", str(path)),),
    )
