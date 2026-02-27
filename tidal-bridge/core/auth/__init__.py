"""
Tidal OAuth2 authentication module.

Handles device-code and authorization-code OAuth2 flows using
the user's own Tidal developer dashboard credentials.
No hardcoded secrets — everything comes from your .env or environment.
"""

from core.auth.client import TidalAuthClient
from core.auth.models import (
    OAuthScope,
    TidalDeviceAuthResponse,
    TidalSession,
    TidalTokenResponse,
    TidalTokenStore,
)

__all__ = [
    "TidalAuthClient",
    "TidalTokenResponse",
    "TidalDeviceAuthResponse",
    "TidalSession",
    "TidalTokenStore",
    "OAuthScope",
]
