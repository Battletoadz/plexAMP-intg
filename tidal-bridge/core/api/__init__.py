"""
Tidal API client layer.

Provides the low-level HTTP client (TidalAPIClient) that handles
authenticated requests to the Tidal API with caching, retries,
and automatic token refresh.

Also exposes the high-level TidalAPI facade that wraps all Tidal
endpoints into typed, easy-to-use methods.
"""

from core.api.client import TidalAPIClient
from core.api.tidal_api import TidalAPI

__all__ = [
    "TidalAPIClient",
    "TidalAPI",
]
