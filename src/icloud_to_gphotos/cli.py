"""Command-line interface.

    i2g login     establish the trusted iCloud session (interactive, run once)
    i2g doctor    verify every dependency and credential before scheduling
    i2g run       execute one migration pass
    i2g status    ledger and session summary
    i2g report    show the most recent run report
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import logging_setup, notify
from .binaries import default_gotohp_config, find_gotohp
from .config import Settings, load_settings
from .icloud_client import ReauthRequired, connect, interactive_login, session_health
from .ledger import Ledger
from .metadata import find_exiftool
from .pipeline import Pipeline
from .uploader import UploadError, check_credentials, missing_upload_flags

app = typer.Typer(
    name="i2g",
    help="Move iCloud Photos into Google Photos, then clear them from iCloud.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
LOGGER = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REAUTH = 2
EXIT_CONFIG = 3


def _settings() -> Settings:
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean CLI error
        console.print(f"[bold red]Configuration error:[/] {exc}")
        console.print("Copy [cyan].env.example[/] to [cyan].env[/] and fill it in.")
        raise typer.Exit(EXIT_CONFIG) from exc
    settings.ensure_dirs()
    return settings


@app.command()
def login(
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            help="Apple ID password. Prompted for if omitted; never stored by us.",
        ),
    ] = None,
    code: Annotated[
        str | None,
        typer.Option("--code", help="Two-factor code, if you already have it."),
    ] = None,
) -> None:
    """Establish a trusted iCloud session so later runs need no interaction.

    Apple expires trust tokens after roughly a month; rerun this when `i2g
    doctor` reports the session is no longer trusted.
    """
    settings = _settings()
    logging_setup.configure(settings.log_level)

    secret = password or settings.icloud_password
    if not secret:
        secret = typer.prompt(
            f"iCloud password for {settings.icloud_username}", hide_input=True
        )

    def provide_code() -> str:
        if code:
            return code
        return typer.prompt("Two-factor code sent to your Apple devices")

    try:
        interactive_login(settings, secret, provide_code)
    except ReauthRequired as exc:
        console.print(f"[bold red]Login failed:[/] {exc}")
        raise typer.Exit(EXIT_REAUTH) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Login failed:[/] {exc}")
        raise typer.Exit(EXIT_FAILED) from exc

    console.print("[bold green]✓[/] Trusted iCloud session stored.")
    console.print(f"  Cookies: [dim]{settings.cookie_dir}[/]")


@app.command()
def doctor() -> None:
    """Check every dependency, credential, and path before you schedule this."""
    settings = _settings()
    logging_setup.configure("WARNING")

    table = Table(title="icloud-to-gphotos preflight", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    problems = 0
    warnings = 0

    def record(name: str, ok: bool | None, detail: str) -> None:
        nonlocal problems, warnings
        if ok is True:
            table.add_row(name, "[green]OK[/]", detail)
        elif ok is None:
            table.add_row(name, "[yellow]WARN[/]", detail)
            warnings += 1
        else:
            table.add_row(name, "[red]FAIL[/]", detail)
            problems += 1

    # gotohp
    gotohp = find_gotohp(settings.gotohp_binary)
    if gotohp is None:
        record(
            "gotohp CLI",
            False,
            "Not found. Run `python scripts/fetch_gotohp.py` or set I2G_GOTOHP_BINARY.",
        )
    else:
        record("gotohp CLI", True, str(gotohp))
        # The published release silently ignores the flags this project needs,
        # so probe for them rather than trusting the binary's presence.
        try:
            missing = missing_upload_flags(gotohp)
        except UploadError as exc:
            record("gotohp capabilities", False, str(exc))
        else:
            record(
                "gotohp capabilities",
                not missing,
                "headless upload and Live Photo pairing supported"
                if not missing
                else (
                    f"missing {', '.join(missing)} — this is the published v0.8.1 "
                    "release, which cannot run under systemd. Rebuild with "
                    "`python scripts/fetch_gotohp.py`."
                ),
            )
        ok, detail = check_credentials(gotohp, settings.gotohp_config)
        record("Google Photos creds", ok, detail)
        if settings.gotohp_config is None and (implied := default_gotohp_config()):
            record(
                "gotohp config",
                None if not implied.exists() else True,
                f"{implied} ({'present' if implied.exists() else 'not created yet'})",
            )

    # exiftool
    exiftool = find_exiftool(settings.exiftool_binary)
    if exiftool is None:
        record(
            "exiftool",
            None if not settings.backfill_metadata else False,
            "Not found. HEIC and video capture dates cannot be verified or repaired. "
            "See docs/SETUP.md.",
        )
    else:
        record("exiftool", True, str(exiftool))

    # iCloud session
    health = session_health(settings)
    if health.get("ok"):
        record("iCloud session", True, f"trusted, cookies in {health['cookie_dir']}")
    else:
        record(
            "iCloud session",
            False,
            f"{health.get('reason', health)} — run `i2g login`",
        )

    # Disk
    import shutil as _shutil

    assert settings.staging_dir is not None
    try:
        usage = _shutil.disk_usage(settings.staging_dir)
        enough = usage.free > settings.disk_headroom_bytes
        record(
            "Disk space",
            enough,
            f"{notify.human_bytes(usage.free)} free at {settings.staging_dir} "
            f"(headroom {notify.human_bytes(settings.disk_headroom_bytes)}, "
            f"batch cap {notify.human_bytes(settings.batch_max_bytes)})",
        )
    except OSError as exc:
        record("Disk space", False, f"cannot read {settings.staging_dir}: {exc}")

    # Notifications
    record(
        "ntfy",
        True if settings.ntfy_topic else None,
        f"{settings.ntfy_server}/{settings.ntfy_topic}"
        if settings.ntfy_topic
        else "Not configured; results go to logs and reports only.",
    )

    # Deletion policy, stated plainly because it is the irreversible part.
    record(
        "Deletion policy",
        True,
        f"delete_from_icloud={settings.delete_from_icloud}, "
        f"grace={settings.delete_grace_days}d, edited_policy={settings.edited_policy}",
    )

    console.print(table)
    if problems:
        console.print(f"[bold red]{problems} blocking problem(s).[/]")
        raise typer.Exit(EXIT_FAILED)
    if warnings:
        console.print(f"[yellow]{warnings} warning(s); the pipeline can still run.[/]")
    console.print("[bold green]Ready.[/]")


@app.command()
def run(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan and report only; download nothing, delete nothing."),
    ] = False,
    max_batches: Annotated[
        int | None,
        typer.Option("--max-batches", help="Stop after this many batches."),
    ] = None,
    no_delete: Annotated[
        bool,
        typer.Option("--no-delete", help="Download and upload, but never delete from iCloud."),
    ] = False,
) -> None:
    """Run one migration pass: download, upload, verify, then delete."""
    settings = _settings()
    if max_batches is not None:
        settings.max_batches_per_run = max_batches
    if no_delete:
        settings.delete_from_icloud = False

    run_id = notify.new_run_id()
    log_file = logging_setup.configure(settings.log_level, settings.log_dir / f"{run_id}.log")
    LOGGER.info("Run %s starting (dry_run=%s)", run_id, dry_run)

    try:
        session = connect(settings)
    except ReauthRequired as exc:
        LOGGER.error("%s", exc)
        notify.notify(
            settings,
            title="iCloud re-authentication needed",
            message=(
                f"The nightly migration could not start:\n\n{exc}\n\n"
                "Run `i2g login` on the host."
            ),
            priority="urgent",
            tags=["warning", "key"],
        )
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(EXIT_REAUTH) from exc

    with Ledger(settings.ledger_path) as ledger:
        ledger.start_run(run_id)
        pipeline = Pipeline(settings, session, ledger, dry_run=dry_run)
        result = pipeline.run(run_id)
        ledger.finish_run(
            run_id,
            status=result.status,
            downloaded=result.totals.downloaded,
            uploaded=result.totals.uploaded,
            failed=result.totals.failed,
            purged=result.totals.purged_assets,
            bytes_moved=result.totals.bytes_downloaded,
            detail="; ".join(result.errors[:5]) or None,
        )

    payload = result.as_dict()
    if log_file is not None:
        payload["log_file"] = str(log_file)
    notify.write_report(settings, run_id, payload)
    notify.prune_old(settings.log_dir, settings.log_retention, "*.log")
    notify.prune_old(settings.report_dir, settings.log_retention, "*.json")

    summary = notify.format_summary(payload)
    console.print(summary)

    failed = result.status in ("error", "interrupted")
    if failed or result.status == "ok_with_blocked" or result.errors:
        notify.notify(
            settings,
            title=f"iCloud → Google Photos: {result.status}",
            message=summary,
            priority="high" if failed else "default",
            tags=["warning"] if failed else ["card_file_box"],
        )
    elif settings.notify_on_success:
        notify.notify(
            settings,
            title="iCloud → Google Photos: done",
            message=summary,
            tags=["white_check_mark"],
        )

    raise typer.Exit(EXIT_FAILED if failed else EXIT_OK)


@app.command()
def status() -> None:
    """Show ledger progress, session health, and recent runs."""
    settings = _settings()
    logging_setup.configure("WARNING")

    with Ledger(settings.ledger_path) as ledger:
        stats = ledger.stats()
        runs = ledger.recent_runs(10)
        blocked = ledger.blocked_resources(20)

    table = Table(title="Migration state")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Assets seen", str(stats.get("assets_total", 0)))
    table.add_row("Assets deleted from iCloud", str(stats.get("assets_purged", 0)))
    for key in ("pending", "downloaded", "uploaded", "failed", "purged"):
        table.add_row(f"Resources {key}", str(stats.get(f"resources_{key}", 0)))
    table.add_row("Bytes confirmed remote", notify.human_bytes(stats.get("bytes_uploaded", 0)))
    console.print(table)

    health = session_health(settings)
    if health.get("ok"):
        console.print("iCloud session: [green]trusted[/]")
    else:
        console.print(f"iCloud session: [red]needs login[/] ({health.get('reason')})")

    if runs:
        history = Table(title="Recent runs")
        for column in ("Run", "Status", "Down", "Up", "Fail", "Deleted"):
            history.add_column(column)
        for row in runs:
            history.add_row(
                row["run_id"],
                row["status"],
                str(row["downloaded"]),
                str(row["uploaded"]),
                str(row["failed"]),
                str(row["purged"]),
            )
        console.print(history)

    if blocked:
        console.print(f"[yellow]{len(blocked)} resource(s) stuck after repeated failures:[/]")
        for stuck in blocked[:10]:
            console.print(f"  [dim]{stuck.filename}[/] — {stuck.error}")


@app.command()
def report(
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Specific run. Defaults to the latest.")
    ] = None,
) -> None:
    """Print a run report as JSON."""
    settings = _settings()
    reports = sorted(settings.report_dir.glob("*.json"))
    if not reports:
        console.print("[yellow]No reports yet.[/]")
        raise typer.Exit(EXIT_OK)

    target: Path | None = None
    if run_id:
        candidate = settings.report_dir / f"{run_id}.json"
        target = candidate if candidate.exists() else None
        if target is None:
            console.print(f"[red]No report for run {run_id}.[/]")
            raise typer.Exit(EXIT_FAILED)
    else:
        target = reports[-1]

    console.print_json(json.dumps(json.loads(target.read_text(encoding="utf-8"))))


def main() -> None:
    """Entry point used by ``python -m icloud_to_gphotos``."""
    try:
        app()
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/]")
        sys.exit(EXIT_FAILED)


if __name__ == "__main__":
    main()
