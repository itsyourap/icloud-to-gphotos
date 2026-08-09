#!/usr/bin/env python3
"""Provide a gotohp CLI binary this project can actually drive.

The published v0.8.1 release cannot be used. It predates two commits on ``main``
that this project depends on:

* ``2ae4fe64 fix: support headless CLI uploads`` — adds ``--no-tui``. Without it
  bubbletea opens ``/dev/tty`` for input, which fails with ENXIO under systemd
  (no controlling terminal) and loses the whole batch after downloading it.
* ``e6e71a40 Support paired Apple Live Photo uploads`` — adds
  ``--pair-live-photos``, without which a Live Photo's HEIC and MOV arrive in
  Google Photos as two unrelated items.

gotohp ignores unknown flags rather than rejecting them, and builds from ``main``
still report ``v0.8.1``, so neither behaviour nor version reveals the difference.
This script therefore builds from a pinned commit by default and verifies the
result exposes the flags before declaring success.

    uv run python scripts/fetch_gotohp.py              # build the pinned commit
    uv run python scripts/fetch_gotohp.py --ref main   # build current main
    uv run python scripts/fetch_gotohp.py --release    # download latest release

Re-check whether a newer release has landed with:
    uv run python scripts/fetch_gotohp.py --release && uv run i2g doctor
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = "xob0t/gotohp"

#: Pinned so builds are reproducible. Bump deliberately after reviewing changes.
#: 20583b88 "fix: cap QuickTime container nesting (#77)", 2026-07-30.
PINNED_REF = "20583b8815255f37cec459d486147371281c1e0e"

#: Release asset names, used only by --release.
RELEASE_ASSETS = {
    "win32": "gotohp-cli-x64.exe",
    "linux": "gotohp-cli_amd64",
    "darwin": "gotohp-cli-macos-universal",
}

#: Output name per platform, matching what binaries.find_gotohp looks for.
OUTPUT_NAMES = {
    "win32": "gotohp-cli-x64.exe",
    "linux": "gotohp-cli_amd64",
    "darwin": "gotohp-cli-macos-universal",
}

#: The upstream CI build flags (.github/workflows/build.yml). CGO_ENABLED=0
#: keeps it pure Go, so it also cross-compiles.
BUILD_FLAGS = ["-tags", "cli", "-trimpath", "-buildvcs=false", "-ldflags", "-w -s"]

#: gotohp's go.mod requires this. Debian's golang-go is far older, and Go only
#: auto-downloads a newer toolchain from 1.21 onwards.
MIN_GO = (1, 21)

REQUIRED_FLAGS = ("--no-tui", "--pair-live-photos", "--upload-incomplete-live-photos")


def platform_key() -> str:
    """Return the platform key for asset and output naming."""
    for prefix in RELEASE_ASSETS:
        if sys.platform.startswith(prefix):
            return prefix
    raise SystemExit(f"Unsupported platform: {sys.platform}")


def go_version(go: str) -> tuple[int, ...] | None:
    """Return the installed Go version, or None if it cannot be determined."""
    try:
        out = subprocess.run(
            [go, "version"], capture_output=True, text=True, timeout=60, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for token in out.split():
        if token.startswith("go") and token[2:3].isdigit():
            parts = token[2:].split(".")[:2]
            try:
                return tuple(int(p) for p in parts)
            except ValueError:
                return None
    return None


def fetch_json(url: str) -> dict:
    """GET a JSON document from the GitHub API."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.load(response)


