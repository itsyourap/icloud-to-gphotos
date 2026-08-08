"""Run reports and ntfy.sh notifications.

Notifications are strictly best-effort: a failed push must never turn a
successful migration run into a failed one.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from .config import Settings

LOGGER = logging.getLogger(__name__)

Priority = Literal["min", "low", "default", "high", "urgent"]


def encode_header(value: str) -> str:
    """Make a header value safe to send, encoding non-ASCII as RFC 2047.

    HTTP header values must be ASCII, so httpx raises ``UnicodeEncodeError`` on
    anything else — which would silently cost us the notification. ntfy accepts
    RFC 2047 encoded-words (``=?UTF-8?B?...?=``) and decodes them back, so a
    title containing an arrow or an emoji still arrives intact.

    The request body is unaffected: that is sent as raw UTF-8 bytes.
    """
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{encoded}?="
    return value


def write_report(settings: Settings, run_id: str, payload: dict[str, Any]) -> Path:
    """Write the run report as JSON and return its path."""
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    path = settings.report_dir / f"{run_id}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    LOGGER.info("Run report written to %s", path)
    return path


def prune_old(directory: Path, keep: int, pattern: str = "*") -> None:
    """Delete all but the ``keep`` newest files matching ``pattern``."""
    if not directory.exists():
        return
    files = sorted(
        (p for p in directory.glob(pattern) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError as exc:  # noqa: PERF203 - per-file failure is not fatal
            LOGGER.debug("Could not prune %s: %s", stale, exc)


def notify(
    settings: Settings,
    *,
    title: str,
    message: str,
    priority: Priority = "default",
    tags: list[str] | None = None,
) -> bool:
    """Send an ntfy notification. Returns False if not configured or it failed."""
    if not settings.ntfy_topic:
        return False

    url = f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}"
    headers = {
        "Title": encode_header(title),
        "Priority": priority,
        "Markdown": "yes",
    }
    if tags:
        headers["Tags"] = encode_header(",".join(tags))
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"

    try:
        response = httpx.post(
            url, content=message.encode("utf-8"), headers=headers, timeout=15.0
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - notification failures are never fatal
        LOGGER.warning("ntfy notification failed: %s", exc)
        return False
    return True


def format_summary(payload: dict[str, Any]) -> str:
    """Render a run report as a short human-readable message."""
    totals = payload.get("totals", {})
    lines = [
        f"**{payload.get('status', 'unknown')}** in {payload.get('duration_seconds', 0):.0f}s"
        f" ({payload.get('batches', 0)} batch(es))",
        f"Downloaded: {totals.get('downloaded', 0)} files"
        f" ({human_bytes(totals.get('bytes_downloaded', 0))})",
        f"Uploaded: {totals.get('uploaded', 0)}  |  Failed: {totals.get('failed', 0)}",
        f"Deleted from iCloud: {totals.get('purged_assets', 0)} assets",
    ]
    remaining = payload.get("library_remaining")
    if remaining is not None:
        lines.append(f"Remaining in iCloud: {remaining}")
    if blocked := payload.get("blocked"):
        lines.append(f"⚠ {len(blocked)} item(s) stuck after repeated failures")
    if errors := payload.get("errors"):
        lines.append(f"⚠ Errors: {'; '.join(str(e) for e in errors[:3])}")
    return "\n".join(lines)


def human_bytes(value: object) -> str:
    """Render a byte count for humans, tolerating missing or malformed input."""
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return "0 B"
    try:
        size = float(value or 0)
    except ValueError:
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def new_run_id() -> str:
    """Return a sortable, filesystem-safe identifier for this run."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
