"""Discovery of the external gotohp and exiftool binaries.

Both are platform-specific native executables, so they are located at runtime
rather than vendored. This keeps the same checkout working on the Windows
development machine and the Linux VM.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Release asset names published by github.com/xob0t/gotohp, by platform.
GOTOHP_ASSETS = {
    "win32": "gotohp-cli-x64.exe",
    "linux": "gotohp-cli_amd64",
    "darwin": "gotohp-cli-macos-universal",
}

_GOTOHP_NAMES = (
    "gotohp-cli",
    "gotohp-cli.exe",
    "gotohp-cli_amd64",
    "gotohp-cli-x64.exe",
    "gotohp-cli-macos-universal",
)


def project_bin_dir() -> Path:
    """Return the repository-local ``bin/`` directory."""
    return Path(__file__).resolve().parents[2] / "bin"


def find_gotohp(explicit: Path | None = None) -> Path | None:
    """Locate the gotohp CLI.

    Search order: explicit setting, ``PATH``, then the repository ``bin/``
    directory populated by ``scripts/fetch-gotohp.py``.
    """
    if explicit is not None:
        return explicit if explicit.exists() else None

    for name in _GOTOHP_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)

    bin_dir = project_bin_dir()
    for name in _GOTOHP_NAMES:
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def default_gotohp_config(os_name: str | None = None) -> Path | None:
    """Return gotohp's own config path, where its credentials live.

    gotohp writes to the OS config directory, or to a file beside its executable.
    Knowing this lets `i2g doctor` tell you whether credentials are present.

    Args:
        os_name: Override for ``os.name``, so both branches are testable from
            either platform.
    """
    if (os_name or os.name) == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "gotohp" / "gotohp.config"
        return None

    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gotohp" / "gotohp.config"
