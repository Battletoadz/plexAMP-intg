"""
Low-level HTTP client for the Tidal API.

Handles authenticated requests with:
  - Bearer token injection from the auth client
  - Automatic token refresh on 401 responses
  - Response caching via requests-cache
  - Retry logic with exponential backoff
  - Debug logging and optional response dumping

Inspired by tiddl's TidalClient (https://github.com/oskvr37/tiddl)
but adapted for the PlexAmp bridge architecture:
  - Uses YOUR OWN Tidal developer credentials (no hardcoded keys)
  - Integrates with TidalAuthClient for seamless token lifecycle
  - Supports both cached and uncached request modes
  - Provides typed response parsing via Pydantic models
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional, Type, TypeVar

from pydantic import BaseModel
from requests import Response, Session
from requests.exceptions import ConnectionError, JSONDecodeError, Timeout

log = logging.getLogger(__name__)

# Tidal API v1 base URL
TIDAL_API_V1_URL = "https://api.tidal.com/v1"

# Retry configuration
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0  # seconds — exponential backoff base
RETRY_MAX_DELAY = 30.0  # seconds — cap on backoff delay

# HTTP status codes
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_MIN = 500

T = TypeVar("T", bound=BaseModel)


class TidalAPIError(Exception):
    """Raised when a Tidal API request fails after all retries."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        endpoint: Optional[str] = None,
        response_body: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
        self.response_body = response_body

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code:
            parts.append(f"status={self.status_code}")
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        return " | ".join(parts)


class TidalRateLimitError(TidalAPIError):
    """Raised when the Tidal API returns 429 Too Many Requests."""

    def __init__(self, retry_after: Optional[int] = None, **kwargs: Any):
        super().__init__("Rate limited by Tidal API", **kwargs)
        self.retry_after = retry_after


