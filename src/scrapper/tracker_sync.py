"""Rclone sync for the primary application tracker."""

from __future__ import annotations

import argparse
import errno
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp
from uuid import uuid4

from pydantic import BaseModel

from scrapper.cli import run
from scrapper.config import SearchConfig
from scrapper.export import export_paths
from scrapper.export.jobs import _publish_exports, read_tracker, tracker_paths
from scrapper.export.tracker import LOCALIZED_TRACKER_COLUMNS


class SyncError(Exception):
    pass


class SyncSettings(BaseModel):
    target: str | None = None
    enabled: bool = False
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
        hint = f"Set sync.target in YAML or use --enable REMOTE:folder/jobs{suffix}."
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
        probe.write_text("job-scrapper sync check\n", encoding="utf-8")
        download = workspace / "probe-readback.txt"
        separator = "" if self.directory.endswith(":") else "/"
        remote_probe = f"{self.directory}{separator}.job-scrapper-check-{uuid4().hex}.txt"
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
        tracker = read_tracker(path)
    except Exception as exc:
        raise SyncError(f"Could not open the remote tracker: {exc}") from exc
    if path.suffix == ".csv" and not set(LOCALIZED_TRACKER_COLUMNS).intersection(tracker.columns):
        raise SyncError("The remote CSV has no tracker columns. Use jobs.csv as the sync target.")


def _check_destination(target: str | None, incoming: Path) -> Remote:
    remote = Remote.connect(target, incoming.suffix)
    remote.fetch(incoming)
    _check_tracker(incoming)
    try:
        remote.check_write(incoming.parent)
    except (OSError, SyncError) as exc:
        raise SyncError(f"Could not verify write access to the tracker folder. {exc}") from exc
    return remote


def sync_settings_path(database_path: Path) -> Path:
    return database_path.parent / ".tracker-sync" / "settings.json"


@contextmanager
def _sync_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as lock:
        if sys.platform == "win32":
            import msvcrt

            lock.seek(0)
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as exc:
                    # The Windows blocking mode gives up after ten attempts.
                    if exc.errno != errno.EDEADLK:
                        raise
            try:
                yield
            finally:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def load_sync_settings(path: Path) -> SyncSettings:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SyncSettings()
    return SyncSettings.model_validate_json(contents)


def _save_settings(path: Path, settings: SyncSettings) -> None:
    with TemporaryDirectory(dir=path.parent) as temp:
        draft = Path(temp) / path.name
        draft.write_text(settings.model_dump_json(indent=2) + "\n", encoding="utf-8")
        draft.replace(path)


def _local_tracker(config: SearchConfig) -> Path:
    candidates = tracker_paths(config.export_dir, config.export.formats)
    return next((path for path in candidates if path.is_file()), candidates[0])


def _save_conflict(output_dir: Path, **copies: Path) -> None:
    backup = Path(mkdtemp(prefix="conflict-", dir=output_dir))
    for label, path in copies.items():
        if path.is_file():
            shutil.copy2(path, backup / f"{label}{path.suffix}")
    print(f"Tracker copies saved to {backup}")


def _sync(config: SearchConfig, path: Path, settings: SyncSettings, preference: str | None) -> None:
    sync_config = config.sync or settings
    if not sync_config.enabled:
        print("Tracker sync disabled. Running local export.")
        run()
        return
    outputs = export_paths(config.export_dir, config.export.formats)
    primary = outputs[0]
    config.export_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tracker-", dir=config.export_dir) as temp:
        workspace = Path(temp)
        incoming = workspace / f"drive{primary.suffix}"
        try:
            remote = _check_destination(sync_config.target, incoming)
        except (OSError, SyncError) as exc:
            print(f"Tracker sync skipped. {exc} Running local export.", file=sys.stderr, flush=True)
            run()
            return

        local = _local_tracker(config)
        local_hash, drive_hash = _digest(local), _digest(incoming)
        checkpoint = settings.checkpoint_for(remote.target)
        diverged = (drive_hash is None and checkpoint is not None) or (
            local_hash is not None
            and drive_hash is not None
            and local_hash != drive_hash
            and checkpoint not in (local_hash, drive_hash)
        )
        inputs = {"local": local, "drive": incoming}
        if diverged:
            _save_conflict(path.parent, local=local, drive=incoming)
            if preference is None or not inputs[preference].is_file():
                print("Tracker sync skipped. Review both copies, then rerun with --prefer local or --prefer drive.")
                run()
                return
            seed = inputs[preference]
        else:
            seed = incoming if drive_hash is not None and local_hash in (None, checkpoint) else local

        if seed.is_file():
            shutil.copy2(seed, workspace / (primary.name if seed == incoming else seed.name))
        run(db=config.database_path, export_dir=workspace)
        output = workspace / primary.name
        if _digest(_local_tracker(config)) != local_hash:
            _save_conflict(path.parent, local=_local_tracker(config), generated=output)
            print("Tracker sync skipped. The local tracker changed during the scrape.")
            return

        upload = workspace / f"upload{primary.suffix}"
        shutil.copy2(output, upload)
        _publish_exports(workspace, outputs)
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

        settings.last_synced_hash = _digest(upload)
        settings.last_synced_target = remote.target
        _save_settings(path, settings)
        print("Tracker published successfully.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="job-scrapper sync", description="Run the scraper and sync your private tracker."
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Show sync status and test remote access.")
    action.add_argument(
        "--enable", nargs="?", const="", metavar="REMOTE:TRACKER", help="Check and enable a destination outside YAML."
    )
    action.add_argument("--disable", action="store_true", help="Disable tracker sync outside YAML.")
    parser.add_argument("--prefer", choices=("local", "drive"), help="Choose the seed if both trackers have changed.")
    args = parser.parse_args(argv)
    if args.prefer and (args.status or args.enable is not None or args.disable):
        parser.error("--prefer applies to a sync run.")

    try:
        config = SearchConfig()
        suffix = f".{config.export.formats[0]}"
        path = sync_settings_path(config.database_path)
        if config.sync is not None and (args.enable is not None or args.disable):
            raise SyncError("Edit sync.enabled and sync.target in your YAML to change tracker sync.")
        if args.status:
            sync_config = config.sync or load_sync_settings(path)
            if sync_config.enabled:
                with TemporaryDirectory() as temp:
                    _check_destination(sync_config.target, Path(temp) / f"drive{suffix}")
                print("Tracker sync enabled. Remote read and folder write checks passed.")
            else:
                print("Tracker sync disabled.")
            return

        with _sync_lock(path.parent / "lock"):
            settings = load_sync_settings(path)
            if args.enable is not None:
                with TemporaryDirectory() as temp:
                    remote = _check_destination(args.enable or settings.target, Path(temp) / f"drive{suffix}")
                checkpoint = settings.checkpoint_for(remote.target)
                _save_settings(
                    path,
                    SyncSettings(
                        target=remote.target,
                        enabled=True,
                        last_synced_hash=checkpoint,
                        last_synced_target=remote.target,
                    ),
                )
                print("Tracker sync enabled. Remote read and folder write checks passed.")
            elif args.disable:
                settings.enabled = False
                _save_settings(path, settings)
                print("Tracker sync disabled.")
            else:
                _sync(config, path, settings, args.prefer)
    except (OSError, SyncError, ValueError) as exc:
        parser.exit(1, f"Tracker sync: {exc}\n")


if __name__ == "__main__":
    main()