def download_source(ref: str, into: Path) -> Path:
    """Download and extract the repository at ``ref``. Returns the source root."""
    url = f"https://github.com/{REPO}/archive/{ref}.tar.gz"
    print(f"Fetching source {ref[:12]} ...")
    archive = into / "src.tar.gz"
    try:
        urllib.request.urlretrieve(url, archive)  # noqa: S310 - fixed GitHub host
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Could not download {url}: {exc}") from exc

    with tarfile.open(archive) as tar:
        # filter="data" refuses absolute paths and traversal (Python 3.12+).
        try:
            tar.extractall(into, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12
            tar.extractall(into)  # noqa: S202
    roots = [p for p in into.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise SystemExit(f"Unexpected archive layout: {[p.name for p in roots]}")
    return roots[0]


def build(ref: str, target: Path) -> None:
    """Build the gotohp CLI at ``ref`` and place the binary at ``target``."""
    go = shutil.which("go")
    if go is None:
        raise SystemExit(
            "Go is required to build gotohp and was not found on PATH.\n"
            "  Debian/Ubuntu: its packaged golang-go is too old; install from\n"
            "                 https://go.dev/dl/ (deploy/install-linux.sh does this)\n"
            "  Windows:       winget install GoLang.Go\n"
            "  macOS:         brew install go\n"
            "Alternatively pass --release to download the published binary, but\n"
            "note that release cannot be driven headlessly."
        )
    version = go_version(go)
    if version is not None and version < MIN_GO:
        raise SystemExit(
            f"Go {'.'.join(map(str, version))} is too old; gotohp needs "
            f"{'.'.join(map(str, MIN_GO))}+ so the toolchain in go.mod can be "
            "fetched automatically. Install a newer Go from https://go.dev/dl/."
        )

    with tempfile.TemporaryDirectory(prefix="gotohp-build-") as tmp:
        source = download_source(ref, Path(tmp))
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"Building with {go} (this downloads Go modules on first run) ...")
        env = {**os.environ, "CGO_ENABLED": "0"}
        completed = subprocess.run(
            [go, "build", *BUILD_FLAGS, "-o", str(target), "."],
            cwd=source,
            env=env,
            check=False,
        )
    if completed.returncode != 0:
        raise SystemExit(f"go build failed with exit code {completed.returncode}")


def download_release(tag: str | None, target: Path) -> None:
    """Download a published release binary to ``target``."""
    endpoint = (
        f"https://api.github.com/repos/{REPO}/releases/tags/{tag}"
        if tag
        else f"https://api.github.com/repos/{REPO}/releases/latest"
    )
    try:
        release = fetch_json(endpoint)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Could not read release metadata: {exc}") from exc

    wanted = RELEASE_ASSETS[platform_key()]
    url = next((a["browser_download_url"] for a in release["assets"] if a["name"] == wanted), None)
    if url is None:
        available = ", ".join(a["name"] for a in release["assets"])
        raise SystemExit(
            f"Release {release['tag_name']} has no asset {wanted}. Available: {available}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {wanted} from {release['tag_name']} ...")
    urllib.request.urlretrieve(url, target)  # noqa: S310 - URL comes from the GitHub API


def make_executable(target: Path) -> None:
    """Set the execute bit on POSIX."""
    if not sys.platform.startswith("win32"):
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def verify(target: Path) -> list[str]:
    """Return required flags the binary is missing. Empty means usable."""
    try:
        out = subprocess.run(
            [str(target), "upload", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except OSError as exc:
        raise SystemExit(f"Could not run {target}: {exc}") from exc
    help_text = f"{out.stdout}\n{out.stderr}"
    return [flag for flag in REQUIRED_FLAGS if flag not in help_text]


def main() -> int:
    """Produce a usable gotohp binary in ./bin."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--ref",
        default=PINNED_REF,
        help=f"Git ref to build (default: pinned {PINNED_REF[:12]}).",
    )
    source.add_argument(
        "--release",
        nargs="?",
        const="",
        metavar="TAG",
        help="Download a published release instead of building. Latest if no tag.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "bin",
        help="Destination directory (default: ./bin).",
    )
    args = parser.parse_args()

    target = args.dest / OUTPUT_NAMES[platform_key()]

    if args.release is not None:
        download_release(args.release or None, target)
    else:
        build(args.ref, target)
    make_executable(target)

    missing = verify(target)
    if missing:
        print(
            f"\nERROR: {target} is missing {', '.join(missing)}.\n"
            "It cannot be driven headlessly and Live Photos would not be paired.\n"
            "Build from source instead:  python scripts/fetch_gotohp.py",
            file=sys.stderr,
        )
        return 1

    print(f"\nSaved to {target}")
    print("Verified: " + ", ".join(REQUIRED_FLAGS))
    print("\nNext: add your Google Photos credentials, then check everything:")
    print(f"  {target} creds add <auth-string>")
    print("  uv run i2g doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
