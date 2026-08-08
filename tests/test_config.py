"""Tests for settings loading and path derivation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from icloud_to_gphotos.config import GiB, Settings, default_state_dir, load_settings


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("icloud_username", "test@example.com")
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_username_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_staging_dir_defaults_under_the_state_dir(tmp_path: Path) -> None:
    settings = _settings(state_dir=tmp_path / "state")

    assert settings.staging_dir == tmp_path / "state" / "staging"
    assert settings.media_staging_dir == tmp_path / "state" / "staging" / "media"
    assert settings.edited_staging_dir == tmp_path / "state" / "staging" / "edited"


def test_explicit_staging_dir_is_respected(tmp_path: Path) -> None:
    settings = _settings(state_dir=tmp_path / "state", staging_dir=tmp_path / "bigdisk")

    assert settings.staging_dir == tmp_path / "bigdisk"
    assert settings.media_staging_dir == tmp_path / "bigdisk" / "media"


def test_derived_paths_hang_off_the_state_dir(tmp_path: Path) -> None:
    settings = _settings(state_dir=tmp_path / "state")

    assert settings.cookie_dir == tmp_path / "state" / "cookies"
    assert settings.ledger_path == tmp_path / "state" / "ledger.db"
    assert settings.log_dir == tmp_path / "state" / "logs"
    assert settings.report_dir == tmp_path / "state" / "reports"


def test_ensure_dirs_creates_everything_needed(tmp_path: Path) -> None:
    settings = _settings(state_dir=tmp_path / "state")

    settings.ensure_dirs()

    for path in (
        settings.state_dir,
        settings.cookie_dir,
        settings.log_dir,
        settings.report_dir,
        settings.media_staging_dir,
        settings.edited_staging_dir,
    ):
        assert path.is_dir()


def test_ensure_dirs_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(state_dir=tmp_path / "state")

    settings.ensure_dirs()
    settings.ensure_dirs()

    assert settings.state_dir.is_dir()


def test_blank_optional_paths_become_none() -> None:
    """A commented-out or empty .env entry must not turn into Path('.')."""
    settings = _settings(gotohp_binary="", exiftool_binary="   ", gotohp_config="")

    assert settings.gotohp_binary is None
    assert settings.exiftool_binary is None
    assert settings.gotohp_config is None


def test_paths_expand_the_home_tilde() -> None:
    settings = _settings(gotohp_binary="~/bin/gotohp-cli")

    assert settings.gotohp_binary is not None
    assert "~" not in str(settings.gotohp_binary)


def test_defaults_are_conservative() -> None:
    settings = _settings()

    assert settings.batch_max_bytes == 20 * GiB
    assert settings.batch_max_items == 500
    assert settings.delete_grace_days == 7
    assert settings.delete_from_icloud is True
    assert settings.edited_policy == "both"
    assert settings.backfill_metadata is True
    assert settings.include_live_photo_video is True
    assert settings.pair_live_photos is True
    assert settings.include_alternative_original is False
    assert settings.ignore_apple_metadata is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("batch_max_bytes", 0),
        ("batch_max_items", 0),
        ("delete_grace_days", -1),
        ("disk_headroom_bytes", -1),
        ("upload_threads", 0),
        ("upload_threads", 99),
        ("download_workers", 0),
        ("edited_policy", "nonsense"),
        ("log_level", "CHATTY"),
    ],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_environment_variables_are_read_with_the_i2g_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("I2G_ICLOUD_USERNAME", "env@example.com")
    monkeypatch.setenv("I2G_DELETE_GRACE_DAYS", "30")
    monkeypatch.setenv("I2G_DELETE_FROM_ICLOUD", "false")
    monkeypatch.setenv("I2G_STATE_DIR", str(tmp_path / "envstate"))

    settings = load_settings()

    assert settings.icloud_username == "env@example.com"
    assert settings.delete_grace_days == 30
    assert settings.delete_from_icloud is False
    assert settings.state_dir == tmp_path / "envstate"


def test_explicit_overrides_beat_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I2G_ICLOUD_USERNAME", "env@example.com")

    settings = load_settings(icloud_username="explicit@example.com")

    assert settings.icloud_username == "explicit@example.com"


def test_default_state_dir_is_platform_appropriate() -> None:
    result = default_state_dir()

    assert result.name == "icloud-to-gphotos"
    assert result.is_absolute()


def test_default_state_dir_follows_xdg_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/var/lib/state")

    assert default_state_dir("posix") == Path("/var/lib/state") / "icloud-to-gphotos"


def test_default_state_dir_uses_localappdata_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))

    result = default_state_dir("nt")

    assert result.name == "icloud-to-gphotos"
    assert "Local" in result.parts
