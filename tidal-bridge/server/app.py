"""
FastAPI REST server for the Tidal Bridge.

Exposes the Python Tidal API client over HTTP so the Go plex-sync service
(and any other local consumer) can fetch Tidal data without reimplementing
the Tidal OAuth2 + API logic in Go.

All endpoints are local-only by default (bound to 127.0.0.1) and require
no additional authentication — the assumption is that this runs on your
own machine or a trusted LAN alongside the Go service.

Endpoints:
  - GET  /health                          → service health + auth status
  - POST /auth/login                      → initiate device auth flow
  - POST /auth/refresh                    → force token refresh
  - GET  /auth/status                     → current token validity
  - POST /auth/logout                     → revoke tokens

  - GET  /session                         → Tidal session info (user, country)

  - GET  /favorites/ids                   → all favorite item IDs
  - GET  /favorites/tracks                → paginated favorite tracks
  - GET  /favorites/tracks/all            → all favorite tracks (auto-paginated)
  - GET  /favorites/albums                → paginated favorite albums
  - GET  /favorites/export                → favorites as NormalizedPlaylist

  - GET  /playlists                       → all user playlists
  - GET  /playlists/{uuid}                → playlist metadata
  - GET  /playlists/{uuid}/tracks         → all tracks in a playlist
  - GET  /playlists/{uuid}/export         → playlist as NormalizedPlaylist
  - GET  /playlists/export/all            → all playlists as NormalizedPlaylist[]

  - GET  /albums/{id}                     → album metadata
  - GET  /albums/{id}/tracks              → all tracks in an album

  - GET  /artists/{id}                    → artist metadata
  - GET  /artists/{id}/albums             → all albums by artist
  - GET  /artists/{id}/mixes              → available mix IDs for artist radio

  - GET  /tracks/{id}                     → track metadata
  - GET  /tracks/{id}/stream              → stream URL for a track
  - GET  /tracks/{id}/lyrics              → track lyrics

  - GET  /mixes/{id}/tracks               → all tracks in a mix
  - GET  /mixes/{id}/export               → mix as NormalizedPlaylist

  - GET  /search                          → search across Tidal

  - GET  /sync/snapshot                   → full export snapshot for plex-sync
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Load .env from project root (two levels up from server/)
# ---------------------------------------------------------------------------
_project_root = Path(__file__).resolve().parent.parent.parent
_env_path = _project_root / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tidal-bridge.server")

# ---------------------------------------------------------------------------
# Lazy-initialized global singletons
# ---------------------------------------------------------------------------
_auth_client = None
_api_client = None
_tidal_api = None
_startup_time: float = 0.0


def _get_env_required(key: str) -> str:
    """Get a required environment variable or raise."""
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}. Set it in your .env file or environment.")
    return value


def _init_clients() -> None:
    """
    Initialize the auth client, API client, and TidalAPI facade.
    Called once at startup (inside the lifespan context manager).
    """
    global _auth_client, _api_client, _tidal_api

    from core.api.client import TidalAPIClient
    from core.api.tidal_api import TidalAPI
    from core.auth.client import TidalAuthClient

    client_id = _get_env_required("TIDAL_CLIENT_ID")
    client_secret = _get_env_required("TIDAL_CLIENT_SECRET")
    country_code = os.getenv("TIDAL_COUNTRY_CODE", "US")
    cache_enabled = os.getenv("TIDAL_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")
    cache_ttl = int(os.getenv("TIDAL_CACHE_TTL", "3600"))
    data_dir = Path(os.getenv("DATA_DIR", "./data"))

    log.info("Initializing Tidal auth client (client_id=%s...)", client_id[:8])

    _auth_client = TidalAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        token_store_path=data_dir / "tidal_tokens.json",
    )

    # Try to load existing tokens — don't start device login automatically
    # (the user triggers that via POST /auth/login)
    try:
        _auth_client.ensure_valid_token()
        log.info("Loaded existing Tidal tokens — authenticated.")
    except Exception as e:
        log.warning("No valid Tidal tokens found: %s", e)
        log.warning("Use POST /auth/login to start the device authorization flow.")

    _api_client = TidalAPIClient(
        get_token=lambda: _auth_client.access_token if _auth_client else None,
        on_token_expired=lambda: _auth_client.refresh().access_token if _auth_client else None,
        cache_enabled=cache_enabled,
        cache_ttl=cache_ttl,
        cache_dir=data_dir / "cache",
    )

    # If we have a valid token, set up the API facade
    if _auth_client.is_authenticated:
        try:
            session = _auth_client.get_session()
            _tidal_api = TidalAPI(
                client=_api_client,
                user_id=str(session.user_id),
                country_code=session.country_code,
            )
            log.info(
                "TidalAPI ready (user_id=%s, country=%s)",
                session.user_id,
                session.country_code,
            )
        except Exception as e:
            log.error("Failed to initialize TidalAPI facade: %s", e)
            # Fall back to env-configured country code
            token_store = _auth_client.token_store
            user_id = token_store.user_id if token_store else "0"
            _tidal_api = TidalAPI(
                client=_api_client,
                user_id=user_id or "0",
                country_code=country_code,
            )
    else:
        log.info("TidalAPI facade not initialized — waiting for authentication.")


def _require_api() -> Any:
    """Get the TidalAPI instance or raise 401 if not authenticated."""
    if _tidal_api is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated with Tidal. POST /auth/login to authenticate.",
        )
    return _tidal_api


def _require_auth() -> Any:
    """Get the auth client or raise 500 if not initialized."""
    if _auth_client is None:
        raise HTTPException(
            status_code=500,
            detail="Tidal auth client not initialized. Check server startup logs.",
        )
    return _auth_client


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — init on startup, cleanup on shutdown."""
    global _startup_time
    _startup_time = time.time()
    log.info("Starting Tidal Bridge server...")

    try:
        _init_clients()
    except Exception as e:
        log.error("Failed to initialize clients: %s", e)
        log.error("The server will start but most endpoints will return errors.")

    yield

    # Cleanup
    log.info("Shutting down Tidal Bridge server...")
    if _api_client:
        _api_client.close()


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Tidal Bridge",
    description=(
        "Local REST API that bridges the Tidal API to the PlexAmp integration. "
        "Authenticates with YOUR OWN Tidal developer credentials and exposes "
        "collections, playlists, metadata, and stream URLs for the Go plex-sync service."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only service — permissive is fine
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "tidal-bridge"
    version: str = "0.1.0"
    authenticated: bool = False
    uptime_seconds: float = 0.0
    user_id: Optional[str] = None
    country_code: Optional[str] = None


class AuthStatusResponse(BaseModel):
    authenticated: bool = False
    token_valid: bool = False
    can_refresh: bool = False
    expires_at: Optional[float] = None
    seconds_until_expiry: Optional[float] = None
    user_id: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)
    client_id_prefix: Optional[str] = None


