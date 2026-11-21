"""Build the native release assets using the current platform and locked environment."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib.metadata import distributions
from pathlib import Path
from tempfile import TemporaryDirectory

from scrapper import __version__
from scrapper.config.settings import PACKAGE_CONFIG_DIR

ROOT = Path(__file__).resolve().parents[1]
NAME = "job-scrapper"
ARCHITECTURES = {"aarch64": "arm64", "arm64": "arm64", "amd64": "amd64", "x86_64": "amd64"}


def _copy_notices(directory: Path) -> None:
    directory.mkdir()
    shutil.copyfile(ROOT / "LICENSE", directory / "job-scrapper.txt")
    for package in distributions():
        for item in package.files or ():
            if not item.parts[0].endswith(".dist-info") or not item.name.lower().startswith(
                ("license", "copying", "notice", "copyright", "authors")
            ):
                continue
            destination = directory / item
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package.locate_file(item), destination)
    python_lib = Path(os.__file__).parent
    for candidate in (python_lib / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE.txt"):
        if candidate.is_file():
            shutil.copyfile(candidate, directory / "Python.txt")
            break


def _freeze(work: Path, notices: Path, *, onefile: bool) -> Path:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--name",
        NAME,
        "--distpath",
        str(work / "dist"),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work),
        "--onefile" if onefile else "--onedir",
        "--python-option",
        "X utf8",
        "--copy-metadata",
        "scrapper",
        "--collect-submodules",
        "jobspy",
        "--hidden-import",
        "tzdata",
        "--collect-data",
        "certifi",
        "--add-data",
        f"{notices}:notices",
    ]
    for filename in ("config.yaml", "config.prod.yaml", "toulouse-graphiste.yaml"):
        command.extend(("--add-data", f"{PACKAGE_CONFIG_DIR / filename}:scrapper/config"))
    subprocess.run([*command, str(ROOT / "main.py")], cwd=work, check=True)
    return work / "dist" / (f"{NAME}.exe" if onefile else NAME)


def _build_deb(bundle: Path, work: Path, destination: Path, arch: str) -> None:
    package = work / "deb"
    control = package / "DEBIAN"
    control.mkdir(parents=True)
    (package / "usr/bin").mkdir(parents=True)
    (package / "usr/bin" / NAME).symlink_to(f"../lib/{NAME}/{NAME}")
    shutil.copytree(bundle, package / "usr/lib" / NAME, symlinks=True)
    docs = package / "usr/share/doc" / NAME
    docs.mkdir(parents=True)
    shutil.copyfile(ROOT / "LICENSE", docs / "copyright")
    _, glibc = os.confstr("CS_GNU_LIBC_VERSION").split()
    size_kib = (
        sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file() and not path.is_symlink()) + 1023
    ) // 1024
    (control / "control").write_text(
        f"Package: {NAME}\n"
        f"Version: {__version__}\n"
        f"Architecture: {arch}\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Maintainer: Job Scrapper contributors <MandalorianY@users.noreply.github.com>\n"
        f"Depends: libc6 (>= {glibc})\n"
        f"Installed-Size: {size_kib}\n"
        "Homepage: https://github.com/MandalorianY/job-scrapper\n"
        "Description: Job scraper and Excel application tracker\n",
        encoding="utf-8",
    )
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(package), str(destination)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    output = parser.parse_args().output.resolve()
    arch = ARCHITECTURES.get(platform.machine().lower())
    if arch is None or sys.platform not in {"win32", "linux", "darwin"}:
        parser.error("The native build requires Windows, Linux or macOS on x64 or ARM64.")
    if sys.platform != "win32":
        os.umask(0o022)
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f"{NAME}-build-") as temporary:
        work = Path(temporary)
        notices = work / "notices"
        _copy_notices(notices)
        bundle = _freeze(work, notices, onefile=sys.platform == "win32")
        if sys.platform == "win32":
            prefix = f"{NAME}-{__version__}-windows-{arch}"
            with zipfile.ZipFile(output / f"{prefix}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(bundle, f"{prefix}.exe")
                for path in sorted(notices.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(work))
        else:
            platform_name = "linux" if sys.platform == "linux" else "macos"
            prefix = f"{NAME}-{__version__}-{platform_name}-{arch}"
            with tarfile.open(output / f"{prefix}.tar.gz", "w:gz") as archive:
                archive.add(bundle, arcname=NAME)
            if sys.platform == "linux":
                _build_deb(bundle, work, output / f"{NAME}_{__version__}_{arch}.deb", arch)


if __name__ == "__main__":
    main()
