from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from contextlib import chdir, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from jobsfr import CLI_NAME
from jobsfr.cli.options import ProfileOption
from jobsfr.config.schedule_yaml import save_schedule_after_install
from jobsfr.config.settings import PROJECT_ROOT, WEEKDAYS, ScheduleConfig, Settings, default_local_config
from jobsfr.config.user_paths import user_data_dir, user_log_dir

TIMER_NAME = f"{CLI_NAME}.timer"
SERVICE_NAME = f"{CLI_NAME}.service"
HOMEBREW_FORMULA = f"mandaloriany/tap/{CLI_NAME}"
HOMEBREW_LABEL = f"homebrew.mxcl.{CLI_NAME}"
schedule_app = typer.Typer(help="Enable or disable automatic searches.", no_args_is_help=True)


def _systemd_argument(value: str) -> str:
    return json.dumps(value.replace("%", "%%"), ensure_ascii=False)


def _systemd_units(
    checkout: Path, executable: Path, schedule: ScheduleConfig, profile_args: list[str]
) -> tuple[str, str]:
    checkout_path = str(checkout).replace("%", "%%")
    if "\n" in checkout_path or "\r" in checkout_path:
        raise ValueError("Systemd requires a checkout path without line breaks.")
    profile_command = "".join(" " + _systemd_argument(arg) for arg in profile_args)
    executable_env = _systemd_argument(f"JOBSFR_EXECUTABLE={executable}")
    service = (checkout / f"deploy/{SERVICE_NAME}").read_text(encoding="utf-8")
    service = service.replace(
        "ExecStart=%h/apps/jobsfr/deploy/run.sh",
        f"ExecStart=:/usr/bin/env {executable_env} bash ./deploy/run.sh{profile_command}",
    ).replace("WorkingDirectory=%h/apps/jobsfr", f"WorkingDirectory={checkout_path}/.")
    service = service.replace("append:%h/apps/jobsfr/data/service.log", f"append:{checkout_path}/data/service.log")

    days = ",".join(day.title() for day in dict.fromkeys(schedule.days))
    calendar = f"{days} *-*-* {schedule.time}:00 Europe/Paris"
    timer = (checkout / f"deploy/{TIMER_NAME}").read_text(encoding="utf-8")
    return service, timer.replace("@CALENDAR@", calendar)


def _install_systemd_timer(checkout: Path, schedule: ScheduleConfig, profile_args: list[str]) -> None:
    for tool in ("systemctl", "flock"):
        if shutil.which(tool) is None:
            raise ValueError(f"Install {tool} before setting up the Linux timer.")
    executable = Path(sys.executable).with_name(CLI_NAME)
    service, timer = _systemd_units(checkout, executable, schedule, profile_args)
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"
    config_dir.mkdir(parents=True, exist_ok=True)
    (checkout / "data").mkdir(exist_ok=True)
    (config_dir / SERVICE_NAME).write_text(service, encoding="utf-8")
    (config_dir / TIMER_NAME).write_text(timer, encoding="utf-8")
    for args in (("daemon-reload",), ("enable", TIMER_NAME), ("restart", TIMER_NAME), ("list-timers", TIMER_NAME)):
        subprocess.run(["systemctl", "--user", *args], check=True)


def _homebrew_plist(
    workdir: Path, executable: Path, schedule: ScheduleConfig, profile_args: list[str], log_path: Path | None = None
) -> bytes:
    hour, minute = map(int, schedule.time.split(":"))
    return plistlib.dumps(
        {
            "Label": HOMEBREW_LABEL,
            "ProgramArguments": [str(executable), "search", *profile_args],
            "WorkingDirectory": str(workdir),
            "StartCalendarInterval": [
                {"Weekday": (WEEKDAYS.index(day) + 1) % 7, "Hour": hour, "Minute": minute}
                for day in dict.fromkeys(schedule.days)
            ],
            **({"StandardOutPath": str(log_path), "StandardErrorPath": str(log_path)} if log_path else {}),
            "Umask": 0o077,
        }
    )