class TidalAPIClient:
    """
    Low-level HTTP client for Tidal API v1.

    Manages authenticated requests, caching, retries, and token refresh.
    This client is designed to be used by the higher-level TidalAPI facade
    and should not typically be called directly by application code.

    Usage:
        from core.auth.client import TidalAuthClient

        auth = TidalAuthClient(client_id="...", client_secret="...")
        auth.load_or_login()

        api_client = TidalAPIClient(
            get_token=lambda: auth.access_token,
            on_token_expired=lambda: auth.refresh().access_token,
        )

        # Fetch and parse into a Pydantic model
        track = api_client.fetch(Track, "tracks/12345", {"countryCode": "US"})
    """

    def __init__(
        self,
        get_token: Callable[[], Optional[str]],
        on_token_expired: Optional[Callable[[], Optional[str]]] = None,
        base_url: str = TIDAL_API_V1_URL,
        cache_enabled: bool = True,
        cache_ttl: int = 3600,
        cache_dir: Optional[Path] = None,
        debug_dir: Optional[Path] = None,
        timeout: int = 30,
    ):
        """
        Initialize the Tidal API HTTP client.

        Args:
            get_token: Callable that returns the current access token string.
                       Called before every request to get the latest token.
            on_token_expired: Optional callable invoked on 401 responses.
                              Should refresh the token and return the new access
                              token string, or None if refresh fails.
            base_url: Tidal API v1 base URL (override for testing).
            cache_enabled: Whether to cache GET responses.
            cache_ttl: Default cache TTL in seconds.
            cache_dir: Directory for the request cache database.
                       Defaults to ./data/cache.
            debug_dir: If set, API responses are dumped as JSON files here
                       for debugging. Set to None to disable.
            timeout: HTTP request timeout in seconds.
        """
        self._get_token = get_token
        self._on_token_expired = on_token_expired
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._debug_dir = debug_dir

        # --- Session setup ---
        # We use a plain requests.Session here. If cache_enabled is True,
        # we swap it for a requests_cache.CachedSession.
        self._cache_enabled = cache_enabled
        self._default_cache_ttl = cache_ttl

        if cache_enabled:
            try:
                from requests_cache import CachedSession

                cache_path = cache_dir or Path("./data/cache")
                cache_path.mkdir(parents=True, exist_ok=True)
                cache_name = str(cache_path / "tidal_api_cache")

                self._session: Session = CachedSession(
                    cache_name=cache_name,
                    backend="sqlite",
                    expire_after=cache_ttl,
                    allowable_methods=["GET"],
                    stale_if_error=True,
                )
                log.info(
                    "Request caching enabled (TTL=%ds, path=%s)",
                    cache_ttl,
                    cache_name,
                )
            except ImportError:
                log.warning("requests-cache not installed; caching disabled. Install with: pip install requests-cache")
                self._session = Session()
                self._cache_enabled = False
        else:
            self._session = Session()

        # Default headers (Authorization is set per-request via _auth_headers)
        self._session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "PlexAmp-Tidal-Bridge/0.1",
            }
        )

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def cache_enabled(self) -> bool:
        return self._cache_enabled

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def fetch(
        self,
        model: Type[T],
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        cache_ttl: Optional[int] = None,
        skip_cache: bool = False,
    ) -> T:
        """
        Fetch data from a Tidal API endpoint and parse it into a Pydantic model.

        This is the primary method used by the TidalAPI facade. It handles:
          - Token injection
          - Automatic retry on transient failures
          - 401 → token refresh → retry
          - Response caching
          - Debug response dumping

        Args:
            model: Pydantic model class to parse the response into.
            endpoint: API endpoint path (e.g., "tracks/12345").
                      Will be appended to the base URL.
            params: Optional query parameters dict.
            cache_ttl: Override the default cache TTL for this request (seconds).
                       Pass 0 or -1 to skip caching for this request.
            skip_cache: If True, bypass the cache entirely for this request.

        Returns:
            An instance of the given Pydantic model.

        Raises:
            TidalAPIError: If the request fails after all retries.
            TidalRateLimitError: If rate-limited and retries are exhausted.
        """
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        params = params or {}

        # Determine cache behavior for this request
        expire_after = self._resolve_cache_ttl(cache_ttl, skip_cache)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._make_request(
                    method="GET",
                    url=url,
                    params=params,
                    expire_after=expire_after,
                    attempt=attempt,
                )

                # Handle 401 — try token refresh once
                if response.status_code == HTTP_UNAUTHORIZED:
                    refreshed_token = self._try_token_refresh()
                    if refreshed_token and attempt < MAX_RETRIES:
                        log.info("Token refreshed after 401 — retrying %s", endpoint)
                        continue
                    raise TidalAPIError(
                        message="Authentication failed (401) — token refresh unsuccessful.",
                        status_code=HTTP_UNAUTHORIZED,
                        endpoint=endpoint,
                    )

                # Handle 429 — rate limit with backoff
                if response.status_code == HTTP_TOO_MANY_REQUESTS:
                    retry_after = self._get_retry_after(response)
                    if attempt < MAX_RETRIES:
                        wait = retry_after or self._backoff_delay(attempt)
                        log.warning(
                            "Rate limited on %s — waiting %.1fs (attempt %d/%d)",
                            endpoint,
                            wait,
                            attempt,
                            MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue
                    raise TidalRateLimitError(
                        retry_after=retry_after,
                        status_code=HTTP_TOO_MANY_REQUESTS,
                        endpoint=endpoint,
                    )

                # Handle 5xx — server error with backoff
                if response.status_code >= HTTP_SERVER_ERROR_MIN:
                    if attempt < MAX_RETRIES:
                        wait = self._backoff_delay(attempt)
                        log.warning(
                            "Server error %d on %s — retrying in %.1fs (attempt %d/%d)",
                            response.status_code,
                            endpoint,
                            wait,
                            attempt,
                            MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue

                # Parse JSON
                data = self._parse_json(response, endpoint, attempt)
                if data is None:
                    continue  # retry on JSON parse failure

                # Log cache hit/miss
                self._log_cache_status(response, endpoint, params)

                # Dump debug response if configured
                self._dump_debug(endpoint, params, response.status_code, data)

                # Handle non-200 responses that aren't retryable
                if response.status_code != HTTP_OK:
                    raise TidalAPIError(
                        message=f"Tidal API error: HTTP {response.status_code}",
                        status_code=response.status_code,
                        endpoint=endpoint,
                        response_body=data,
                    )

                # Parse into model
                return model.model_validate(data)

            except (ConnectionError, Timeout) as e:
                if attempt >= MAX_RETRIES:
                    raise TidalAPIError(
                        message=f"Connection failed after {MAX_RETRIES} attempts: {e}",
                        endpoint=endpoint,
                    ) from e
                wait = self._backoff_delay(attempt)
                log.warning(
                    "Connection error on %s — retrying in %.1fs (attempt %d/%d): %s",
                    endpoint,
                    wait,
                    attempt,
                    MAX_RETRIES,
                    e,
                )
                time.sleep(wait)

        # Should not reach here, but just in case
        raise TidalAPIError(
            message=f"Request failed after {MAX_RETRIES} attempts",
            endpoint=endpoint,
        )

    def fetch_raw(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        skip_cache: bool = True,
    ) -> dict[str, Any]:
        """
        Fetch raw JSON data from a Tidal API endpoint without model parsing.

        Useful for endpoints where the response schema is unknown or variable.

        Args:
            endpoint: API endpoint path.
            params: Optional query parameters.
            skip_cache: Whether to skip caching (default True).

        Returns:
            Raw JSON response as a dict.

        Raises:
            TidalAPIError: On request failure.
        """
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        params = params or {}
        expire_after = self._resolve_cache_ttl(None, skip_cache)

        response = self._make_request(
            method="GET",
            url=url,
            params=params,
            expire_after=expire_after,
            attempt=1,
        )

        if response.status_code != HTTP_OK:
            raise TidalAPIError(
                message=f"Tidal API error: HTTP {response.status_code}",
                status_code=response.status_code,
                endpoint=endpoint,
            )

        try:
            return response.json()
        except JSONDecodeError as e:
            raise TidalAPIError(
                message=f"Invalid JSON response from {endpoint}: {e}",
                endpoint=endpoint,
            ) from e

    def post(
        self,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Make an authenticated POST request to the Tidal API.

        Used for write operations (e.g., adding to collection, creating playlists).

        Args:
            endpoint: API endpoint path.
            data: Form data or JSON body.
            params: Optional query parameters.

        Returns:
            JSON response as a dict, or empty dict for 204 No Content.

        Raises:
            TidalAPIError: On request failure.
        """
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        headers = self._auth_headers()

        try:
            response = self._session.post(
                url,
                data=data,
                params=params or {},
                headers=headers,
                timeout=self._timeout,
            )
        except (ConnectionError, Timeout) as e:
            raise TidalAPIError(
                message=f"POST request failed: {e}",
                endpoint=endpoint,
            ) from e

        if response.status_code == HTTP_UNAUTHORIZED and self._on_token_expired:
            new_token = self._try_token_refresh()
            if new_token:
                headers = self._auth_headers()
                response = self._session.post(
                    url,
                    data=data,
                    params=params or {},
                    headers=headers,
                    timeout=self._timeout,
                )

        if response.status_code not in (200, 201, 204):
            body = None
            try:
                body = response.json()
            except Exception:
                pass
            raise TidalAPIError(
                message=f"POST {endpoint} failed: HTTP {response.status_code}",
                status_code=response.status_code,
                endpoint=endpoint,
                response_body=body,
            )

        if response.status_code == 204:
            return {}

        try:
            return response.json()
        except JSONDecodeError:
            return {}

    def delete(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Make an authenticated DELETE request to the Tidal API.

        Args:
            endpoint: API endpoint path.
            params: Optional query parameters.

        Returns:
            True if the deletion was successful.

        Raises:
            TidalAPIError: On request failure.
        """
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        headers = self._auth_headers()

        try:
            response = self._session.delete(
                url,
                params=params or {},
                headers=headers,
                timeout=self._timeout,
            )
        except (ConnectionError, Timeout) as e:
            raise TidalAPIError(
                message=f"DELETE request failed: {e}",
                endpoint=endpoint,
            ) from e

        if response.status_code == HTTP_UNAUTHORIZED and self._on_token_expired:
            new_token = self._try_token_refresh()
            if new_token:
                headers = self._auth_headers()
                response = self._session.delete(
                    url,
                    params=params or {},
                    headers=headers,
                    timeout=self._timeout,
                )

        if response.status_code not in (200, 204):
            raise TidalAPIError(
                message=f"DELETE {endpoint} failed: HTTP {response.status_code}",
                status_code=response.status_code,
                endpoint=endpoint,
            )

        return True

    # -------------------------------------------------------------------------
    # Cache Management
    # -------------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear all cached API responses."""
        if self._cache_enabled and hasattr(self._session, "cache"):
            self._session.cache.clear()  # type: ignore[union-attr]
            log.info("API response cache cleared.")

    def remove_expired_cache(self) -> None:
        """Remove only expired entries from the cache."""
        if self._cache_enabled and hasattr(self._session, "cache"):
            self._session.cache.delete(expired=True)  # type: ignore[union-attr]
            log.info("Expired cache entries removed.")

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Build Authorization header with the current access token."""
        token = self._get_token()
        if not token:
            log.warning("No access token available — request will likely fail with 401.")
            return {}
        return {"Authorization": f"Bearer {token}"}

    def _make_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        expire_after: Optional[int],
        attempt: int,
    ) -> Response:
        """
        Execute a single HTTP request with auth headers and optional cache control.
        """
        headers = self._auth_headers()
        kwargs: dict[str, Any] = {
            "params": params,
            "headers": headers,
            "timeout": self._timeout,
        }

        # If using requests-cache, pass expire_after for per-request TTL control
        if self._cache_enabled and expire_after is not None:
            kwargs["expire_after"] = expire_after

        if method.upper() == "GET":
            return self._session.get(url, **kwargs)
        elif method.upper() == "POST":
            return self._session.post(url, **kwargs)
        else:
            return self._session.request(method, url, **kwargs)

    def _try_token_refresh(self) -> Optional[str]:
        """
        Attempt to refresh the access token via the callback.
        Returns the new token string, or None if refresh fails.
        """
        if not self._on_token_expired:
            return None

        try:
            new_token = self._on_token_expired()
            if new_token:
                log.info("Access token refreshed successfully via callback.")
                return new_token
            log.warning("Token refresh callback returned None — refresh failed.")
            return None
        except Exception as e:
            log.error("Token refresh callback raised an exception: %s", e)
            return None

    def _parse_json(
        self,
        response: Response,
        endpoint: str,
        attempt: int,
    ) -> Optional[dict[str, Any]]:
        """
        Parse JSON from a response, with retry on decode failure.
        Returns None if parsing fails and the caller should retry.
        """
        try:
            return response.json()
        except JSONDecodeError as e:
            if attempt >= MAX_RETRIES:
                raise TidalAPIError(
                    message=f"Invalid JSON after {MAX_RETRIES} attempts: {e}",
                    status_code=response.status_code,
                    endpoint=endpoint,
                ) from e
            wait = self._backoff_delay(attempt)
            log.warning(
                "JSON decode error on %s — retrying in %.1fs (attempt %d/%d): %s",
                endpoint,
                wait,
                attempt,
                MAX_RETRIES,
                e,
            )
            time.sleep(wait)
            return None

    def _resolve_cache_ttl(
        self,
        cache_ttl: Optional[int],
        skip_cache: bool,
    ) -> Optional[int]:
        """
        Determine the effective cache TTL for a request.

        Returns:
            - None if caching is disabled or cache_ttl is not set (use session default)
            - 0 or -1 to skip caching for this request
            - Positive int for a custom TTL
        """
        if not self._cache_enabled:
            return None

        if skip_cache:
            return 0  # requests-cache: 0 means do not cache

        if cache_ttl is not None:
            return cache_ttl

        return None  # use session default

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Calculate exponential backoff delay for a given attempt number."""
        delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
        return min(delay, RETRY_MAX_DELAY)

    @staticmethod
    def _get_retry_after(response: Response) -> Optional[int]:
        """Extract Retry-After header value if present."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return int(retry_after)
            except ValueError:
                pass
        return None

    def _log_cache_status(
        self,
        response: Response,
        endpoint: str,
        params: dict[str, Any],
    ) -> None:
        """Log whether a response was served from cache or fetched fresh."""
        if not self._cache_enabled:
            return

        # requests-cache adds a from_cache attribute to responses
        from_cache = getattr(response, "from_cache", False)
        status = "CACHE HIT" if from_cache else "CACHE MISS"
        log.debug("%s %s %s [%d]", status, endpoint, params, response.status_code)

    def _dump_debug(
        self,
        endpoint: str,
        params: dict[str, Any],
        status_code: int,
        data: Any,
    ) -> None:
        """Dump API response to a JSON file for debugging, if debug_dir is set."""
        if not self._debug_dir:
            return

        try:
            # Sanitize endpoint for use as filename
            safe_endpoint = endpoint.replace("/", "_").replace("?", "_")
            file_path = self._debug_dir / f"{safe_endpoint}.json"
            file_path.parent.mkdir(parents=True, exist_ok=True)

            file_path.write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "params": params,
                        "status_code": status_code,
                        "data": data,
                    },
                    indent=2,
                    default=str,
                )
            )
            log.debug("Debug response dumped to %s", file_path)
        except Exception as e:
            log.debug("Failed to dump debug response: %s", e)

    def close(self) -> None:
        """Close the HTTP session and release resources."""
        self._session.close()

    def __enter__(self) -> TidalAPIClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
