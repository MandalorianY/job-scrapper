import csv
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="Requires the Unix Docker launcher")
def test_docker_launcher_maps_host_paths_and_reuses_replay_destination(tmp_path: Path) -> None:
    checkout = tmp_path / 'checkout, with "quotes"'
    deploy = checkout / "deploy"
    deploy.mkdir(parents=True)
    launcher = deploy / "run-docker.sh"
    shutil.copyfile(Path(__file__).parents[2] / "deploy/run-docker.sh", launcher)
    profile = tmp_path / "search profile.yaml"
    profile.touch()
    external = tmp_path / "external output"
    external.mkdir()
    archive = external / "jobs.duckdb"
    archive.touch()
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nif [ "$1" = build ]; then echo fixture-image; else printf "%s\\0" "$@"; fi\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)

    result = subprocess.run(
        [
            "bash",
            str(launcher),
            f"--profile={profile.name}",
            "--replay-from",
            str(archive),
            "--db",
            str(archive),
            "--log-file",
            "data/custom.jsonl",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )
    args = result.stdout.rstrip("\0").split("\0")
    volumes = [
        dict(part.split("=", 1) for part in next(csv.reader([args[i + 1]])) if "=" in part)
        for i, arg in enumerate(args)
        if arg == "--mount"
    ]
    by_source = {volume["src"]: volume["dst"] for volume in volumes}
    assert by_source[str((checkout / "data").resolve())] == "/work/data"
    assert f"--profile={by_source[str(profile.resolve())]}" in args
    database = f"{by_source[str(external.resolve())]}/{archive.name}"
    assert args[args.index("--replay-from") + 1] == args[args.index("--db") + 1] == database
    assert args[args.index("--log-file") + 1] == "/work/data/custom.jsonl"
