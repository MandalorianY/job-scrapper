import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

import pandas as pd
import pytest
from openpyxl import load_workbook

from scrapper import tracker_sync
from scrapper.config import SearchConfig
from scrapper.config.settings import ExportFormat
from scrapper.export import export_job_formats, export_paths
from scrapper.export.jobs import read_tracker
from scrapper.models import JobOffer
from scrapper.storage import JobStore


@pytest.fixture
def remote_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    directory = tmp_path / "remote"
    directory.mkdir()

    def resolve(value: str) -> Path:
        return directory / value.removeprefix("fixture:") if value.startswith("fixture:") else Path(value)

    def call(_remote: tracker_sync.Remote, action: str, *args: str, allow_missing: bool = False) -> str | None:
        if action == "listremotes":
            return "fixture:\n"
        if action == "lsf":
            return "\n".join(path.name for path in resolve(args[0]).iterdir())
        if action == "deletefile":
            resolve(args[0]).unlink()
            return ""
        assert action == "copyto"
        source, target = map(resolve, args[:2])
        if allow_missing and not source.is_file():
            return None
        if "--immutable" in args[2:]:
            assert not target.exists()
        shutil.copy2(source, target)
        return ""

    monkeypatch.setattr(tracker_sync.shutil, "which", lambda _: "rclone")
    monkeypatch.setattr(tracker_sync.Remote, "call", call)
    return directory


def _edit_status(path: Path, status: str) -> None:
    if path.suffix == ".xlsx":
        workbook = load_workbook(path)
        try:
            workbook["Offres"]["A2"] = status
            workbook.save(path)
        finally:
            workbook.close()
        return
    tracker = read_tracker(path)
    tracker.at[0, "Statut"] = status
    if path.suffix == ".ods":
        with pd.ExcelWriter(path, engine="odf") as writer:
            tracker.to_excel(writer, sheet_name="Offres", index=False)
    else:
        tracker.to_csv(path, index=False)


def _assert_statuses(paths: Sequence[Path], expected: dict[str, str]) -> None:
    for path in paths:
        tracker = read_tracker(path)
        assert len(tracker) == len(expected), path
        assert tracker.set_index("Intitulé")["Statut"].to_dict() == expected, path