class DeviceAuthResponse(BaseModel):
    user_code: str
    verification_uri: str
    expires_in: int
    message: str


class SyncSnapshotResponse(BaseModel):
    """Full snapshot for the Go plex-sync service to consume."""

    timestamp: float
    user_id: str
    country_code: str
    favorites: Optional[Any] = None
    playlists: list[Any] = Field(default_factory=list)
    mix_playlists: list[Any] = Field(default_factory=list)


class StreamUrlResponse(BaseModel):
    track_id: int
    url: Optional[str] = None
    audio_quality: Optional[str] = None
    audio_mode: Optional[str] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    is_encrypted: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health():
    """Service health check with auth status."""
    auth = _auth_client
    token_store = auth.token_store if auth else None

    return HealthResponse(
        status="ok",
        authenticated=auth.is_authenticated if auth else False,
        uptime_seconds=round(time.time() - _startup_time, 1),
        user_id=token_store.user_id if token_store else None,
        country_code=token_store.country_code if token_store else None,
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.get("/auth/status", response_model=AuthStatusResponse, tags=["auth"])
async def auth_status():
    """Check current authentication status."""
    auth = _require_auth()
    store = auth.token_store

    if store is None:
        return AuthStatusResponse()

    return AuthStatusResponse(
        authenticated=auth.is_authenticated,
        token_valid=store.is_valid,
        can_refresh=store.can_refresh,
        expires_at=store.expires_at,
        seconds_until_expiry=store.seconds_until_expiry,
        user_id=store.user_id,
        scopes=store.scopes,
        client_id_prefix=store.client_id[:8] + "..." if store.client_id else None,
    )


@app.post("/auth/login", response_model=DeviceAuthResponse, tags=["auth"])
async def auth_login():
    """
    Initiate the Tidal device authorization flow.

    Returns a user code and verification URL. The user must visit the URL
    and enter the code to authorize. Then call POST /auth/poll to complete.
    """
    auth = _require_auth()

    try:
        device_auth = auth.request_device_authorization()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to start device auth: {e}")

    return DeviceAuthResponse(
        user_code=device_auth.user_code,
        verification_uri=device_auth.verification_uri,
        expires_in=device_auth.expires_in,
        message=(
            f"Visit {device_auth.verification_uri} and enter code: {device_auth.user_code} — then call POST /auth/poll"
        ),
    )


@app.post("/auth/poll", tags=["auth"])
async def auth_poll(
    device_code: str = Query(..., description="The device_code from /auth/login response"),
    interval: int = Query(5, description="Polling interval in seconds"),
    timeout: int = Query(300, description="Max seconds to wait for authorization"),
):
    """
    Poll for device authorization completion.

    This is a blocking call that waits until the user completes authorization
    or the timeout is reached. In a production setup you'd want to poll from
    the client side, but for a local bridge this is fine.
    """
    global _tidal_api
    auth = _require_auth()

    from core.auth.models import TidalDeviceAuthResponse

    # Construct a minimal device auth response for polling
    device_auth = TidalDeviceAuthResponse(
        deviceCode=device_code,
        userCode="(polling)",
        verificationUri="(polling)",
        expiresIn=timeout,
        interval=interval,
    )

    try:
        token_store = await asyncio.to_thread(auth.poll_for_token, device_auth)
    except Exception as e:
        raise HTTPException(status_code=408, detail=f"Authorization failed or timed out: {e}")

    # Re-initialize the API facade with fresh session info
    try:
        session = auth.get_session()
        from core.api.tidal_api import TidalAPI

        _tidal_api = TidalAPI(
            client=_api_client,
            user_id=str(session.user_id),
            country_code=session.country_code,
        )
    except Exception as e:
        log.error("Authenticated but failed to init API facade: %s", e)

    return {
        "status": "authenticated",
        "user_id": token_store.user_id,
        "expires_at": token_store.expires_at,
        "scopes": token_store.scopes,
    }


@app.post("/auth/refresh", tags=["auth"])
async def auth_refresh():
    """Force an immediate token refresh."""
    global _tidal_api
    auth = _require_auth()

    try:
        token_store = await asyncio.to_thread(auth.refresh)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token refresh failed: {e}")

    # Re-init API facade in case country/user changed
    try:
        session = auth.get_session()
        from core.api.tidal_api import TidalAPI

        _tidal_api = TidalAPI(
            client=_api_client,
            user_id=str(session.user_id),
            country_code=session.country_code,
        )
    except Exception:
        pass

    return {
        "status": "refreshed",
        "expires_at": token_store.expires_at,
        "seconds_until_expiry": token_store.seconds_until_expiry,
    }


@app.post("/auth/logout", tags=["auth"])
async def auth_logout():
    """Revoke tokens and clear stored credentials."""
    global _tidal_api
    auth = _require_auth()

    try:
        await asyncio.to_thread(auth.logout)
    except Exception as e:
        log.warning("Logout encountered an error: %s", e)

    _tidal_api = None
    return {"status": "logged_out"}


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@app.get("/session", tags=["session"])
async def get_session():
    """Get current Tidal session info (user ID, country, etc.)."""
    api = _require_api()
    try:
        session = await asyncio.to_thread(api.get_session)
        return session.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Session fetch failed: {e}")


# ---------------------------------------------------------------------------
# Favorites / Collection
# ---------------------------------------------------------------------------


@app.get("/favorites/ids", tags=["favorites"])
async def get_favorite_ids():
    """Get IDs of all favorited items (lightweight, no metadata)."""
    api = _require_api()
    try:
        ids = await asyncio.to_thread(api.get_favorite_ids)
        return ids.model_dump()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch favorite IDs: {e}")


@app.get("/favorites/tracks", tags=["favorites"])
async def get_favorite_tracks(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    order: str = Query("DATE", description="Sort: DATE, NAME, ARTIST, ALBUM"),
    order_direction: str = Query("DESC", description="ASC or DESC"),
):
    """Get favorite tracks with full metadata (paginated)."""
    api = _require_api()
    try:
        page = await asyncio.to_thread(
            api.get_favorite_tracks,
            limit=limit,
            offset=offset,
            order=order,
            order_direction=order_direction,
        )
        return page.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch favorite tracks: {e}")


@app.get("/favorites/tracks/all", tags=["favorites"])
async def get_all_favorite_tracks():
    """Get ALL favorite tracks (auto-paginated). May be slow for large collections."""
    api = _require_api()
    try:
        tracks = await asyncio.to_thread(api.get_all_favorite_tracks)
        return {
            "total": len(tracks),
            "tracks": [t.model_dump(by_alias=True) for t in tracks],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch all favorite tracks: {e}")


@app.get("/favorites/albums", tags=["favorites"])
async def get_favorite_albums(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Get favorite albums with full metadata (paginated)."""
    api = _require_api()
    try:
        page = await asyncio.to_thread(api.get_favorite_albums, limit=limit, offset=offset)
        return page.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch favorite albums: {e}")


@app.get("/favorites/export", tags=["favorites"])
async def export_favorites(
    playlist_name: str = Query("Tidal Favorites", description="Name for the exported playlist"),
):
    """
    Export all favorites as a NormalizedPlaylist for Plex sync.
    This is what the Go plex-sync service calls to create the favorites playlist.
    """
    api = _require_api()
    try:
        normalized = await asyncio.to_thread(api.export_favorites_for_plex, playlist_name)
        return normalized.model_dump()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to export favorites: {e}")


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


@app.get("/playlists", tags=["playlists"])
async def get_playlists():
    """Get all playlists owned by the authenticated user."""
    api = _require_api()
    try:
        playlists = await asyncio.to_thread(api.get_all_user_playlists)
        return {
            "total": len(playlists),
            "playlists": [p.model_dump(by_alias=True) for p in playlists],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch playlists: {e}")


@app.get("/playlists/export/all", tags=["playlists"])
async def export_all_playlists():
    """
    Export ALL playlists (user-created + favorited) as NormalizedPlaylist[].
    This is the primary bulk-sync endpoint for the Go plex-sync service.
    May take a while for accounts with many playlists.
    """
    api = _require_api()
    try:
        normalized_list = await asyncio.to_thread(api.export_all_playlists_for_plex)
        return {
            "total": len(normalized_list),
            "playlists": [p.model_dump() for p in normalized_list],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to export all playlists: {e}")


@app.get("/playlists/{uuid}", tags=["playlists"])
async def get_playlist(uuid: str):
    """Get playlist metadata by UUID."""
    api = _require_api()
    try:
        playlist = await asyncio.to_thread(api.get_playlist, uuid)
        return playlist.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch playlist {uuid}: {e}")


@app.get("/playlists/{uuid}/tracks", tags=["playlists"])
async def get_playlist_tracks(uuid: str):
    """Get all tracks in a playlist (auto-paginated, videos filtered out)."""
    api = _require_api()
    try:
        tracks = await asyncio.to_thread(api.get_all_playlist_tracks, uuid)
        return {
            "total": len(tracks),
            "tracks": [t.model_dump(by_alias=True) for t in tracks],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch playlist tracks: {e}")


@app.get("/playlists/{uuid}/export", tags=["playlists"])
async def export_playlist(uuid: str):
    """Export a playlist as a NormalizedPlaylist for Plex sync."""
    api = _require_api()
    try:
        normalized = await asyncio.to_thread(api.export_playlist_for_plex, uuid)
        return normalized.model_dump()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to export playlist: {e}")


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------


@app.get("/albums/{album_id}", tags=["albums"])
async def get_album(album_id: int):
    """Get album metadata by ID."""
    api = _require_api()
    try:
        album = await asyncio.to_thread(api.get_album, album_id)
        return album.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch album {album_id}: {e}")


@app.get("/albums/{album_id}/tracks", tags=["albums"])
async def get_album_tracks(album_id: int):
    """Get all tracks in an album (auto-paginated)."""
    api = _require_api()
    try:
        tracks = await asyncio.to_thread(api.get_all_album_tracks, album_id)
        return {
            "total": len(tracks),
            "tracks": [t.model_dump(by_alias=True) for t in tracks],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch album tracks: {e}")


# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------


@app.get("/artists/{artist_id}", tags=["artists"])
async def get_artist(artist_id: int):
    """Get artist metadata by ID."""
    api = _require_api()
    try:
        artist = await asyncio.to_thread(api.get_artist, artist_id)
        return artist.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch artist {artist_id}: {e}")


@app.get("/artists/{artist_id}/albums", tags=["artists"])
async def get_artist_albums(
    artist_id: int,
    include_eps_singles: bool = Query(True, description="Include EPs and singles"),
):
    """Get all albums by an artist (auto-paginated)."""
    api = _require_api()
    try:
        albums = await asyncio.to_thread(
            api.get_all_artist_albums,
            artist_id,
            include_eps_singles=include_eps_singles,
        )
        return {
            "total": len(albums),
            "albums": [a.model_dump(by_alias=True) for a in albums],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch artist albums: {e}")


@app.get("/artists/{artist_id}/mixes", tags=["artists"])
async def get_artist_mixes(artist_id: int):
    """
    Get available Tidal mix IDs for an artist.

    These mix IDs can be passed to /mixes/{id}/tracks or /mixes/{id}/export
    to generate artist radio playlists — this is how we replace AI-based
    Sonic Analysis with Tidal's native recommendation engine.
    """
    api = _require_api()
    try:
        mixes = await asyncio.to_thread(api.get_artist_mix_ids, artist_id)
        return {
            "artist_id": artist_id,
            "mixes": mixes,
            "hint": "Use GET /mixes/{mix_id}/export to create a Plex playlist from any of these mixes.",
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch artist mixes: {e}")


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


@app.get("/tracks/{track_id}", tags=["tracks"])
async def get_track(track_id: int):
    """Get track metadata by ID."""
    api = _require_api()
    try:
        track = await asyncio.to_thread(api.get_track, track_id)
        return track.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch track {track_id}: {e}")


@app.get("/tracks/{track_id}/stream", response_model=StreamUrlResponse, tags=["tracks"])
async def get_track_stream(
    track_id: int,
    quality: str = Query("LOSSLESS", description="Audio quality: LOW, HIGH, LOSSLESS, HI_RES, HI_RES_LOSSLESS"),
):
    """
    Get the stream URL and audio info for a track.

    Returns the CDN URL, codec info, and whether DRM encryption is present.
    Note: DRM-encrypted streams cannot be proxied — they require a licensed
    Tidal client for playback.
    """
    api = _require_api()
    try:
        stream = await asyncio.to_thread(api.get_track_stream, track_id, quality)

        manifest = api.decode_stream_manifest(stream)
        if manifest is None:
            return StreamUrlResponse(
                track_id=track_id,
                audio_quality=stream.audio_quality,
                audio_mode=stream.audio_mode,
                bit_depth=stream.bit_depth,
                sample_rate=stream.sample_rate,
                error="Failed to decode stream manifest (may be DASH format).",
            )

        return StreamUrlResponse(
            track_id=track_id,
            url=manifest.primary_url,
            audio_quality=stream.audio_quality,
            audio_mode=stream.audio_mode,
            bit_depth=stream.bit_depth,
            sample_rate=stream.sample_rate,
            is_encrypted=manifest.is_encrypted,
            error="DRM encrypted — cannot proxy." if manifest.is_encrypted else None,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to get stream for track {track_id}: {e}")


@app.get("/tracks/{track_id}/lyrics", tags=["tracks"])
async def get_track_lyrics(track_id: int):
    """Get lyrics for a track (plain text and/or time-synced)."""
    api = _require_api()
    try:
        lyrics = await asyncio.to_thread(api.get_track_lyrics, track_id)
        return lyrics.model_dump(by_alias=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch lyrics for track {track_id}: {e}")


# ---------------------------------------------------------------------------
# Mixes (Tidal Radio / DJ replacement — NO AI, uses Tidal's curation)
# ---------------------------------------------------------------------------


@app.get("/mixes/{mix_id}/tracks", tags=["mixes"])
async def get_mix_tracks(mix_id: str):
    """
    Get all tracks in a Tidal mix (auto-paginated).

    Mixes are Tidal's curated radio/recommendation playlists. This is how
    we replace PlexAmp's AI-based Sonic Analysis and DJ features — by using
    Tidal's own curation through your existing subscription.
    """
    api = _require_api()
    try:
        tracks = await asyncio.to_thread(api.get_all_mix_tracks, mix_id)
        return {
            "mix_id": mix_id,
            "total": len(tracks),
            "tracks": [t.model_dump(by_alias=True) for t in tracks],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch mix tracks: {e}")


@app.get("/mixes/{mix_id}/export", tags=["mixes"])
async def export_mix(
    mix_id: str,
    playlist_name: Optional[str] = Query(None, description="Custom name for the Plex playlist"),
):
    """
    Export a Tidal mix as a NormalizedPlaylist for Plex sync.

    This is the key endpoint for generating "artist radio" or "track radio"
    playlists in PlexAmp without any AI — it uses Tidal's native
    recommendation engine through the mixes API.

    To discover mix IDs, use:
      - GET /artists/{id}/mixes → artist-level mixes
      - GET /tracks/{id} → check the 'mixes' field in track metadata
    """
    api = _require_api()
    try:
        normalized = await asyncio.to_thread(api.export_mix_for_plex, mix_id, playlist_name)
        return normalized.model_dump()
    except Exception as e:
        raise HTTPException
