import plistlib
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from scout.cli import main
from scout.errors import ScoutError
from scout.monitor import service

PYTHON = Path("/opt/scout/bin/python")
ENV = Path("/home/ana/scout/.env")


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.fail = fail

    def __call__(self, command, *, check):
        self.commands.append(command)
        if self.fail and check:
            raise subprocess.CalledProcessError(1, command)


def test_linux_gets_a_systemd_user_unit(tmp_path):
    plan = service.plan("linux", python=PYTHON, home=tmp_path, env_file=ENV)
    ((path, unit),) = plan.files.items()
    assert path == tmp_path / ".config/systemd/user/scout.service"
    assert f'ExecStart="{PYTHON}" -m scout daemon' in unit
    assert f'Environment="SCOUT_ENV_FILE={ENV}"' in unit

    run = Recorder()
    service.install(plan, run=run)
    assert path.read_text(encoding="utf-8") == unit
    assert run.commands[-1] == ["systemctl", "--user", "enable", "--now", "scout.service"]
    service.uninstall(plan, run=run)
    assert not path.exists()
    assert run.commands[-1] == ["systemctl", "--user", "disable", "--now", "scout.service"]


def test_macos_gets_a_launch_agent(tmp_path):
    plan = service.plan("darwin", python=PYTHON, home=tmp_path, env_file=None)
    ((path, text),) = plan.files.items()
    agent = plistlib.loads(text.encode())
    assert agent["ProgramArguments"] == [str(PYTHON), "-m", "scout", "daemon"]
    assert "EnvironmentVariables" not in agent
    assert plan.install == (("launchctl", "load", "-w", str(path)),)


def test_windows_gets_a_windowless_startup_script(tmp_path):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").touch()
    (scripts / "pythonw.exe").touch()
    appdata = tmp_path / "Roaming"
    plan = service.plan(
        "win32", python=scripts / "python.exe", home=tmp_path, env_file=ENV, appdata=appdata
    )
    ((path, script),) = plan.files.items()
    assert path == appdata / "Microsoft/Windows/Start Menu/Programs/Startup/scout-daemon.cmd"
    assert f'"{scripts / "pythonw.exe"}" -m scout daemon' in script
    assert f'set "SCOUT_ENV_FILE={ENV}"' in script
    assert (plan.install, plan.uninstall) == ((), ())
    service.install(plan, run=Recorder())
    written = path.read_bytes()
    assert b"\r\r\n" not in written
    assert written.count(b"\r\n") == len(script.splitlines())


def test_a_failing_service_manager_is_reported(tmp_path):
    plan = service.plan("linux", python=PYTHON, home=tmp_path, env_file=None)
    with pytest.raises(ScoutError, match="systemctl --user daemon-reload failed"):
        service.install(plan, run=Recorder(fail=True))


def test_service_install_asks_first_and_dry_run_changes_nothing(workspace, monkeypatch):
    monkeypatch.setattr("scout.cli.daemon.sys.platform", "linux")
    monkeypatch.setattr("scout.cli.daemon.Path.home", lambda: workspace)
    run = Recorder()
    monkeypatch.setattr("scout.monitor.service.subprocess.run", run)
    unit = workspace / ".config/systemd/user/scout.service"

    dry = CliRunner().invoke(main, ["service", "install", "--dry-run"])
    assert "ExecStart=" in dry.output
    declined = CliRunner().invoke(main, ["service", "install"], input="n\n")
    assert declined.exit_code == 1
    assert not unit.exists()
    assert run.commands == []

    assert CliRunner().invoke(main, ["service", "install", "--yes"]).exit_code == 0
    assert unit.exists()
    assert CliRunner().invoke(main, ["service", "uninstall", "--yes"]).exit_code == 0
    assert not unit.exists()


def test_logs_shows_the_end_of_the_daemon_log(workspace):
    runner = CliRunner()
    assert "No log yet" in runner.invoke(main, ["logs"]).output
    log = workspace / "data" / "logs" / "scout.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\ntwo [bold]\nthree\n", encoding="utf-8")
    assert runner.invoke(main, ["logs", "-n", "2"]).output.splitlines() == ["two [bold]", "three"]
