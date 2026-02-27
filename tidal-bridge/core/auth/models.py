"""
Pydantic models for Tidal OAuth2 authentication.

Covers device authorization flow, token responses, session info,
and persistent token storage. All scopes from the Tidal Developer
Dashboard are represented as an enum for type-safe scope management.
"""

from __future__ import annotations

import time
from enum import StrEnum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, computed_field


class OAuthScope(StrEnum):
    """
    All available Tidal OAuth2 scopes.
    These correspond to the scopes configurable in the Tidal Developer Dashboard.
    """

    USER_READ = "user.read"
    COLLECTION_READ = "collection.read"
    COLLECTION_WRITE = "collection.write"
    SEARCH_READ = "search.read"
    SEARCH_WRITE = "search.write"
    PLAYLISTS_READ = "playlists.read"
    PLAYLISTS_WRITE = "playlists.write"
    ENTITLEMENTS_READ = "entitlements.read"
    RECOMMENDATIONS_READ = "recommendations.read"
    PLAYBACK = "playback"

    @classmethod
    def all_scopes(cls) -> list[OAuthScope]:
        """Return all available scopes."""
        return list(cls)

    @classmethod
    def all_scopes_string(cls, separator: str = " ") -> str:
        """Return all scopes joined as a single string for OAuth requests."""
        return separator.join(s.value for s in cls)

    @classmethod
    def read_scopes(cls) -> list[OAuthScope]:
        """Return only read scopes (safe for read-only integrations)."""
        return [
            cls.USER_READ,
            cls.COLLECTION_READ,
            cls.SEARCH_READ,
            cls.PLAYLISTS_READ,
            cls.ENTITLEMENTS_READ,
            cls.RECOMMENDATIONS_READ,
        ]

    @classmethod
    def bridge_scopes(cls) -> list[OAuthScope]:
        """
        Return the scopes needed for the PlexAmp bridge.
        Includes read access to collections/playlists/recommendations
        plus playback for stream URL resolution.
        """
        return [
            cls.USER_READ,
            cls.COLLECTION_READ,
            cls.SEARCH_READ,
            cls.PLAYLISTS_READ,
            cls.ENTITLEMENTS_READ,
            cls.RECOMMENDATIONS_READ,
            cls.PLAYBACK,
        ]

    @classmethod
    def bridge_scopes_string(cls, separator: str = " ") -> str:
        """Return bridge scopes joined as a single string for OAuth requests."""
        return separator.join(s.value for s in cls.bridge_scopes())


