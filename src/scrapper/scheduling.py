from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer

from scrapper.cli_paths import existing_file
from scrapper.config.settings import PROJECT_ROOT, WEEKDAYS, ScheduleConfig, Settings

TIMER_NAME = "job-scrapper.timer"
HOMEBREW_FORMULA = "mandaloriany/tap/job-scrapper"
HOMEBREW_LABEL = "homebrew.mxcl.job-scrapper"


def _systemd_argument(value: str) -> str:
    return json.dumps(value.replace("%", "%%"), ensure_ascii=False)


def _systemd_units(
    checkout: Path, executable: Path, schedule: ScheduleConfig, profile_args: list[str]
) -> tuple[str, str]:
    checkout_path = str(checkout).replace("%", "%%")
    if "\n" in checkout_path or "\r" in checkout_path:
        raise ValueError("Systemd requires a checkout path without line breaks.")
    profile_command = "".join(" " + _systemd_argument(arg) for arg in profile_args)
    executable_env = _systemd_argument(f"JOB_SCRAPPER_EXECUTABLE={executable}")
    service = (checkout / "deploy/job-scrapper.service").read_text(encoding="utf-8")
    service = service.replace(
        "ExecStart=%h/apps/job-scrapper/deploy/run.sh",
        f"ExecStart=:/usr/bin/env {executable_env} bash ./deploy/run.sh{profile_command}",
    ).replace("WorkingDirectory=%h/apps/job-scrapper", f"WorkingDirectory={checkout_path}/.")
    service = service.replace(
        "append:%h/apps/job-scrapper/data/service.log", f"append:{checkout_path}/data/service.log"
    )

    days = ",".join(day.title() for day in dict.fromkeys(schedule.days))
    calendar = f"{days} *-*-* {schedule.time}:00 Europe/Paris"
    timer = (checkout / f"deploy/{TIMER_NAME}").read_text(encoding="utf-8")
    timer = timer.replace("Run the job scraper on weekdays at 15:30 Paris time", "Run the job scraper on a schedule")
    timer = timer.replace("OnCalendar=Mon..Fri *-*-* 15:30:00 Europe/Paris", f"OnCalendar={calendar}")
    return service, timer


def _install_systemd_timer(checkout: Path, schedule: ScheduleConfig, profile_args: list[str]) -> None:
    for tool in ("systemctl", "flock"):
        if shutil.which(tool) is None:
            raise ValueError(f"Install {tool} before setting up the Linux timer.")
    executable = Path(sys.executable).with_name("job-scrapper")
    service, timer = _systemd_units(checkout, executable, schedule, profile_args)
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"
    config_dir.mkdir(parents=True, exist_ok=True)
    (checkout / "data").mkdir(exist_ok=True)
    (config_dir / "job-scrapper.service").write_text(service, encoding="utf-8")
    (config_dir / TIMER_NAME).write_text(timer, encoding="utf-8")
    for args in (("daemon-reload",), ("enable", TIMER_NAME), ("restart", TIMER_NAME), ("list-timers", TIMER_NAME)):
        subprocess.run(["systemctl", "--user", *args], check=True)


def _homebrew_plist(workdir: Path, executable: Path, schedule: ScheduleConfig, profile_args: list[str]) -> bytes:
    hour, minute = map(int, schedule.time.split(":"))
    log_path = str(workdir / "data/service.log")
    return plistlib.dumps(
        {
            "Label": HOMEBREW_LABEL,
            "ProgramArguments": [str(executable), *profile_args],
            "WorkingDirectory": str(workdir),
            "StartCalendarInterval": [
                {"Weekday": (WEEKDAYS.index(day) + 1) % 7, "Hour": hour, "Minute": minute}
                for day in dict.fromkeys(schedule.days)
            ],
            "StandardOutPath": log_path,
            "StandardErrorPath": log_path,
            "Umask": 0o077,
        }
    )


def _install_homebrew_timer(workdir: Path, schedule: ScheduleConfig, profile_args: list[str]) -> None:
    prefix = subprocess.check_output(["brew", "--prefix", HOMEBREW_FORMULA], text=True).strip()
    executable = Path(prefix) / "bin/job-scrapper"
    if not executable.is_file():
        raise ValueError(f"Install {HOMEBREW_FORMULA} with Homebrew first.")
    (workdir / "data").mkdir(exist_ok=True)
    with TemporaryDirectory() as temp_dir:
        plist = Path(temp_dir) / f"{HOMEBREW_LABEL}.plist"
        plist.write_bytes(_homebrew_plist(workdir, executable, schedule, profile_args))
        subprocess.run(["brew", "services", "restart", HOMEBREW_FORMULA, f"--file={plist}"], check=True)
    typer.echo(f"Scheduled for {', '.join(schedule.days)} at {schedule.time} (Mac local time).")


def _install_timer(
    profile: Annotated[
        Path | None,
        typer.Option(parser=existing_file, metavar="FILE", help="Use this YAML profile."),
    ] = None,
) -> None:
    """Install or update the scraper's user timer."""
    try:
        if sys.platform == "linux" and PROJECT_ROOT is not None:
            os.chdir(PROJECT_ROOT)
        schedule = Settings(_profile=profile).schedule
        profile_args = ["--profile", str(profile)] if profile else []
        if sys.platform == "darwin":
            _install_homebrew_timer(Path.cwd(), schedule, profile_args)
        elif sys.platform == "linux" and PROJECT_ROOT is not None:
            _install_systemd_timer(PROJECT_ROOT, schedule, profile_args)
        else:
            raise ValueError("Run from a Linux checkout or a Homebrew installation on macOS.")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Could not install timer: {exc}", err=True)
        raise typer.Exit(1) from exc


def main() -> None:
    typer.run(_install_timer)
