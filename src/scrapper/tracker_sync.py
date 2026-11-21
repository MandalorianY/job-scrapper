"""Rclone sync for the Excel application tracker."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from uuid import uuid4

from openpyxl import Workbook, load_workbook
from pydantic import BaseModel

from scrapper.cli import run
from scrapper.config import SearchConfig
from scrapper.export import _publish_exports


class SyncError(Exception):
    pass


class SyncSettings(BaseModel):
    target: str | None = None
    enabled: bool = False
    last_synced_hash: str | None = None


@dataclass(frozen=True)
class Remote:
    executable: str
    target: str

    @classmethod
    def connect(cls, target: str | None) -> Remote:
        executable = shutil.which("rclone")
        if executable is None:
            raise SyncError("Please install rclone, then run rclone config to set up a remote.")
        if not target:
            raise SyncError("Set a destination with --enable REMOTE:path/workbook.xlsx.")
        name, separator, path = target.partition(":")
        if (
            not separator
            or not name
            or target.startswith("-")
            or path.endswith("/")
            or Path(path).suffix.lower() != ".xlsx"
        ):
            raise SyncError("Use a named rclone remote and an .xlsx path, such as REMOTE:folder/workbook.xlsx.")
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
            result = subprocess.run([self.executable, *args], capture_output=True, text=True, timeout=120, check=False)
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
                raise SyncError("The remote workbook path did not resolve to a file.")
            return
        listing = self.call("lsf", self.directory, "--files-only", "--max-depth", "1")
        name = self.target.partition(":")[2].rsplit("/", 1)[-1]
        if listing is None or name in listing.splitlines():
            raise SyncError("Could not read the remote workbook.")

    def check_write(self, workspace: Path) -> None:
        probe = workspace / "probe.xlsx"
        Workbook().save(probe)
        download = workspace / "probe-readback.xlsx"
        separator = "" if self.directory.endswith(":") else "/"
        remote_probe = f"{self.directory}{separator}.job-scrapper-check-{uuid4().hex}.xlsx"
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


def _check_workbook(path: Path) -> None:
    if not path.exists():
        return
    try:
        with path.open("rb") as file:
            workbook = load_workbook(file, read_only=True)
            try:
                if "Offres" not in workbook.sheetnames:
                    raise SyncError("The remote workbook has no Offres sheet.")
            finally:
                workbook.close()
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f"Could not open the remote workbook: {exc}") from exc


def sync_settings_path(database_path: Path) -> Path:
    return database_path.parent / ".tracker-sync" / "settings.json"


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
    return config.excel_path if config.excel_path.exists() else config.csv_path


def _save_conflict(output_dir: Path, **copies: Path) -> None:
    backup = Path(mkdtemp(prefix="conflict-", dir=output_dir))
    for label, path in copies.items():
        if path.is_file():
            shutil.copy2(path, backup / f"{label}{path.suffix}")
    print(f"Tracker copies saved to {backup}")


def _sync(config: SearchConfig, path: Path, settings: SyncSettings, preference: str | None) -> None:
    if not settings.enabled:
        print("Tracker sync disabled. Running local export.")
        run()
        return

    config.export_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tracker-", dir=config.export_dir) as temp:
        workspace = Path(temp)
        incoming = workspace / "drive.xlsx"
        try:
            remote = Remote.connect(settings.target)
            remote.fetch(incoming)
            _check_workbook(incoming)
        except (OSError, SyncError) as exc:
            print(f"Tracker sync skipped. {exc}")
            run()
            return

        local = _local_tracker(config)
        local_hash, drive_hash = _digest(local), _digest(incoming)
        checkpoint = settings.last_synced_hash
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
            name = config.csv_path.name if seed.suffix == ".csv" else config.excel_path.name
            shutil.copy2(seed, workspace / name)
        run(db=config.database_path, export_dir=workspace)
        output = workspace / config.excel_path.name
        if _digest(_local_tracker(config)) != local_hash:
            _save_conflict(path.parent, local=_local_tracker(config), generated=output)
            print("Tracker sync skipped. The local workbook changed during the scrape.")
            return

        upload = workspace / "upload.xlsx"
        shutil.copy2(output, upload)
        _publish_exports(workspace, (config.csv_path, config.raw_csv_path, config.excel_path))
        print(f"Local workbook saved to {config.excel_path}")
        latest = workspace / "latest.xlsx"
        try:
            remote.fetch(latest)
            if _digest(latest) != drive_hash:
                _save_conflict(path.parent, local=config.excel_path, drive=latest)
                raise SyncError("The remote workbook changed during the scrape.")
            remote.publish(upload, first_upload=drive_hash is None)
        except (OSError, SyncError) as exc:
            print(f"Tracker upload skipped or unsuccessful. {exc}")
            return

        settings.last_synced_hash = _digest(upload)
        _save_settings(path, settings)
        print("Tracker workbook published successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the scraper and sync your private tracker.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Show the private sync setting and rclone availability.")
    action.add_argument(
        "--enable", nargs="?", const="", metavar="REMOTE:WORKBOOK", help="Check and enable a destination."
    )
    action.add_argument("--disable", action="store_true", help="Disable tracker sync.")
    parser.add_argument("--prefer", choices=("local", "drive"), help="Choose the seed if both workbooks have changed.")
    args = parser.parse_args()
    if args.prefer and (args.status or args.enable is not None or args.disable):
        parser.error("--prefer applies to a sync run.")

    config = SearchConfig()
    path = sync_settings_path(config.database_path)
    try:
        if args.status:
            settings = load_sync_settings(path)
            if settings.enabled:
                Remote.connect(settings.target)
                print("Tracker sync enabled. The rclone remote is configured.")
            else:
                print("Tracker sync disabled.")
            return

        import fcntl

        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / "lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            settings = load_sync_settings(path)
            if args.enable is not None:
                remote = Remote.connect(args.enable or settings.target)
                with TemporaryDirectory() as temp:
                    workspace = Path(temp)
                    remote.fetch(workspace / "drive.xlsx")
                    _check_workbook(workspace / "drive.xlsx")
                    remote.check_write(workspace)
                checkpoint = settings.last_synced_hash if settings.target == remote.target else None
                _save_settings(path, SyncSettings(target=remote.target, enabled=True, last_synced_hash=checkpoint))
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