def test_sync_lock_excludes_a_second_file_handle(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with tracker_sync._sync_lock(path), path.open("ab") as competing, pytest.raises(OSError):
        if sys.platform == "win32":
            import msvcrt

            competing.seek(0)
            msvcrt.locking(competing.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("kind", ("xlsx", "ods", "csv"))
def test_tracker_sync_saves_incremental_updates_and_recovers_from_conflicts(
    kind: ExportFormat, remote_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind == "ods":
        pytest.importorskip("odf")
    formats = ("xlsx", "csv") if kind == "xlsx" else (kind, "xlsx")
    config = SearchConfig(
        paths={"database_path": tmp_path / "jobs.duckdb", "export_dir": tmp_path / "exports"},
        export={"formats": formats, "excel_columns": ("Statut", "Lien", "Intitulé")},
    )
    store = JobStore(config.database_path)
    try:
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.com/jobs/1",
                title="Example coordinator",
                company="Example",
                location="Toulouse",
                description="Full-time role coordinating projects.",
            )
        )
        paths, _ = export_job_formats(store, tmp_path / "seed", config.export.excel_columns, formats=(kind,))
    finally:
        store.close()
    remote = remote_dir / f"tracker.{kind}"
    shutil.copy2(paths[0], remote)
    _edit_status(remote, "Postulé")

    def run(*, db: Path | None = None, export_dir: Path | None = None) -> None:
        store = JobStore(db or config.database_path)
        try:
            export_job_formats(
                store, export_dir or config.export_dir, config.export.excel_columns, formats=config.export.formats
            )
        finally:
            store.close()

    monkeypatch.setattr(tracker_sync, "SearchConfig", lambda: config)
    monkeypatch.setattr(tracker_sync, "run", run)
    monkeypatch.setattr(sys, "argv", ["tracker_sync", "--enable", f"fixture:{remote.name}"])
    tracker_sync.main()
    assert {path.name for path in remote_dir.iterdir()} == {remote.name}

    monkeypatch.setattr(sys, "argv", ["tracker_sync"])
    tracker_sync.main()
    outputs = export_paths(config.export_dir, config.export.formats)
    assert {path.name for path in outputs} == {path.name for path in config.export_dir.iterdir()}
    trackers = [path for path in outputs if path.name != "jobs_raw.csv"]
    _assert_statuses(trackers, {"Example coordinator": "Postulé"})
    assert outputs[0].read_bytes() == remote.read_bytes()

    _edit_status(outputs[0], "Refusé")
    store = JobStore(config.database_path)
    try:
        new_offer = JobOffer(
            source="example",
            source_url="https://example.com/jobs/2",
            title="Example analyst",
            company="Another example",
            location="Toulouse",
            description="Full-time role analyzing financial reports.",
        )
        store.upsert_offer(new_offer)
        store.upsert_offer(new_offer)
    finally:
        store.close()
    expected = {"Example coordinator": "Refusé", "Example analyst": "En Attente"}
    settings_path = tracker_sync.sync_settings_path(config.database_path)
    checkpoint = tracker_sync.load_sync_settings(settings_path).last_synced_hash

    def fail_upload(_remote: tracker_sync.Remote, _path: Path, *, first_upload: bool) -> Never:
        assert not first_upload
        raise tracker_sync.SyncError("Simulated upload failure")

    with monkeypatch.context() as failed:
        failed.setattr(tracker_sync.Remote, "publish", fail_upload)
        tracker_sync.main()
    _assert_statuses(trackers, expected)
    _assert_statuses([remote], {"Example coordinator": "Postulé"})
    assert tracker_sync.load_sync_settings(settings_path).last_synced_hash == checkpoint

    tracker_sync.main()
    _assert_statuses([*trackers, remote], expected)
    tracker_sync.main()
    _assert_statuses([*trackers, remote], expected)
    assert {path.name for path in remote_dir.iterdir()} == {remote.name}

    edited_title = read_tracker(outputs[0]).at[0, "Intitulé"]
    _edit_status(outputs[0], "Postulé")
    _edit_status(remote, "En Attente" if expected[edited_title] != "En Attente" else "Refusé")
    remote_before_conflict = remote.read_bytes()
    remote_statuses = read_tracker(remote).set_index("Intitulé")["Statut"].to_dict()
    tracker_sync.main()
    assert remote.read_bytes() == remote_before_conflict
    (backup,) = settings_path.parent.glob("conflict-*")
    _assert_statuses([backup / f"local.{kind}"], {**expected, edited_title: "Postulé"})
    _assert_statuses([backup / f"drive.{kind}"], remote_statuses)

    monkeypatch.setattr(sys, "argv", ["tracker_sync", "--prefer", "drive"])
    tracker_sync.main()
    _assert_statuses([*trackers, remote], remote_statuses)


def test_remote_extension_must_match_primary_format(remote_dir: Path) -> None:
    with pytest.raises(tracker_sync.SyncError, match=r"--enable REMOTE:folder/jobs\.ods"):
        tracker_sync.Remote.connect("fixture:jobs.xlsx", ".ods")
    assert not list(remote_dir.iterdir())


def test_windows_drive_letter_cannot_be_used_as_remote(remote_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(tracker_sync.SyncError, match="avoid drive letters"):
        tracker_sync.Remote.connect(r"C:\jobs.xlsx", ".xlsx")


def test_raw_csv_cannot_be_used_as_remote_tracker(tmp_path: Path) -> None:
    path = tmp_path / "jobs_raw.csv"
    pd.DataFrame({"title": ["Example coordinator"], "source": ["example"]}).to_csv(path, index=False)
    with pytest.raises(tracker_sync.SyncError, match="Use jobs.csv"):
        tracker_sync._check_tracker(path)
