from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP_DIR = "job-scrapper"


def _base_dir(variable: str, fallback: str) -> Path:
    configured = Path(os.environ.get(variable, ""))
    return configured if configured.is_absolute() else Path.home() / fallback


def user_config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support" / APP_DIR
    if sys.platform == "win32":
        return _base_dir("APPDATA", "AppData/Roaming") / APP_DIR
    return _base_dir("XDG_CONFIG_HOME", ".config") / APP_DIR


def user_data_dir() -> Path:
    if sys.platform == "darwin":
        return user_config_dir()
    if sys.platform == "win32":
        return _base_dir("LOCALAPPDATA", "AppData/Local") / APP_DIR
    return _base_dir("XDG_DATA_HOME", ".local/share") / APP_DIR


def user_log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Logs" / APP_DIR
    if sys.platform == "win32":
        return user_data_dir() / "Logs"
    return _base_dir("XDG_STATE_HOME", ".local/state") / APP_DIR


def _xdg_documents(home: Path) -> Path:
    config = _base_dir("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() != "XDG_DOCUMENTS_DIR":
                continue
            if match := re.match(r'\s*"((?:[^"\\]|\\.)*)"', value):
                value = match[1]
                directory = Path(re.sub(r"\\(.)", r"\1", value.removeprefix("$HOME/")))
                if value.startswith("$HOME/"):
                    directory = home / directory
                if directory.is_absolute():
                    return directory
            break
    except (OSError, ValueError):
        pass
    return home / "Documents"


def _windows_documents(home: Path) -> Path:
    if sys.platform == "win32":
        from ctypes import byref, c_void_p, windll, wstring_at
        from uuid import UUID

        folder_id = UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")  # FOLDERID_Documents
        path = c_void_p()
        try:
            dont_verify = 0x4000  # Allow first use before Documents exists.
            if windll.shell32.SHGetKnownFolderPath(folder_id.bytes_le, dont_verify, None, byref(path)) == 0:
                return Path(wstring_at(path))
        finally:
            windll.ole32.CoTaskMemFree(path)
    return home / "Documents"


def user_export_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        documents = _windows_documents(home)
    elif sys.platform == "darwin":
        documents = home / "Documents"
    else:
        documents = _xdg_documents(home)
    return documents / APP_DIR
