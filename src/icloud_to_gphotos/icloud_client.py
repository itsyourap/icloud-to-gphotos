"""iCloud authentication and session management.

Session cookies and Apple's trust token are persisted under ``<state_dir>/cookies``
so unattended runs do not prompt for 2FA. Apple still expires trust tokens after
roughly a month, at which point :func:`connect` raises
:class:`ReauthRequired` and the operator must run ``i2g login`` once.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from pyicloud.base import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

from .config import Settings

LOGGER = logging.getLogger(__name__)


class ReauthRequired(RuntimeError):
    """Raised when the stored iCloud session can no longer be used unattended."""


class ICloudSession:
    """A connected iCloud session scoped to the primary photo library."""

    def __init__(self, api: PyiCloudService) -> None:
        self.api = api

    @property
    def photos(self) -> Any:
        """The Photos service facade."""
        return self.api.photos

    @property
    def library(self) -> Any:
        """The primary (personal) photo library.

        pyicloud keys the personal library as ``root``; ``shared`` is the legacy
        shared-stream library and ``shared:<zone>`` entries are Shared Photo
        Libraries. Those are deliberately out of scope, because deleting from a
        Shared Photo Library removes other participants' copies too.
        """
        libraries = self.photos.libraries
        if "root" in libraries:
            return libraries["root"]
        # Defensive: if Apple ever renames the primary key, prefer any private
        # zone over a shared one rather than failing outright.
        for key, value in libraries.items():
            if "shared" not in key.lower():
                return value
        raise ReauthRequired("No personal iCloud photo library is accessible.")

    def iter_all_assets(self) -> Iterator[Any]:
        """Yield every asset in the main library, oldest capture date first.

        Oldest-first matters: the deletion grace period protects the newest
        items, so ascending order drains the backlog instead of repeatedly
        revisiting photos that are too recent to delete.
        """
        return iter(self.library.all.photos)

    def library_size(self) -> int:
        """Return the number of assets in the main library."""
        return len(self.library.all)


def _build(settings: Settings, *, password: str | None, authenticate: bool) -> PyiCloudService:
    settings.cookie_dir.mkdir(parents=True, exist_ok=True)
    return PyiCloudService(
        apple_id=settings.icloud_username,
        password=password,
        cookie_directory=str(settings.cookie_dir),
        with_family=False,
        authenticate=authenticate,
    )


def connect(settings: Settings) -> ICloudSession:
    """Connect using the persisted session only; never prompt.

    Raises:
        ReauthRequired: if no valid trusted session is stored.
    """
    try:
        api = _build(settings, password=settings.icloud_password, authenticate=True)
    except PyiCloudFailedLoginException as exc:
        raise ReauthRequired(f"iCloud login failed: {exc}") from exc

    if api.requires_2fa or api.requires_2sa:
        raise ReauthRequired(
            "iCloud requires two-factor verification. Run `i2g login` to re-establish "
            "the trusted session."
        )
    if not api.is_trusted_session:
        raise ReauthRequired(
            "The stored iCloud session is not trusted. Run `i2g login` to refresh it."
        )
    LOGGER.info("Connected to iCloud as %s", settings.icloud_username)
    return ICloudSession(api)


def interactive_login(
    settings: Settings,
    password: str,
    code_provider: Any,
) -> ICloudSession:
    """Log in interactively, completing 2FA and persisting a trusted session.

    Args:
        settings: Runtime settings supplying the Apple ID and cookie directory.
        password: The Apple ID password.
        code_provider: Zero-argument callable returning the 6-digit 2FA code.
    """
    api = _build(settings, password=password, authenticate=True)

    if api.requires_2fa:
        code = str(code_provider()).strip()
        if not api.validate_2fa_code(code):
            raise ReauthRequired("The two-factor code was rejected by Apple.")
        LOGGER.info("Two-factor verification accepted.")

    if not api.is_trusted_session:
        if not api.trust_session():
            raise ReauthRequired(
                "Apple refused to trust this session. Unattended runs will keep "
                "prompting for 2FA."
            )
        LOGGER.info("Session trusted; cookies stored in %s", settings.cookie_dir)

    return ICloudSession(api)


def session_health(settings: Settings) -> dict[str, Any]:
    """Report on the stored session without prompting or raising."""
    try:
        api = _build(settings, password=None, authenticate=False)
    except Exception as exc:  # noqa: BLE001 - health check must never raise
        return {"ok": False, "reason": f"could not initialise client: {exc}"}

    try:
        api.authenticate()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}

    trusted = bool(api.is_trusted_session)
    return {
        "ok": trusted and not api.requires_2fa,
        "trusted_session": trusted,
        "requires_2fa": bool(api.requires_2fa),
        "requires_2sa": bool(api.requires_2sa),
        "cookie_dir": str(settings.cookie_dir),
    }
