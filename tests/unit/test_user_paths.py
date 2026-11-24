from pathlib import Path
from types import SimpleNamespace

import pytest

from jobsfr.config import SearchConfig, settings, user_paths


@pytest.mark.parametrize(
    ("platform", "config_dir", "data_dir", "documents", "logs"),
    [
        ("darwin", ".config", "Library/Application Support", "Documents", "Library/Logs"),
        ("linux", "XDG config", "XDG data", "Mes documents $", "XDG state"),
        ("win32", "Roaming data", "Local data", "Redirected Documents", "Local data"),
    ],
)
def test_installed_defaults_follow_platform_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    config_dir: str,
    data_dir: str,
    documents: str,
    logs: str,
) -> None:
    monkeypatch.setattr(settings, "PROJECT_ROOT", None)
    monkeypatch.setattr(user_paths, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(user_paths, "_windows_documents", lambda home: home / "Redirected Documents")
    for name, folder in {
        "APPDATA": "Roaming data",
        "LOCALAPPDATA": "Local data",
        "XDG_CONFIG_HOME": "XDG config",
        "XDG_DATA_HOME": "XDG data",
        "XDG_STATE_HOME": "XDG state",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / folder))
    linux_config = tmp_path / "XDG config"
    linux_config.mkdir()
    (linux_config / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Mes documents \\$"\n', encoding="utf-8")

    log_dir = tmp_path / logs / "jobsfr"
    if platform == "win32":
        log_dir /= "Logs"
    config = SearchConfig()
    assert settings.default_local_config() == tmp_path / config_dir / "jobsfr/config.yaml"
    assert config.database_path == tmp_path / data_dir / "jobsfr/job_search.duckdb"
    assert config.export_dir == tmp_path / documents / "jobsfr"
    assert config.log_file == log_dir / "jobsfr.jsonl"


def test_linux_ignores_relative_xdg_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(user_paths, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    assert user_paths.user_config_dir() == tmp_path / ".config/jobsfr"
    assert user_paths.user_data_dir() == tmp_path / ".local/share/jobsfr"
    assert user_paths.user_log_dir() == tmp_path / ".local/state/jobsfr"
    assert user_paths.user_export_dir() == tmp_path / "Documents/jobsfr"
