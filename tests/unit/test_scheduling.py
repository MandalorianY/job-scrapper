import json
import plistlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from jobsfr.cli import app, scheduling
from jobsfr.cli.scheduling import HOMEBREW_LABEL, _homebrew_plist, _systemd_units
from jobsfr.config import settings
from jobsfr.config.settings import PROJECT_ROOT, ScheduleConfig, Settings


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_schedule_command_uses_fixed_working_directory(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "scheduled workdir"
    workdir.mkdir()
    (workdir / ".env").write_text('JOBSFR_SCHEDULE__TIME="09:15"\n', encoding="utf-8")
    monkeypatch.delenv("JOBSFR_SCHEDULE__TIME", raising=False)
    monkeypatch.setattr(scheduling, "PROJECT_ROOT", workdir if platform == "linux" else None)
    monkeypatch.setattr(scheduling, "user_data_dir", lambda: workdir)
    monkeypatch.setattr(scheduling, "sys", SimpleNamespace(platform=platform))
    calls: list[tuple[Path, ScheduleConfig, list[str]]] = []

    def install(workdir: Path, schedule: ScheduleConfig, profile_args: list[str]) -> None:
        calls.append((workdir, schedule, profile_args))

    monkeypatch.setattr(scheduling, "_install_homebrew_timer", install)
    monkeypatch.setattr(scheduling, "_install_systemd_timer", install)
    result = CliRunner().invoke(app, ["schedule", "enable"])
    assert result.exit_code == 1
    assert "jobsfr setup" in result.output
    assert not calls

    for directory in (tmp_path / "first caller", tmp_path / "second caller"):
        directory.mkdir()
        monkeypatch.chdir(directory)
        caller_dir = Path.cwd()
        (caller_dir / ".env").write_text('JOBSFR_SCHEDULE__TIME="20:00"\n', encoding="utf-8")
        profile = caller_dir / "search profile.yaml"
        profile.write_text(
            'queries: [data analyst]\nlocations: ["Toulouse, France"]\nschedule: {days: [mon], time: "10:30"}\n',
            encoding="utf-8",
        )

        result = CliRunner().invoke(app, ["schedule", "enable", "--yes", "--profile", profile.name])

        assert result.exit_code == 0, result.output
        assert calls[-1] == (workdir, ScheduleConfig(days=("mon",), time="09:15"), ["--profile", str(profile)])
        assert Path.cwd() == caller_dir
    assert len(calls) == 2


def test_schedule_enable_prompts_and_applies_confirmed_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile.yaml"
    search_yaml = "# Search settings\nqueries: [data analyst]\nlocations: [Toulouse]\n"
    profile.write_text(search_yaml + 'schedule:\n  days: [mon]\n  time: "09:15"\n# Keep this note\n', encoding="utf-8")
    monkeypatch.setattr(scheduling, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(scheduling, "user_data_dir", lambda: tmp_path)
    monkeypatch.delenv("JOBSFR_SCHEDULE__TIME", raising=False)
    monkeypatch.delenv("JOBSFR_SCHEDULE__DAYS", raising=False)
    install = Mock()
    monkeypatch.setattr(scheduling, "_install_homebrew_timer", install)
    runner = CliRunner()
    args = ["schedule", "enable", "--profile", str(profile)]

    result = runner.invoke(app, args, input="y\nsunday\n25:30\nWed, Fri\n08:30\n")

    assert result.exit_code == 0, result.output
    assert "Run on (comma-separated days: mon, tue, wed, thu, fri, sat, sun)" in result.output
    assert "[mon]" in result.output and "[09:15]" in result.output
    assert "Use days like mon, wed, fri" in result.stderr
    schedule = ScheduleConfig(days=("wed", "fri"), time="08:30")
    install.assert_called_once_with(tmp_path, schedule, ["--profile", str(profile)])
    saved = profile.read_text(encoding="utf-8")
    assert saved.startswith(search_yaml) and saved.endswith("# Keep this note\n")
    assert Settings(_profile=profile).schedule == schedule
    install.reset_mock()

    result = runner.invoke(app, args, input="n\n")

    assert result.exit_code == 0, result.output
    assert "Modify this schedule before enabling it?" in result.output
    assert "Run on" not in result.output
    install.assert_called_once_with(tmp_path, schedule, ["--profile", str(profile)])
    assert profile.read_text(encoding="utf-8") == saved
    install.reset_mock()

    result = runner.invoke(app, args, input="y\nsun\n")

    assert result.exit_code == 1, result.output
    install.assert_not_called()
    assert profile.read_text(encoding="utf-8") == saved


def test_schedule_enable_saves_first_schedule_only_after_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "config.local.yaml"
    original = "queries: [data analyst]\nlocations: [Toulouse]\n"
    profile.write_text(original, encoding="utf-8")
    monkeypatch.setattr(settings, "LOCAL_CONFIG_FILE", profile)
    monkeypatch.delenv("JOBSFR_DISABLE_LOCAL_CONFIG", raising=False)
    monkeypatch.setattr(scheduling, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(scheduling, "user_data_dir", lambda: tmp_path)
    install = Mock(side_effect=subprocess.CalledProcessError(1, "brew"))
    monkeypatch.setattr(scheduling, "_install_homebrew_timer", install)
    runner = CliRunner()
    args = ["schedule", "enable"]

    result = runner.invoke(app, args, input="tue\n08:00\n")

    assert result.exit_code == 1, result.output
    assert profile.read_text(encoding="utf-8") == original

    install.side_effect = None
    result = runner.invoke(app, args, input="tue\n08:00\n")

    assert result.exit_code == 0, result.output
    assert result.output.startswith("Run on (comma-separated days:")
    assert "Modify this schedule" not in result.output
    assert "Saved schedule" in result.output
    assert yaml.safe_load(profile.read_text(encoding="utf-8"))["schedule"] == {"days": ["tue"], "time": "08:00"}


@pytest.mark.parametrize(
    ("platform", "command"),
    [
        ("darwin", ["brew", "services", "stop", scheduling.HOMEBREW_FORMULA]),
        ("linux", ["systemctl", "--user", "disable", "--now", scheduling.TIMER_NAME]),
    ],
)
def test_schedule_disable_stops_the_platform_timer(
    platform: str, command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduling, "sys", SimpleNamespace(platform=platform))
    stop = Mock()
    monkeypatch.setattr(scheduling.subprocess, "run", stop)

    result = CliRunner().invoke(app, ["schedule", "disable"])

    assert result.exit_code == 0, result.output
    assert "Scheduled searches disabled." in result.output
    stop.assert_called_once_with(command, check=True)


def test_linux_schedule_enable_writes_units_and_reports_missing_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert PROJECT_ROOT is not None
    checkout = tmp_path / "checkout été"
    deploy = checkout / "deploy"
    deploy.mkdir(parents=True)
    for name in (scheduling.SERVICE_NAME, scheduling.TIMER_NAME):
        shutil.copyfile(PROJECT_ROOT / "deploy" / name, deploy / name)
    profile = tmp_path / "search profile.yaml"
    profile.write_text(
        'queries: [analyste]\nlocations: [Lille]\nschedule: {days: [sun], time: "07:15"}\n', encoding="utf-8"
    )
    config_dir = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setattr(scheduling, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(scheduling, "sys", SimpleNamespace(platform="linux", executable=str(tmp_path / "python")))
    commands = Mock()
    monkeypatch.setattr(scheduling.subprocess, "run", commands)
    which = Mock(side_effect=lambda tool: None if tool == "flock" else tool)
    monkeypatch.setattr(scheduling.shutil, "which", which)
    args = ["schedule", "enable", "--yes", "--profile", str(profile)]
    runner = CliRunner()

    failed = runner.invoke(app, args)
    assert failed.exit_code == 1, failed.output
    assert "Install flock" in failed.stderr
    assert not config_dir.exists()
    commands.assert_not_called()

    which.side_effect = lambda tool: tool
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    user_units = config_dir / "systemd" / "user"
    service = (user_units / scheduling.SERVICE_NAME).read_text(encoding="utf-8")
    assert f"WorkingDirectory={checkout}/." in service
    assert json.dumps(str(profile), ensure_ascii=False) in service
    timer = (user_units / scheduling.TIMER_NAME).read_text(encoding="utf-8")
    assert "OnCalendar=Sun *-*-* 07:15:00 Europe/Paris" in timer
    assert [call.args[0][2:] for call in commands.call_args_list] == [
        ["daemon-reload"],
        ["enable", scheduling.TIMER_NAME],
        ["restart", scheduling.TIMER_NAME],
        ["list-timers", scheduling.TIMER_NAME],
    ]


def test_homebrew_schedule_enable_checks_install_and_passes_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "search profile.yaml"
    profile.write_text(
        'queries: [analyste]\nlocations: [Lille]\nschedule: {days: [sat], time: "07:15"}\n', encoding="utf-8"
    )
    executable = tmp_path / "formula" / "bin" / "jobsfr"
    workdir = tmp_path / "user data"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(scheduling, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(scheduling, "user_data_dir", lambda: workdir)
    monkeypatch.setattr(scheduling, "user_log_dir", lambda: log_dir)
    monkeypatch.setattr(scheduling.subprocess, "check_output", Mock(return_value=str(executable.parents[1])))
    plists = []

    def run(command: list[str], *, check: bool) -> None:
        assert command[:4] == ["brew", "services", "restart", scheduling.HOMEBREW_FORMULA]
        assert check
        plists.append(plistlib.loads(Path(command[4].removeprefix("--file=")).read_bytes()))

    monkeypatch.setattr(scheduling.subprocess, "run", run)
    args = ["schedule", "enable", "--yes", "--profile", str(profile)]
    runner = CliRunner()

    failed = runner.invoke(app, args)
    assert failed.exit_code == 1, failed.output
    assert "with Homebrew first" in failed.stderr
    assert not plists

    executable.parent.mkdir(parents=True)
    executable.touch()
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    (plist,) = plists
    assert plist["ProgramArguments"] == [str(executable), "search", "--profile", str(profile)]
    assert plist["StartCalendarInterval"] == [{"Weekday": 6, "Hour": 7, "Minute": 15}]
    assert plist["StandardOutPath"] == str(log_dir / "service.log")


def test_native_timers_use_profile_schedule(tmp_path: Path) -> None:
    assert PROJECT_ROOT is not None
    profile = tmp_path / "my tracker % $ profile.yaml"
    profile.write_text('schedule:\n  days: [mon, sun]\n  time: "09:15"\n', encoding="utf-8")
    schedule = Settings(_profile=profile).schedule
    profile_args = ["--profile", str(profile)]

    executable = tmp_path / "custom env %" / "bin/jobsfr"
    service, timer = _systemd_units(PROJECT_ROOT, executable, schedule, profile_args)
    quoted_profile = json.dumps(str(profile).replace("%", "%%"), ensure_ascii=False)
    quoted_exec_env = json.dumps(f"JOBSFR_EXECUTABLE={executable}".replace("%", "%%"), ensure_ascii=False)
    assert f"WorkingDirectory={str(PROJECT_ROOT).replace('%', '%%')}/.\n" in service
    assert quoted_exec_env in service
    assert f'./deploy/run.sh "--profile" {quoted_profile}\n' in service
    assert "OnCalendar=Mon,Sun *-*-* 09:15:00 Europe/Paris\n" in timer
    assert "Unit=jobsfr.service\n" in timer

    log_path = tmp_path / "Library/Logs/jobsfr/service.log"
    plist = plistlib.loads(_homebrew_plist(tmp_path, executable, schedule, profile_args, log_path))
    assert plist["Label"] == HOMEBREW_LABEL == "homebrew.mxcl.jobsfr"
    assert plist["ProgramArguments"] == [str(executable), "search", *profile_args]
    assert plist["WorkingDirectory"] == str(tmp_path)
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(log_path)
    assert plist["StartCalendarInterval"] == [
        {"Weekday": 1, "Hour": 9, "Minute": 15},
        {"Weekday": 0, "Hour": 9, "Minute": 15},
    ]
    assert "RunAtLoad" not in plist
    assert "StandardOutPath" not in plistlib.loads(_homebrew_plist(tmp_path, executable, schedule, profile_args))


@pytest.mark.parametrize("schedule", [{"days": []}, {"days": ["sunday"]}, {"time": "25:30"}])
def test_schedule_rejects_invalid_values(schedule: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScheduleConfig.model_validate(schedule)
