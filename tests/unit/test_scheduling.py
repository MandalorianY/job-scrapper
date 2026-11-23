import json
import plistlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from scrapper.config.settings import PROJECT_ROOT, ScheduleConfig, Settings
from scrapper.scheduling import HOMEBREW_LABEL, _homebrew_plist, _systemd_units


def test_native_timers_use_profile_schedule(tmp_path: Path) -> None:
    assert PROJECT_ROOT is not None
    profile = tmp_path / "my tracker % $ profile.yaml"
    profile.write_text('schedule:\n  days: [mon, sun]\n  time: "09:15"\n', encoding="utf-8")
    schedule = Settings(_profile=profile).schedule
    profile_args = ["--profile", str(profile)]

    executable = tmp_path / "custom env %" / "bin/jobsfr"
    service, timer = _systemd_units(PROJECT_ROOT, executable, schedule, profile_args)
    quoted_profile = json.dumps(str(profile).replace("%", "%%"), ensure_ascii=False)
    quoted_exec_env = json.dumps(f"JOB_SCRAPPER_EXECUTABLE={executable}".replace("%", "%%"), ensure_ascii=False)
    assert f"WorkingDirectory={str(PROJECT_ROOT).replace('%', '%%')}/.\n" in service
    assert quoted_exec_env in service
    assert f'./deploy/run.sh "--profile" {quoted_profile}\n' in service
    assert "OnCalendar=Mon,Sun *-*-* 09:15:00 Europe/Paris\n" in timer

    log_path = tmp_path / "Library/Logs/job-scrapper/service.log"
    plist = plistlib.loads(_homebrew_plist(tmp_path, executable, schedule, profile_args, log_path))
    assert plist["Label"] == HOMEBREW_LABEL == "homebrew.mxcl.jobsfr"
    assert plist["ProgramArguments"] == [str(executable), *profile_args]
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