def _install_homebrew_timer(workdir: Path, schedule: ScheduleConfig, profile_args: list[str]) -> None:
    prefix = subprocess.check_output(["brew", "--prefix", HOMEBREW_FORMULA], text=True).strip()
    executable = Path(prefix) / "bin" / CLI_NAME
    if not executable.is_file():
        raise ValueError(f"Install {HOMEBREW_FORMULA} with Homebrew first.")
    log_path = user_log_dir() / "service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as temp_dir:
        plist = Path(temp_dir) / f"{HOMEBREW_LABEL}.plist"
        plist.write_bytes(_homebrew_plist(workdir, executable, schedule, profile_args, log_path))
        subprocess.run(["brew", "services", "restart", HOMEBREW_FORMULA, f"--file={plist}"], check=True)
    typer.echo(f"Scheduled for {', '.join(schedule.days)} at {schedule.time} (Mac local time).")


def _prompt_schedule(defaults: ScheduleConfig, timezone: str) -> ScheduleConfig:
    days = ", ".join(defaults.days)
    time = defaults.time
    day_choices = ", ".join(WEEKDAYS)
    while True:
        days = typer.prompt(f"Run on (comma-separated days: {day_choices})", default=days)
        time = typer.prompt(f"Time (HH:MM, {timezone})", default=time).strip()
        try:
            return ScheduleConfig.model_validate(
                {"days": [day.strip().lower() for day in days.split(",")], "time": time}
            )
        except ValidationError:
            typer.echo("Use days like mon, wed, fri and a 24-hour time like 09:15.", err=True)


@schedule_app.command("enable")
def install_timer(
    profile: ProfileOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Use the configured schedule without prompts.")] = False,
) -> None:
    """Install or update scheduled searches on macOS or Linux."""
    try:
        if sys.platform == "darwin":
            workdir = user_data_dir()
            workdir.mkdir(parents=True, exist_ok=True)
            install = _install_homebrew_timer
            timezone = "Mac local time"
        elif sys.platform == "linux" and PROJECT_ROOT is not None:
            workdir = PROJECT_ROOT
            install = _install_systemd_timer
            timezone = "Europe/Paris"
        else:
            raise ValueError("Run from a Linux checkout or a Homebrew installation on macOS.")

        profile_args = ["--profile", str(profile)] if profile else []
        with chdir(workdir):
            schedule = Settings(_profile=profile, _require_search_setup=True).schedule
            save_to = None
            if not yes:
                path = (profile or default_local_config()).resolve()
                saved = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
                if saved and "schedule" in saved:
                    days = ", ".join(schedule.days)
                    typer.echo(f"Configured schedule is {days} at {schedule.time} ({timezone}).")
                    modify = typer.confirm("Modify this schedule before enabling it?")
                else:
                    modify = True
                if modify:
                    schedule = _prompt_schedule(schedule, timezone)
                    save_to = path
            with save_schedule_after_install(save_to, schedule) if save_to is not None else nullcontext():
                install(workdir, schedule, profile_args)
            if save_to is not None:
                typer.echo(f"Saved schedule to {save_to}")
    except (OSError, ValueError, subprocess.CalledProcessError, yaml.YAMLError) as exc:
        typer.echo(f"Could not update schedule: {exc}", err=True)
        raise typer.Exit(1) from exc


@schedule_app.command("disable")
def disable_timer() -> None:
    """Stop scheduled searches on macOS or Linux."""
    try:
        if sys.platform == "darwin":
            command = ["brew", "services", "stop", HOMEBREW_FORMULA]
        elif sys.platform == "linux":
            command = ["systemctl", "--user", "disable", "--now", TIMER_NAME]
        else:
            raise ValueError("Scheduling is supported on macOS with Homebrew and Linux with systemd.")
        subprocess.run(command, check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Could not disable timer: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Scheduled searches disabled.")