class TidalDeviceAuthResponse(BaseModel):
    """
    Response from POST /v1/oauth2/device_authorization.
    Used in the device-code OAuth2 flow where the user visits
    a URL and enters a code to authorize the app.
    """

    device_code: str = Field(
        ...,
        description="The device verification code to poll with.",
        alias="deviceCode",
    )
    user_code: str = Field(
        ...,
        description="The short code the user enters at the verification URL.",
        alias="userCode",
    )
    verification_uri: str = Field(
        ...,
        description="The URL the user visits to authorize the device.",
        alias="verificationUri",
    )
    verification_uri_complete: Optional[str] = Field(
        default=None,
        description="Full URL with the user code pre-filled.",
        alias="verificationUriComplete",
    )
    expires_in: int = Field(
        ...,
        description="Number of seconds until the device code expires.",
        alias="expiresIn",
    )
    interval: int = Field(
        default=5,
        description="Polling interval in seconds.",
    )

    model_config = {"populate_by_name": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expires_at(self) -> float:
        """Absolute timestamp when this device code expires."""
        return time.time() + self.expires_in


class TidalTokenResponse(BaseModel):
    """
    Response from POST /v1/oauth2/token.
    Returned after successful device-code polling, authorization-code exchange,
    or refresh-token grant.
    """

    access_token: str = Field(
        ...,
        description="Bearer token for Tidal API requests.",
        alias="access_token",
    )
    refresh_token: Optional[str] = Field(
        default=None,
        description="Long-lived token for obtaining new access tokens.",
        alias="refresh_token",
    )
    token_type: str = Field(
        default="Bearer",
        description="Token type, always 'Bearer'.",
        alias="token_type",
    )
    expires_in: int = Field(
        ...,
        description="Access token lifetime in seconds.",
        alias="expires_in",
    )
    scope: Optional[str] = Field(
        default=None,
        description="Space-separated list of granted scopes.",
    )
    user_id: Optional[str] = Field(
        default=None,
        description="Tidal user ID associated with this token.",
        alias="user",
    )

    model_config = {"populate_by_name": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expires_at(self) -> float:
        """Absolute timestamp when this access token expires."""
        return time.time() + self.expires_in

    @property
    def granted_scopes(self) -> list[str]:
        """Parse the scope string into a list of individual scopes."""
        if not self.scope:
            return []
        return self.scope.split()


class TidalSession(BaseModel):
    """
    Response from GET /v1/sessions.
    Contains information about the authenticated user's active session.
    """

    session_id: str = Field(..., alias="sessionId")
    user_id: int = Field(..., alias="userId")
    country_code: str = Field(..., alias="countryCode")
    channel_id: Optional[int] = Field(default=None, alias="channelId")
    partner_id: Optional[int] = Field(default=None, alias="partnerId")
    client_id: Optional[str] = Field(default=None, alias="clientId")

    model_config = {"populate_by_name": True}


class TidalTokenStore(BaseModel):
    """
    Persistent storage model for OAuth2 tokens.
    Serialized to disk as JSON so the bridge can resume sessions
    without re-authenticating every time.
    """

    access_token: str = Field(..., description="Current access token.")
    refresh_token: str = Field(..., description="Refresh token for renewal.")
    token_type: str = Field(default="Bearer")
    expires_at: float = Field(
        ...,
        description="Unix timestamp when the access token expires.",
    )
    user_id: Optional[str] = Field(
        default=None,
        description="Tidal user ID.",
    )
    country_code: Optional[str] = Field(
        default=None,
        description="User's country code from session.",
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="List of granted OAuth scopes.",
    )
    client_id: Optional[str] = Field(
        default=None,
        description="The Tidal developer app client_id used to obtain this token.",
    )
    created_at: float = Field(
        default_factory=time.time,
        description="Unix timestamp when this token store was created.",
    )
    last_refreshed_at: Optional[float] = Field(
        default=None,
        description="Unix timestamp when the token was last refreshed.",
    )

    model_config = {"populate_by_name": True}

    @property
    def is_expired(self) -> bool:
        """Check if the access token has expired (with 30s safety margin)."""
        return time.time() >= (self.expires_at - 30)

    @property
    def seconds_until_expiry(self) -> float:
        """Seconds remaining until access token expires."""
        remaining = self.expires_at - time.time()
        return max(0.0, remaining)

    @property
    def is_valid(self) -> bool:
        """Check if we have a usable (non-expired) access token."""
        return bool(self.access_token) and not self.is_expired

    @property
    def can_refresh(self) -> bool:
        """Check if we have a refresh token available."""
        return bool(self.refresh_token)

    @classmethod
    def from_token_response(
        cls,
        response: TidalTokenResponse,
        client_id: Optional[str] = None,
        country_code: Optional[str] = None,
        existing_refresh_token: Optional[str] = None,
    ) -> TidalTokenStore:
        """
        Create a TidalTokenStore from a fresh token response.
        If the response doesn't include a refresh_token (e.g. on refresh grant),
        the existing_refresh_token is preserved.
        """
        return cls(
            access_token=response.access_token,
            refresh_token=response.refresh_token or existing_refresh_token or "",
            token_type=response.token_type,
            expires_at=response.expires_at,
            user_id=response.user_id,
            country_code=country_code,
            scopes=response.granted_scopes,
            client_id=client_id,
            created_at=time.time(),
            last_refreshed_at=time.time(),
        )

    def save(self, path: Path) -> None:
        """Persist the token store to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> Optional[TidalTokenStore]:
        """Load a token store from a JSON file, or return None if not found."""
        if not path.exists():
            return None
        try:
            return cls.model_validate_json(path.read_text())
        except Exception:
            return None


class TidalAuthError(BaseModel):
    """
    Error response from Tidal OAuth2 endpoints.
    Matches the JSON error body returned on 4xx/5xx responses.
    """

    error: str = Field(..., description="Error code, e.g. 'authorization_pending', 'invalid_grant'.")
    error_description: Optional[str] = Field(
        default=None,
        description="Human-readable error description.",
        alias="error_description",
    )
    sub_status: Optional[int] = Field(
        default=None,
        description="Tidal-specific sub-status code.",
        alias="sub_status",
    )

    model_config = {"populate_by_name": True}

    @property
    def is_pending(self) -> bool:
        """Check if the device auth is still waiting for user action."""
        return self.error == "authorization_pending"

    @property
    def is_slow_down(self) -> bool:
        """Check if we're polling too fast."""
        return self.error == "slow_down"

    @property
    def is_expired(self) -> bool:
        """Check if the device code has expired."""
        return self.error == "expired_token"

    @property
    def is_denied(self) -> bool:
        """Check if the user denied authorization."""
        return self.error == "access_denied"

    @property
    def is_invalid_grant(self) -> bool:
        """Check if the refresh token is invalid/revoked."""
        return self.error == "invalid_grant"

    def __str__(self) -> str:
        msg = f"TidalAuthError({self.error})"
        if self.error_description:
            msg += f": {self.error_description}"
        return msg
