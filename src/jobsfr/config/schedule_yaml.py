from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from yaml.nodes import MappingNode, ScalarNode

from jobsfr.config.settings import ScheduleConfig


def _update_schedule(contents: str, schedule: ScheduleConfig) -> str:
    settings = yaml.safe_load(contents)
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise yaml.YAMLError("The search profile must be a YAML mapping.")
    settings["schedule"] = schedule.model_dump(mode="json")
    entry = f'schedule: {{days: [{", ".join(schedule.days)}], time: "{schedule.time}"}}'
    newline = "\r\n" if "\r\n" in contents else "\n"
    root = yaml.compose(contents, Loader=yaml.SafeLoader)
    if isinstance(root, MappingNode) and not root.flow_style:
        for key, value in reversed(root.value):
            if (
                isinstance(key, ScalarNode)
                and key.value == "schedule"
                and key.start_mark.column == root.start_mark.column
            ):
                start = key.start_mark.index
                # The last real token excludes comments and block-end markers.
                end = max(
                    (
                        token.end_mark.index
                        for token in yaml.scan(contents, Loader=yaml.SafeLoader)
                        if start <= token.start_mark.index < token.end_mark.index <= value.end_mark.index
                    ),
                    default=start,
                )
                updated = contents[:start] + entry + contents[end:]
                break
        else:
            end = root.end_mark.index
            prefix = contents[:end]
            separator = "" if prefix.endswith("\n") else newline
            updated = prefix + separator + " " * root.start_mark.column + entry + newline + contents[end:]
        try:
            if yaml.safe_load(updated) == settings:
                return updated
        except yaml.YAMLError:
            pass
    elif root is None:
        separator = newline if contents and not contents.endswith("\n") else ""
        return contents + separator + entry + newline

    return yaml.safe_dump(settings, allow_unicode=True, sort_keys=False, line_break=newline)


@contextmanager
def save_schedule_after_install(path: Path, schedule: ScheduleConfig) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else None
    updated = _update_schedule(original.decode("utf-8") if original is not None else "", schedule)
    with TemporaryDirectory(dir=path.parent) as temp:
        draft = Path(temp) / path.name
        draft.write_bytes(updated.encode("utf-8"))
        if original is not None:
            shutil.copymode(path, draft)
        yield
        try:
            current = path.read_bytes() if path.exists() else None
            if current != original:
                raise OSError("The profile changed during installation.")
            draft.replace(path)
        except OSError as exc:
            raise ValueError(f"The timer was enabled, but the schedule could not be saved to {path}: {exc}") from exc
