"""
Tidal OAuth2 Authentication Client.

Handles the full OAuth2 lifecycle using YOUR OWN Tidal developer credentials
from https://developer.tidal.com/dashboard — no hardcoded secrets, no trusting
some random GitHub project's embedded keys.

Supports two flows:
  1. Device Authorization (for headless/CLI use — user visits a URL, enters a code)
  2. Refresh Token (for long-lived sessions without re-auth)

Inspired by tiddl (https://github.com/oskvr37/tiddl) but rewritten to:
  - Use your own client_id / client_secret from .env
  - Support all Tidal Developer Dashboard scopes
  - Provide proper token persistence and auto-refresh
  - Never embed or fallback to hardcoded credentials
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests

from core.auth.models import (
    OAuthScope,
    TidalAuthError,
    TidalDeviceAuthResponse,
    TidalSession,
    TidalTokenResponse,
    TidalTokenStore,
)

log = logging.getLogger(__name__)

# Tidal OAuth2 endpoints
TIDAL_AUTH_BASE_URL = "https://auth.tidal.com/v1/oauth2"
TIDAL_API_BASE_URL = "https://api.tidal.com/v1"

# Polling constraints for device auth flow
DEFAULT_POLL_INTERVAL = 5
MAX_POLL_ATTEMPTS = 120  # 10 minutes at 5s intervals


class TidalAuthError_(Exception):
    """Raised when Tidal authentication fails."""

    def __init__(self, message: str, error_response: Optional[TidalAuthError] = None):
        super().__init__(message)
        self.error_response = error_response


class TidalCredentialsMissing(TidalAuthError_):
    """Raised when client_id or client_secret is not configured."""

    pass


class TidalTokenExpired(TidalAuthError_):
    """Raised when the access token is expired and cannot be refreshed."""

    pass


class TidalDeviceAuthTimeout(TidalAuthError_):
    """Raised when the device authorization flow times out waiting for user."""

    pass


class TidalAuthClient:
    """
    OAuth2 client for Tidal API using YOUR developer credentials.

    Usage:
        client = TidalAuthClient(
            client_id="your-client-id-from-dashboard",
            client_secret="your-client-secret-from-dashboard",
        )

        # First-time login (device flow):
        token_store = client.device_login()

        # Later, refresh the token:
        token_store = client.refresh(token_store)

        # Or load from disk and auto-refresh if needed:
        token_store = client.ensure_valid_token(token_store)
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        scopes: Optional[list[OAuthScope]] = None,
        token_store_path: Optional[Path] = None,
        auth_base_url: str = TIDAL_AUTH_BASE_URL,
        api_base_url: str = TIDAL_API_BASE_URL,
    ):
        """
        Initialize the Tidal auth client.

        Args:
            client_id: Your Tidal developer app client ID.
            client_secret: Your Tidal developer app client secret.
            scopes: OAuth scopes to request. Defaults to bridge_scopes().
            token_store_path: Path to persist tokens on disk. Defaults to ./data/tidal_tokens.json.
            auth_base_url: Tidal OAuth2 base URL (override for testing).
            api_base_url: Tidal API base URL (override for testing).
        """
        if not client_id or not client_secret:
            raise TidalCredentialsMissing(
                "Tidal client_id and client_secret are required. "
                "Set TIDAL_CLIENT_ID and TIDAL_CLIENT_SECRET in your .env file. "
                "Get these from https://developer.tidal.com/dashboard"
            )

        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or OAuthScope.bridge_scopes()
        self.auth_base_url = auth_base_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")

        self.token_store_path = token_store_path or Path("./data/tidal_tokens.json")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
        )

        self._token_store: Optional[TidalTokenStore] = None

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def scope_string(self) -> str:
        """Join configured scopes into a space-separated string for OAuth requests."""
        return " ".join(s.value for s in self.scopes)

    @property
    def token_store(self) -> Optional[TidalTokenStore]:
        """Current in-memory token store."""
        return self._token_store

    @property
    def access_token(self) -> Optional[str]:
        """Current access token, or None if not authenticated."""
        if self._token_store and self._token_store.is_valid:
            return self._token_store.access_token
        return None

    @property
    def is_authenticated(self) -> bool:
        """Check if we have a valid (non-expired) access token."""
        return self._token_store is not None and self._token_store.is_valid

    # -------------------------------------------------------------------------
    # Device Authorization Flow
    # -------------------------------------------------------------------------

    def request_device_authorization(self) -> TidalDeviceAuthResponse:
        """
        Step 1 of device auth: Request a device code + user code.

        Returns a TidalDeviceAuthResponse containing the verification URL
        and user code. The user must visit the URL and enter the code.

        Returns:
            TidalDeviceAuthResponse with device_code, user_code, verification_uri.

        Raises:
            TidalAuthError_: If the request fails.
        """
        log.info("Requesting device authorization from Tidal...")

        response = self._session.post(
            f"{self.auth_base_url}/device_authorization",
            data={
                "client_id": self.client_id,
                "scope": self.scope_string,
            },
        )

        if response.status_code != 200:
            self._handle_error_response(response, "Device authorization request failed")

        data = response.json()
        device_auth = TidalDeviceAuthResponse.model_validate(data)

        log.info(
            "Device authorization received. User code: %s | Verify at: %s",
            device_auth.user_code,
            device_auth.verification_uri,
        )

        return device_auth

    def poll_for_token(
        self,
        device_auth: TidalDeviceAuthResponse,
        on_poll: Optional[callable] = None,
    ) -> TidalTokenStore:
        """
        Step 2 of device auth: Poll until the user authorizes the device.

        Polls the token endpoint at the specified interval until either:
        - The user completes authorization (returns tokens)
        - The device code expires
        - Max poll attempts are reached

        Args:
            device_auth: The device authorization response from step 1.
            on_poll: Optional callback called each poll iteration with
                     (attempt_number, seconds_remaining). Useful for UI updates.

        Returns:
            TidalTokenStore with access + refresh tokens.

        Raises:
            TidalDeviceAuthTimeout: If the device code expires or max attempts reached.
            TidalAuthError_: If the user denies or an unexpected error occurs.
        """
        interval = max(device_auth.interval, DEFAULT_POLL_INTERVAL)
        max_attempts = min(MAX_POLL_ATTEMPTS, device_auth.expires_in // interval)

        log.info(
            "Polling for user authorization (interval=%ds, max_attempts=%d)...",
            interval,
            max_attempts,
        )

        for attempt in range(1, max_attempts + 1):
            time.sleep(interval)

            seconds_remaining = device_auth.expires_in - (attempt * interval)
            if on_poll:
                on_poll(attempt, max(0, seconds_remaining))

            try:
                token_response = self._exchange_device_code(device_auth.device_code)
            except _PendingAuthorization:
                log.debug("Poll %d/%d: authorization_pending", attempt, max_attempts)
                continue
            except _SlowDown:
                log.debug("Poll %d/%d: slow_down — increasing interval", attempt, max_attempts)
                interval += 1
                continue
            except _DeviceCodeExpired:
                raise TidalDeviceAuthTimeout("Device code expired. The user did not authorize in time.")
            except _AccessDenied:
                raise TidalAuthError_("User denied authorization.")

            # Success! Build and persist token store.
            token_store = TidalTokenStore.from_token_response(
                response=token_response,
                client_id=self.client_id,
            )

            self._token_store = token_store
            self._persist_token_store()

            log.info(
                "Authorization successful! User ID: %s | Token expires in %ds",
                token_store.user_id,
                token_response.expires_in,
            )

            return token_store

        raise TidalDeviceAuthTimeout(
            f"Timed out after {max_attempts} poll attempts. The user did not complete authorization."
        )

    def device_login(
        self,
        on_user_code: Optional[callable] = None,
        on_poll: Optional[callable] = None,
    ) -> TidalTokenStore:
        """
        Complete device authorization flow in one call.

        This is the primary method for first-time authentication.

        Args:
            on_user_code: Callback with (user_code, verification_uri) so the caller
                          can display instructions to the user. If not provided,
                          instructions are printed to stdout.
            on_poll: Optional callback for poll progress.

        Returns:
            TidalTokenStore with valid tokens.
        """
        device_auth = self.request_device_authorization()

        if on_user_code:
            on_user_code(device_auth.user_code, device_auth.verification_uri)
        else:
            print("\n" + "=" * 60)
            print("  TIDAL DEVICE AUTHORIZATION")
            print("=" * 60)
            print(f"\n  1. Open: {device_auth.verification_uri}")
            print(f"  2. Enter code: {device_auth.user_code}")
            print(f"\n  Code expires in {device_auth.expires_in // 60} minutes.")
            print("  Waiting for authorization...\n")

        return self.poll_for_token(device_auth, on_poll=on_poll)

    # -------------------------------------------------------------------------
    # Token Refresh
    # -------------------------------------------------------------------------

    def refresh(self, token_store: Optional[TidalTokenStore] = None) -> TidalTokenStore:
        """
        Refresh an expired access token using the refresh token.

        Args:
            token_store: Token store to refresh. Uses the internal store if not provided.

        Returns:
            Updated TidalTokenStore with a fresh access token.

        Raises:
            TidalAuthError_: If the refresh token is invalid or revoked.
            TidalTokenExpired: If no refresh token is available.
        """
        store = token_store or self._token_store
        if not store or not store.can_refresh:
            raise TidalTokenExpired("No refresh token available. Run device_login() to re-authenticate.")

        log.info("Refreshing Tidal access token...")

        response = self._session.post(
            f"{self.auth_base_url}/token",
            data={
                "client_id": self.client_id,
                "refresh_token": store.refresh_token,
                "grant_type": "refresh_token",
                "scope": self.scope_string,
            },
            auth=(self.client_id, self.client_secret),
        )

        if response.status_code != 200:
            error_data = self._parse_error(response)
            if error_data and error_data.is_invalid_grant:
                raise TidalTokenExpired("Refresh token is invalid or revoked. Run device_login() to re-authenticate.")
            self._handle_error_response(response, "Token refresh failed")

        token_response = TidalTokenResponse.model_validate(response.json())

        # Build updated store, preserving the existing refresh token if the
        # response doesn't include a new one.
        updated_store = TidalTokenStore.from_token_response(
            response=token_response,
            client_id=self.client_id,
            country_code=store.country_code,
            existing_refresh_token=store.refresh_token,
        )

        self._token_store = updated_store
        self._persist_token_store()

        log.info(
            "Token refreshed successfully. Expires in %ds (at %s)",
            token_response.expires_in,
            time.strftime("%H:%M:%S", time.localtime(updated_store.expires_at)),
        )

        return updated_store

    # -------------------------------------------------------------------------
    # Token Lifecycle Management
    # -------------------------------------------------------------------------

    def ensure_valid_token(self, token_store: Optional[TidalTokenStore] = None) -> TidalTokenStore:
        """
        Ensure we have a valid (non-expired) access token.

        If the current token is expired but we have a refresh token,
        this will automatically refresh it. If no token exists, it will
        try to load from disk.

        Args:
            token_store: Optional token store to check. Uses internal store if not provided.

        Returns:
            A valid TidalTokenStore.

        Raises:
            TidalTokenExpired: If the token can't be refreshed.
        """
        store = token_store or self._token_store

        # Try loading from disk if we have nothing in memory
        if store is None:
            store = TidalTokenStore.load(self.token_store_path)
            if store:
                log.info("Loaded token store from disk: %s", self.token_store_path)
                self._token_store = store

        if store is None:
            raise TidalTokenExpired("No token found. Run device_login() to authenticate.")

        if store.is_valid:
            self._token_store = store
            return store

        if store.can_refresh:
            log.info(
                "Access token expired (%.0fs ago). Refreshing...",
                time.time() - store.expires_at,
            )
            return self.refresh(store)

        raise TidalTokenExpired(
            "Access token expired and no refresh token available. Run device_login() to re-authenticate."
        )

    def load_or_login(
        self,
        on_user_code: Optional[callable] = None,
        on_poll: Optional[callable] = None,
    ) -> TidalTokenStore:
        """
        Load existing tokens from disk, refresh if needed, or start a fresh login.

        This is the recommended entry point for most use cases.

        Args:
            on_user_code: Callback for device auth if a fresh login is needed.
            on_poll: Callback for device auth polling progress.

        Returns:
            A valid TidalTokenStore.
        """
        try:
            return self.ensure_valid_token()
        except TidalTokenExpired:
            log.info("No valid token available. Starting device login flow...")
            return self.device_login(on_user_code=on_user_code, on_poll=on_poll)

    # -------------------------------------------------------------------------
    # Session Info
    # -------------------------------------------------------------------------

    def get_session(self) -> TidalSession:
        """
        Fetch the current session info from Tidal API.

        This confirms the token is working and retrieves the user's
        country code, user ID, and session details.

        Returns:
            TidalSession with user info.

        Raises:
            TidalAuthError_: If the request fails or token is invalid.
        """
        store = self.ensure_valid_token()

        response = self._session.get(
            f"{self.api_base_url}/sessions",
            headers={"Authorization": f"Bearer {store.access_token}"},
        )

        if response.status_code != 200:
            self._handle_error_response(response, "Session fetch failed")

        session = TidalSession.model_validate(response.json())

        # Update the stored country code
        if self._token_store:
            self._token_store.country_code = session.country_code
            self._token_store.user_id = str(session.user_id)
            self._persist_token_store()

        log.info(
            "Session info: user_id=%s, country=%s",
            session.user_id,
            session.country_code,
        )

        return session

    # -------------------------------------------------------------------------
    # Logout
    # -------------------------------------------------------------------------

    def logout(self) -> None:
        """
        Revoke the current access token and clear stored tokens.
        """
        if self._token_store and self._token_store.access_token:
            try:
                self._session.post(
                    f"{self.api_base_url}/logout",
                    headers={"Authorization": f"Bearer {self._token_store.access_token}"},
                )
                log.info("Tidal access token revoked.")
            except Exception as e:
                log.warning("Failed to revoke token (continuing with cleanup): %s", e)

        self._token_store = None

        if self.token_store_path.exists():
            self.token_store_path.unlink()
            log.info("Token store file removed: %s", self.token_store_path)

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _exchange_device_code(self, device_code: str) -> TidalTokenResponse:
        """
        Exchange a device code for tokens. Used during device auth polling.

        Raises internal sentinel exceptions for expected polling states.
        """
        response = self._session.post(
            f"{self.auth_base_url}/token",
            data={
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": self.scope_string,
            },
            auth=(self.client_id, self.client_secret),
        )

        if response.status_code == 200:
            return TidalTokenResponse.model_validate(response.json())

        # Parse error response for polling flow control
        error_data = self._parse_error(response)
        if error_data:
            if error_data.is_pending:
                raise _PendingAuthorization()
            if error_data.is_slow_down:
                raise _SlowDown()
            if error_data.is_expired:
                raise _DeviceCodeExpired()
            if error_data.is_denied:
                raise _AccessDenied()

        self._handle_error_response(response, "Device code exchange failed")
        raise TidalAuthError_("Unexpected error during device code exchange")  # unreachable

    def _parse_error(self, response: requests.Response) -> Optional[TidalAuthError]:
        """Parse an error response body into a TidalAuthError model."""
        try:
            data = response.json()
            return TidalAuthError.model_validate(data)
        except Exception:
            return None

    def _handle_error_response(self, response: requests.Response, context: str) -> None:
        """Log and raise on a failed HTTP response."""
        error_data = self._parse_error(response)
        error_msg = f"{context}: HTTP {response.status_code}"
        if error_data:
            error_msg += f" — {error_data}"
        else:
            error_msg += f" — {response.text[:500]}"

        log.error(error_msg)
        raise TidalAuthError_(error_msg, error_response=error_data)

    def _persist_token_store(self) -> None:
        """Write the current token store to disk."""
        if self._token_store:
            try:
                self._token_store.save(self.token_store_path)
                log.debug("Token store persisted to %s", self.token_store_path)
            except Exception as e:
                log.warning("Failed to persist token store: %s", e)


# -----------------------------------------------------------------------------
# Internal sentinel exceptions for device auth polling flow control.
# These never escape the class — they're caught in poll_for_token().
# -----------------------------------------------------------------------------


class _PendingAuthorization(Exception):
    pass


class _SlowDown(Exception):
    pass


class _DeviceCodeExpired(Exception):
    pass


class _AccessDenied(Exception):
    pass
