"""Tests for locating the external gotohp binary across platforms."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from icloud_to_gphotos import binaries
from icloud_to_gphotos.binaries import (
    GOTOHP_ASSETS,
    default_gotohp_config,
    find_gotohp,
    project_bin_dir,
)


def test_explicit_path_wins_when_it_exists(tmp_path: Path) -> None:
    binary = tmp_path / "gotohp-cli"
    binary.write_text("#!/bin/sh")

    assert find_gotohp(binary) == binary


def test_explicit_path_that_does_not_exist_returns_none(tmp_path: Path) -> None:
    """Fail closed rather than silently falling back to some other binary the
    operator did not ask for."""
    assert find_gotohp(tmp_path / "missing") is None


def test_path_lookup_is_used_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    found = tmp_path / "gotohp-cli"
    found.write_text("#!/bin/sh")
    monkeypatch.setattr(
        shutil, "which", lambda name: str(found) if name == "gotohp-cli" else None
    )

    assert find_gotohp(None) == found


@pytest.mark.parametrize("name", ["gotohp-cli", "gotohp-cli.exe", "gotohp-cli_amd64"])
def test_project_bin_directory_is_searched(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Linux and Windows release assets have different names, so all of them
    must be discoverable from ./bin."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / name).write_text("#!/bin/sh")
    monkeypatch.setattr(binaries, "project_bin_dir", lambda: bin_dir)

    assert find_gotohp(None) == bin_dir / name


def test_returns_none_when_nothing_is_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(binaries, "project_bin_dir", lambda: tmp_path / "empty")

    assert find_gotohp(None) is None


def test_project_bin_dir_is_beside_the_repo_root() -> None:
    result = project_bin_dir()

    assert result.name == "bin"
    assert (result.parent / "pyproject.toml").exists()


def test_release_assets_cover_the_platforms_we_support() -> None:
    assert set(GOTOHP_ASSETS) == {"win32", "linux", "darwin"}


def test_default_gotohp_config_follows_xdg_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/me/.config")

    result = default_gotohp_config("posix")

    assert result == Path("/home/me/.config") / "gotohp" / "gotohp.config"


def test_default_gotohp_config_falls_back_to_home_without_xdg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    result = default_gotohp_config("posix")

    assert result is not None
    assert result.parts[-3:] == (".config", "gotohp", "gotohp.config")


def test_default_gotohp_config_uses_appdata_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))

    result = default_gotohp_config("nt")

    assert result is not None
    assert result.parts[-2:] == ("gotohp", "gotohp.config")


def test_default_gotohp_config_is_none_without_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPDATA", raising=False)

    assert default_gotohp_config("nt") is None
