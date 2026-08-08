"""Tests for iCloud session handling.

The rule: an unattended run must never hang waiting for a 2FA code. Anything
short of a trusted session has to raise :class:`ReauthRequired` so the scheduler
can alert a human instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from pyicloud.exceptions import PyiCloudFailedLoginException

from icloud_to_gphotos import icloud_client
from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.icloud_client import (
    ICloudSession,
    ReauthRequired,
    connect,
    interactive_login,
    session_health,
)


class FakeApi:
    """Duck-typed stand-in for ``PyiCloudService``."""

    def __init__(
        self,
        *,
        requires_2fa: bool = False,
        requires_2sa: bool = False,
        is_trusted_session: bool = True,
        libraries: dict[str, Any] | None = None,
        code_accepted: bool = True,
        trust_accepted: bool = True,
        authenticate_error: Exception | None = None,
    ) -> None:
        self.requires_2fa = requires_2fa
        self.requires_2sa = requires_2sa
        self.is_trusted_session = is_trusted_session
        self._libraries = libraries if libraries is not None else {"root": "primary-library"}
        self._code_accepted = code_accepted
        self._trust_accepted = trust_accepted
        self._authenticate_error = authenticate_error
        self.validated_codes: list[str] = []
        self.trust_calls = 0

    @property
    def photos(self) -> Any:
        return self

    @property
    def libraries(self) -> dict[str, Any]:
        return self._libraries

    def authenticate(self) -> None:
        if self._authenticate_error is not None:
            raise self._authenticate_error

    def validate_2fa_code(self, code: str) -> bool:
        self.validated_codes.append(code)
        if self._code_accepted:
            self.requires_2fa = False
            self.is_trusted_session = True
        return self._code_accepted

    def trust_session(self) -> bool:
        self.trust_calls += 1
        if self._trust_accepted:
            self.is_trusted_session = True
        return self._trust_accepted


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch):
    """Replace the real PyiCloudService constructor with a fake."""

    def install(api: FakeApi | Exception) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def fake_build(settings, *, password, authenticate):
            calls.append({"password": password, "authenticate": authenticate})
            if isinstance(api, Exception):
                raise api
            return api

        monkeypatch.setattr(icloud_client, "_build", fake_build)
        return calls

    return install


# --- connect ---------------------------------------------------------------


def test_connect_succeeds_with_a_trusted_session(settings: Settings, build) -> None:
    api = FakeApi()
    build(api)

    session = connect(settings)

    assert isinstance(session, ICloudSession)
    assert session.api is api


def test_connect_refuses_when_two_factor_is_pending(settings: Settings, build) -> None:
    """Never prompt from `run`; a cron job has no terminal to prompt on."""
    build(FakeApi(requires_2fa=True, is_trusted_session=False))

    with pytest.raises(ReauthRequired, match="i2g login"):
        connect(settings)


def test_connect_refuses_when_two_step_is_pending(settings: Settings, build) -> None:
    build(FakeApi(requires_2sa=True, is_trusted_session=False))

    with pytest.raises(ReauthRequired):
        connect(settings)


def test_connect_refuses_an_untrusted_session(settings: Settings, build) -> None:
    build(FakeApi(is_trusted_session=False))

    with pytest.raises(ReauthRequired, match="not trusted"):
        connect(settings)


def test_connect_wraps_a_failed_login(settings: Settings, build) -> None:
    build(PyiCloudFailedLoginException("bad password"))

    with pytest.raises(ReauthRequired, match="login failed"):
        connect(settings)


# --- Library selection -----------------------------------------------------


def test_library_uses_the_root_key() -> None:
    """pyicloud keys the personal library as "root"; "shared" is the legacy
    shared-stream library and "shared:<zone>" entries are Shared Libraries."""
    session = ICloudSession(
        FakeApi(libraries={"root": "mine", "shared": "streams", "shared:X": "theirs"})
    )

    assert session.library == "mine"


def test_library_prefers_root_over_other_private_zones() -> None:
    session = ICloudSession(FakeApi(libraries={"SomeZone": "other", "root": "mine"}))

    assert session.library == "mine"


def test_library_falls_back_to_the_first_non_shared_library() -> None:
    session = ICloudSession(
        FakeApi(libraries={"shared:abc": "theirs", "something-else": "mine"})
    )

    assert session.library == "mine"


def test_library_never_selects_a_shared_library() -> None:
    """Deleting from a Shared Photo Library would remove other people's copies,
    so refuse rather than guess."""
    session = ICloudSession(FakeApi(libraries={"shared": "streams", "shared:abc": "theirs"}))

    with pytest.raises(ReauthRequired, match="No personal iCloud photo library"):
        _ = session.library


def test_library_raises_when_none_are_accessible() -> None:
    session = ICloudSession(FakeApi(libraries={}))

    with pytest.raises(ReauthRequired):
        _ = session.library


# --- Asset iteration -------------------------------------------------------


class _FakeLibrary:
    def __init__(self, photos: list[str]) -> None:
        self.all = _FakeAlbum(photos)


class _FakeAlbum:
    def __init__(self, photos: list[str]) -> None:
        self._photos = photos

    @property
    def photos(self) -> Any:
        return iter(self._photos)

    def __len__(self) -> int:
        return len(self._photos)


def test_iter_all_assets_walks_the_main_library(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ICloudSession(FakeApi())
    monkeypatch.setattr(
        type(session), "library", property(lambda _self: _FakeLibrary(["a", "b", "c"]))
    )

    assert list(session.iter_all_assets()) == ["a", "b", "c"]
    assert session.library_size() == 3


# --- interactive_login -----------------------------------------------------


def test_interactive_login_completes_two_factor_and_trusts(settings: Settings, build) -> None:
    api = FakeApi(requires_2fa=True, is_trusted_session=False)
    build(api)

    session = interactive_login(settings, "pw", lambda: "123456")

    assert api.validated_codes == ["123456"]
    assert session.api is api


def test_interactive_login_strips_whitespace_from_the_code(settings: Settings, build) -> None:
    api = FakeApi(requires_2fa=True, is_trusted_session=False)
    build(api)

    interactive_login(settings, "pw", lambda: "  123456\n")

    assert api.validated_codes == ["123456"]


def test_interactive_login_raises_on_a_rejected_code(settings: Settings, build) -> None:
    build(FakeApi(requires_2fa=True, is_trusted_session=False, code_accepted=False))

    with pytest.raises(ReauthRequired, match="rejected by Apple"):
        interactive_login(settings, "pw", lambda: "000000")


def test_interactive_login_trusts_an_already_authenticated_session(
    settings: Settings, build
) -> None:
    api = FakeApi(requires_2fa=False, is_trusted_session=False)
    build(api)

    interactive_login(settings, "pw", lambda: "unused")

    assert api.validated_codes == []
    assert api.trust_calls == 1


def test_interactive_login_raises_when_apple_refuses_to_trust(
    settings: Settings, build
) -> None:
    """Without a trust token every future run would prompt for 2FA, so surface it
    now rather than at 00:00."""
    build(FakeApi(requires_2fa=False, is_trusted_session=False, trust_accepted=False))

    with pytest.raises(ReauthRequired, match="refused to trust"):
        interactive_login(settings, "pw", lambda: "unused")


def test_interactive_login_skips_trusting_when_already_trusted(
    settings: Settings, build
) -> None:
    api = FakeApi(is_trusted_session=True)
    build(api)

    interactive_login(settings, "pw", lambda: "unused")

    assert api.trust_calls == 0


# --- session_health --------------------------------------------------------


def test_session_health_reports_a_healthy_session(settings: Settings, build) -> None:
    calls = build(FakeApi())

    health = session_health(settings)

    assert health["ok"] is True
    assert health["trusted_session"] is True
    assert health["cookie_dir"] == str(settings.cookie_dir)
    # Health checks must not attempt a password login.
    assert calls[0] == {"password": None, "authenticate": False}


def test_session_health_reports_pending_two_factor(settings: Settings, build) -> None:
    build(FakeApi(requires_2fa=True, is_trusted_session=False))

    health = session_health(settings)

    assert health["ok"] is False
    assert health["requires_2fa"] is True


def test_session_health_never_raises_on_an_authentication_error(
    settings: Settings, build
) -> None:
    build(FakeApi(authenticate_error=RuntimeError("cookies are gone")))

    health = session_health(settings)

    assert health["ok"] is False
    assert "cookies are gone" in health["reason"]


def test_session_health_never_raises_on_a_construction_error(
    settings: Settings, build
) -> None:
    build(RuntimeError("cannot reach Apple"))

    health = session_health(settings)

    assert health["ok"] is False
    assert "could not initialise" in health["reason"]
