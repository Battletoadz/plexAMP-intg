"""
tidal-bridge core package

Tidal API client library for PlexAmp integration.
Handles OAuth2 authentication, API communication, metadata fetching,
and stream URL resolution using your own Tidal developer credentials.

Forked from and inspired by tiddl (https://github.com/oskvr37/tiddl)
"""

from core.auth.client import TidalAuthClient
from core.api.client import TidalAPIClient
from core.api.tidal_api import TidalAPI

__all__ = [
    "TidalAuthClient",
    "TidalAPIClient",
    "TidalAPI",
]
