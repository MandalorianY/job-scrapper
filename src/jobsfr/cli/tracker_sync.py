"""Rclone sync for the primary application tracker."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from hashlib import file_digest
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp
from typing import Annotated
from uuid import uuid4

import typer
import yaml
from pydantic import BaseModel

from jobsfr.cli.options import ProfileOption
from jobsfr.cli.runner import run
from jobsfr.config import SearchConfig
from jobsfr.export import export_paths
from jobsfr.export.jobs import TRACKER_HISTORY_FILENAME, _publish_exports, read_tracker, tracker_paths
from jobsfr.runtime.file_lock import file_lock as _sync_lock
from jobsfr.runtime.files import write_text_atomically


class SyncError(Exception):
    pass


class SyncCheckpoint(BaseModel):
    last_synced_hash: str | None = None
    last_synced_target: str | None = None

    def checkpoint_for(self, target: str) -> str | None:
        return self.last_synced_hash if target == self.last_synced_target else None


@dataclass(frozen=True)
class Remote:
    executable: str
    target: str

    @classmethod
    def connect(cls, target: str | None, suffix: str) -> Remote:
        executable = shutil.which("rclone")
        if executable is None:
            raise SyncError("Please install rclone, then run rclone config to set up a remote.")
        hint = f"Set sync.target in YAML to REMOTE:folder/jobs{suffix}."
        if not target:
            raise SyncError(hint)
        name, separator, path = target.partition(":")
        if sys.platform == "win32" and len(name) == 1 and name.isascii() and name.isalpha():
            raise SyncError("On Windows, use an rclone remote name with more than one letter to avoid drive letters.")
        if (
            not separator
            or not name
            or target.startswith("-")
            or path.endswith("/")
            or PurePosixPath(path).suffix.lower() != suffix
        ):
            raise SyncError(f"The destination must be a named rclone file ending in {suffix}. {hint}")
        remote = cls(executable, target)
        configured = remote.call("listremotes")
        if configured is None or f"{name}:" not in configured.splitlines():
            raise SyncError(f"Remote {name!r} is missing. Run rclone config and set it up first.")
        return remote

    @property
    def directory(self) -> str:
        name, _, path = self.target.partition(":")
        return f"{name}:{path.rpartition('/')[0]}"

    def call(self, *args: str, allow_missing: bool = False) -> str | None:
        try:
            result = subprocess.run(
                [self.executable, *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncError(f"rclone {args[0]} failed: {exc}") from exc
        if result.returncode == 0:
            return result.stdout
        if allow_missing and result.returncode in (3, 4):
            return None
        detail = result.stderr.strip().splitlines()
        raise SyncError(detail[-1] if detail else f"rclone {args[0]} exited with code {result.returncode}.")

    def fetch(self, path: Path) -> None:
        if self.call("copyto", self.target, str(path), allow_missing=True) is not None:
            if not path.is_file():
                raise SyncError("The remote tracker path did not resolve to a file.")
            return
        listing = self.call("lsf", self.directory, "--files-only", "--max-depth", "1")
        name = self.target.partition(":")[2].rsplit("/", 1)[-1]
        if listing is None or name in listing.splitlines():
            raise SyncError("Could not read the remote tracker.")

    def check_write(self, workspace: Path) -> None:
        probe = workspace / "probe.txt"
        probe.write_text("jobsfr sync check\n", encoding="utf-8")
        download = workspace / "probe-readback.txt"
        separator = "" if self.directory.endswith(":") else "/"
        remote_probe = f"{self.directory}{separator}.jobsfr-check-{uuid4().hex}.txt"
        self.call("copyto", str(probe), remote_probe, "--immutable")
        try:
            self.call("copyto", remote_probe, str(download))
            if download.read_bytes() != probe.read_bytes():
                raise SyncError("Could not verify the remote write.")
        finally:
            self.call("deletefile", remote_probe)

    def publish(self, path: Path, *, first_upload: bool) -> None:
        flags = ("--ignore-times", "--immutable") if first_upload else ("--ignore-times",)
        self.call("copyto", str(path), self.target, *flags)


def _digest(path: Path) -> str | None:
    try:
        with path.open("rb") as file:
            return file_digest(file, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def _check_tracker(path: Path) -> None:
    if not path.exists():
        return
    try:
        read_tracker(path)
    except Exception as exc:
        raise SyncError(f"Could not open the remote tracker: {exc}") from exc


def _check_destination(target: str | None, incoming: Path) -> Remote:
    remote = Remote.connect(target, incoming.suffix)
    remote.fetch(incoming)
    _check_tracker(incoming)
    try:
        remote.check_write(incoming.parent)
    except (OSError, SyncError) as exc:
        raise SyncError(f"Could not verify write access to the tracker folder. {exc}") from exc
    return remote


def sync_checkpoint_path(database_path: Path) -> Path:
    database = database_path.resolve()
    return database.parent / ".tracker-sync" / "databases" / database.name / "settings.json"


def load_sync_checkpoint(path: Path) -> SyncCheckpoint:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SyncCheckpoint()
    return SyncCheckpoint.model_validate_json(contents)


def _save_checkpoint(path: Path, checkpoint: SyncCheckpoint) -> None:
    write_text_atomically(path, checkpoint.model_dump_json(indent=2) + "\n")


def _local_tracker(config: SearchConfig) -> Path:
    candidates = tracker_paths(config.export_dir, config.export.formats)
    return next((path for path in candidates if path.is_file()), candidates[0])


def _save_conflict(output_dir: Path, **copies: Path) -> None:
    backup = Path(mkdtemp(prefix="conflict-", dir=output_dir))
    for label, path in copies.items():
        if path.is_file():
            shutil.copy2(path, backup / f"{label}{path.suffix}")
    print(f"Tracker copies saved to {backup}")


def _sync(config: SearchConfig, path: Path, target: str | None, preference: str | None, profile: Path | None) -> None:
    checkpoint = load_sync_checkpoint(path)
    outputs = export_paths(config.export_dir, config.export.formats)
    primary = outputs[0]
    config.export_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tracker-", dir=config.export_dir) as temp:
        workspace = Path(temp)
        incoming = workspace / f"drive{primary.suffix}"
        try:
            remote = _check_destination(target, incoming)
        except (OSError, SyncError) as exc:
            print(f"Tracker sync skipped. {exc} Running search and export locally.", file=sys.stderr, flush=True)
            run(profile=profile)
            return

        local = _local_tracker(config)
        local_hash, drive_hash = _digest(local), _digest(incoming)
        last_synced_hash = checkpoint.checkpoint_for(remote.target)
        diverged = (drive_hash is None and last_synced_hash is not None) or (
            local_hash is not None
            and drive_hash is not None
            and local_hash != drive_hash
            and last_synced_hash not in (local_hash, drive_hash)
        )
        inputs = {"local": local, "drive": incoming}
        if diverged:
            _save_conflict(path.parent, local=local, drive=incoming)
            if preference is None or not inputs[preference].is_file():
                print(
                    "Tracker sync skipped. Running search and export locally. "
                    "Review both copies, then rerun with --prefer local or --prefer drive."
                )
                run(profile=profile)
                return
            seed = inputs[preference]
        else:
            seed = incoming if drive_hash is not None and local_hash in (None, last_synced_hash) else local

        if seed.is_file():
            shutil.copy2(seed, workspace / (primary.name if seed == incoming else seed.name))
        history = config.export_dir / TRACKER_HISTORY_FILENAME
        history_hash = _digest(history)
        if history_hash is not None:
            shutil.copy2(history, workspace / history.name)
        run(profile=profile, db=config.database_path, export_dir=workspace)
        output = workspace / primary.name
        if _digest(_local_tracker(config)) != local_hash or _digest(history) != history_hash:
            _save_conflict(path.parent, local=_local_tracker(config), generated=output)
            print("Tracker sync skipped. The local tracker changed during the scrape.")
            return

        upload = workspace / f"upload{primary.suffix}"
        shutil.copy2(output, upload)
        published = (*outputs, history) if (workspace / history.name).is_file() else outputs
        _publish_exports(workspace, published)
        print(f"Local tracker saved to {primary}")
        latest = workspace / f"latest{primary.suffix}"
        try:
            remote.fetch(latest)
            if _digest(latest) != drive_hash:
                _save_conflict(path.parent, local=primary, drive=latest)
                raise SyncError("The remote tracker changed during the scrape.")
            remote.publish(upload, first_upload=drive_hash is None)
        except (OSError, SyncError) as exc:
            print(f"Tracker upload skipped or unsuccessful. {exc}")
            return

        _save_checkpoint(path, SyncCheckpoint(last_synced_hash=_digest(upload), last_synced_target=remote.target))
        print("Tracker published successfully.")


class SyncPreference(StrEnum):
    LOCAL = "local"
    DRIVE = "drive"


def sync(
    profile: ProfileOption = None,
    status: Annotated[bool, typer.Option("--status", help="Show sync status and test remote access.")] = False,
    prefer: Annotated[
        SyncPreference | None, typer.Option("--prefer", help="Choose the tracker to use if both copies changed.")
    ] = None,
) -> None:
    """Run the scraper and sync your private tracker with rclone."""
    if prefer is not None and status:
        raise typer.BadParameter("--prefer applies to a sync run.")

    try:
        config = SearchConfig(_profile=profile, _require_search_setup=not status)
        if config.sync is None or not config.sync.enabled:
            if status:
                print("Tracker sync disabled.")
            else:
                print("Tracker sync disabled. Running search and export locally.")
                run(profile=profile)
            return

        if status:
            with TemporaryDirectory() as temp:
                _check_destination(config.sync.target, Path(temp) / f"drive.{config.export.formats[0]}")
            print("Tracker sync enabled. Remote read and folder write checks passed.")
            return

        path = sync_checkpoint_path(config.database_path)
        with _sync_lock(path.parent / "lock"):
            _sync(config, path, config.sync.target, prefer, profile)
    except yaml.YAMLError as exc:
        typer.echo("Tracker sync: Invalid YAML configuration.", err=True)
        raise typer.Exit(1) from exc
    except (OSError, SyncError, ValueError) as exc:
        typer.echo(f"Tracker sync: {exc}", err=True)
        raise typer.Exit(1) from exc
