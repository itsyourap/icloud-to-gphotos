#!/usr/bin/env python3
"""Download the gotohp CLI release binary for the current platform into ./bin.

Usage:
    uv run python scripts/fetch_gotohp.py            # latest release
    uv run python scripts/fetch_gotohp.py --tag v0.8.1
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "xob0t/gotohp"
ASSETS = {
    "win32": "gotohp-cli-x64.exe",
    "linux": "gotohp-cli_amd64",
    "darwin": "gotohp-cli-macos-universal",
}


def platform_asset() -> str:
    """Return the release asset name for the running platform."""
    for prefix, asset in ASSETS.items():
        if sys.platform.startswith(prefix):
            return asset
    raise SystemExit(f"Unsupported platform: {sys.platform}")


def fetch_json(url: str) -> dict:
    """GET a JSON document from the GitHub API."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> int:
    """Download the binary and mark it executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Release tag. Defaults to the latest release.")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "bin",
        help="Destination directory (default: ./bin).",
    )
    args = parser.parse_args()

    endpoint = (
        f"https://api.github.com/repos/{REPO}/releases/tags/{args.tag}"
        if args.tag
        else f"https://api.github.com/repos/{REPO}/releases/latest"
    )
    try:
        release = fetch_json(endpoint)
    except urllib.error.HTTPError as exc:
        print(f"Could not read release metadata: {exc}", file=sys.stderr)
        return 1

    wanted = platform_asset()
    url = next((a["browser_download_url"] for a in release["assets"] if a["name"] == wanted), None)
    if url is None:
        available = ", ".join(a["name"] for a in release["assets"])
        print(
            f"Release {release['tag_name']} has no asset {wanted}. Available: {available}",
            file=sys.stderr,
        )
        return 1

    args.dest.mkdir(parents=True, exist_ok=True)
    target = args.dest / wanted
    print(f"Downloading {wanted} from {release['tag_name']}...")
    urllib.request.urlretrieve(url, target)  # noqa: S310 - URL comes from the GitHub API

    if not sys.platform.startswith("win32"):
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"Saved to {target}")
    print("\nNext: add your Google Photos credentials, then verify:")
    print(f"  {target} creds add <auth-string>")
    print("  uv run i2g doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
