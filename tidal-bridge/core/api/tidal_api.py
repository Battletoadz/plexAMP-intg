"""
High-level Tidal API facade.

Wraps all Tidal API v1 endpoints into typed, easy-to-use methods.
This is the primary interface that the bridge service and the REST
server use to interact with Tidal.

Each method:
  - Delegates HTTP work to the low-level TidalAPIClient
  - Returns typed Pydantic models from core.models
  - Handles pagination transparently where needed
  - Provides sensible defaults for limit/offset/quality params

Endpoint coverage mirrors tiddl's TidalAPI but adds:
  - Full favorites listing (tracks, albums, artists, playlists)
  - User playlist enumeration
  - Mix/recommendation fetching for DJ/radio replacement
  - Stream manifest decoding
  - Normalized export helpers for the Plex sync layer

Inspired by tiddl (https://github.com/oskvr37/tiddl) — rewritten for
the PlexAmp bridge with YOUR OWN Tidal developer credentials.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

from core.api.client import TidalAPIClient
from core.models import (
    Album,
    AlbumItems,
    AlbumItemsCredits,
    AlbumReview,
    Artist,
    ArtistAlbums,
    ArtistVideos,
    AudioQuality,
    FavoriteAlbums,
    FavoriteArtists,
    FavoriteIds,
    FavoritePlaylists,
    FavoriteTracks,
    MixItems,
    NormalizedPlaylist,
    NormalizedTrack,
    Playlist,
    PlaylistItems,
    SearchResults,
    SessionResponse,
    StreamManifest,
    Track,
    TrackLyrics,
    TrackStream,
    Video,
    VideoStream,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default page sizes — these match Tidal's documented limits
# ---------------------------------------------------------------------------

class Limits:
    """Default and maximum page sizes for paginated Tidal endpoints."""

    ALBUM_ITEMS = 50
    ALBUM_ITEMS_MAX = 100

    ARTIST_ALBUMS = 50
    ARTIST_ALBUMS_MAX = 100

    ARTIST_VIDEOS = 25
    ARTIST_VIDEOS_MAX = 100

    PLAYLIST_ITEMS = 50
    PLAYLIST_ITEMS_MAX = 100

    MIX_ITEMS = 50
    MIX_ITEMS_MAX = 100

    FAVORITES = 50
    FAVORITES_MAX = 100

    USER_PLAYLISTS = 50
    USER_PLAYLISTS_MAX = 100

    SEARCH = 25
    SEARCH_MAX = 50


class TidalAPI:
    """
    High-level facade over the Tidal API v1.

    All methods require an authenticated TidalAPIClient and the user's
    country code (from the session endpoint).

    Usage::

        from core.auth.client import TidalAuthClient
        from core.api.client import TidalAPIClient
        from core.api.tidal_api import TidalAPI

        auth = TidalAuthClient(client_id="...", client_secret="...")
        token_store = auth.load_or_login()
        session = auth.get_session()

        api_client = TidalAPIClient(
            get_token=lambda: auth.access_token,
            on_token_expired=lambda: auth.refresh().access_token,
        )

        api = TidalAPI(
            client=api_client,
            user_id=str(session.user_id),
            country_code=session.country_code,
        )

        # Now use it:
        favorites = api.get_favorite_ids()
        playlist = api.get_playlist("some-uuid")
        stream = api.get_track_stream(12345)
    """

    def __init__(
        self,
        client: TidalAPIClient,
        user_id: str,
        country_code: str,
    ) -> None:
        self.client = client
        self.user_id = user_id
        self.country_code = country_code

    # =====================================================================
    # Albums
    # =====================================================================

    def get_album(self, album_id: int | str) -> Album:
        """Fetch album metadata by ID."""
        return self.client.fetch(
            Album,
            f"albums/{album_id}",
            {"countryCode": self.country_code},
            cache_ttl=3600,
        )

    def get_album_items(
        self,
        album_id: int | str,
        limit: int = Limits.ALBUM_ITEMS,
        offset: int = 0,
    ) -> AlbumItems:
        """Fetch tracks and videos in an album (paginated)."""
        return self.client.fetch(
            AlbumItems,
            f"albums/{album_id}/items",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.ALBUM_ITEMS_MAX),
                "offset": offset,
            },
            cache_ttl=3600,
        )

    def get_all_album_tracks(self, album_id: int | str) -> list[Track]:
        """
        Fetch ALL tracks in an album, handling pagination automatically.
        Returns only tracks (videos are filtered out).
        """
        tracks: list[Track] = []
        offset = 0
        limit = Limits.ALBUM_ITEMS_MAX

        while True:
            page = self.get_album_items(album_id, limit=limit, offset=offset)
            for item in page.items:
                t = item.as_track()
                if t is not None:
                    tracks.append(t)
            if not page.has_more:
                break
            offset = page.next_offset

        log.debug("Fetched %d tracks from album %s", len(tracks), album_id)
        return tracks

    def get_album_items_credits(
        self,
        album_id: int | str,
        limit: int = Limits.ALBUM_ITEMS,
        offset: int = 0,
    ) -> AlbumItemsCredits:
        """Fetch per-track credits for an album (paginated)."""
        return self.client.fetch(
            AlbumItemsCredits,
            f"albums/{album_id}/items/credits",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.ALBUM_ITEMS_MAX),
                "offset": offset,
            },
            cache_ttl=3600,
        )

    def get_album_review(self, album_id: int | str) -> AlbumReview:
        """Fetch editorial review text for an album."""
        return self.client.fetch(
            AlbumReview,
            f"albums/{album_id}/review",
            {"countryCode": self.country_code},
            cache_ttl=3600,
        )

    # =====================================================================
    # Artists
    # =====================================================================

    def get_artist(self, artist_id: int | str) -> Artist:
        """Fetch artist metadata by ID."""
        return self.client.fetch(
            Artist,
            f"artists/{artist_id}",
            {"countryCode": self.country_code},
            cache_ttl=3600,
        )

    def get_artist_albums(
        self,
        artist_id: int | str,
        limit: int = Limits.ARTIST_ALBUMS,
        offset: int = 0,
        filter_type: str = "ALBUMS",
    ) -> ArtistAlbums:
        """
        Fetch albums by an artist (paginated).

        Args:
            filter_type: "ALBUMS" or "EPSANDSINGLES".
        """
        return self.client.fetch(
            ArtistAlbums,
            f"artists/{artist_id}/albums",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.ARTIST_ALBUMS_MAX),
                "offset": offset,
                "filter": filter_type,
            },
            cache_ttl=3600,
        )

    def get_all_artist_albums(
        self,
        artist_id: int | str,
        include_eps_singles: bool = True,
    ) -> list[Album]:
        """
        Fetch ALL albums by an artist, handling pagination.
        Optionally includes EPs and singles.
        """
        albums: list[Album] = []

        for filter_type in ["ALBUMS"] + (["EPSANDSINGLES"] if include_eps_singles else []):
            offset = 0
            while True:
                page = self.get_artist_albums(
                    artist_id,
                    limit=Limits.ARTIST_ALBUMS_MAX,
                    offset=offset,
                    filter_type=filter_type,
                )
                albums.extend(page.items)
                if not page.has_more:
                    break
                offset = page.next_offset

        log.debug("Fetched %d albums for artist %s", len(albums), artist_id)
        return albums

    def get_artist_videos(
        self,
        artist_id: int | str,
        limit: int = Limits.ARTIST_VIDEOS,
        offset: int = 0,
    ) -> ArtistVideos:
        """Fetch videos by an artist (paginated)."""
        return self.client.fetch(
            ArtistVideos,
            f"artists/{artist_id}/videos",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.ARTIST_VIDEOS_MAX),
                "offset": offset,
            },
            cache_ttl=3600,
        )

    # =====================================================================
    # Tracks
    # =====================================================================

    def get_track(self, track_id: int | str) -> Track:
        """Fetch track metadata by ID."""
        return self.client.fetch(
            Track,
            f"tracks/{track_id}",
            {"countryCode": self.country_code},
            cache_ttl=3600,
        )

    def get_track_lyrics(self, track_id: int | str) -> TrackLyrics:
        """Fetch lyrics for a track."""
        return self.client.fetch(
            TrackLyrics,
            f"tracks/{track_id}/lyrics",
            {"countryCode": self.country_code},
            cache_ttl=3600,
        )

    def get_track_stream(
        self,
        track_id: int | str,
        quality: str | AudioQuality = AudioQuality.LOSSLESS,
    ) -> TrackStream:
        """
        Fetch the stream/playback info for a track.

        This returns the raw stream metadata including the base64-encoded
        manifest. Use decode_stream_manifest() to get the actual URLs.

        Args:
            track_id: Tidal track ID.
            quality: Audio quality tier (LOW, HIGH, LOSSLESS, HI_RES, HI_RES_LOSSLESS).

        Returns:
            TrackStream with manifest and audio metadata.
        """
        quality_str = quality.value if isinstance(quality, AudioQuality) else quality
        return self.client.fetch(
            TrackStream,
            f"tracks/{track_id}/playbackinfopostpaywall",
            {
                "audioquality": quality_str,
                "playbackmode": "STREAM",
                "assetpresentation": "FULL",
            },
            skip_cache=True,  # Stream URLs are ephemeral — never cache
        )

    def get_track_stream_url(
        self,
        track_id: int | str,
        quality: str | AudioQuality = AudioQuality.LOSSLESS,
    ) -> Optional[str]:
        """
        Convenience method: get the primary stream URL for a track.

        Returns the first URL from the decoded manifest, or None if
        the stream is DRM-encrypted or no URL is available.
        """
        stream = self.get_track_stream(track_id, quality)
        manifest = self.decode_stream_manifest(stream)

        if manifest is None:
            log.warning("Failed to decode stream manifest for track %s", track_id)
            return None

        if manifest.is_encrypted:
            log.warning(
                "Track %s stream is DRM-encrypted (encryption=%s) — cannot proxy",
                track_id,
                manifest.encryption_type,
            )
            return None

        return manifest.primary_url

    # =====================================================================
    # Videos
    # =====================================================================

    def get_video(self, video_id: int | str) -> Video:
        """Fetch video metadata by ID."""
        return self.client.fetch(
            Video,
            f"videos/{video_id}",
            {"countryCode": self.country_code},
            cache_ttl=3600,
        )

    def get_video_stream(
        self,
        video_id: int | str,
        quality: str = "HIGH",
    ) -> VideoStream:
        """Fetch the stream/playback info for a video."""
        return self.client.fetch(
            VideoStream,
            f"videos/{video_id}/playbackinfopostpaywall",
            {
                "videoquality": quality,
                "playbackmode": "STREAM",
                "assetpresentation": "FULL",
            },
            skip_cache=True,
        )

    # =====================================================================
    # Playlists
    # =====================================================================

    def get_playlist(self, playlist_uuid: str) -> Playlist:
        """Fetch playlist metadata by UUID."""
        return self.client.fetch(
            Playlist,
            f"playlists/{playlist_uuid}",
            {"countryCode": self.country_code},
            skip_cache=True,  # Playlists change frequently
        )

    def get_playlist_items(
        self,
        playlist_uuid: str,
        limit: int = Limits.PLAYLIST_ITEMS,
        offset: int = 0,
    ) -> PlaylistItems:
        """Fetch tracks and videos in a playlist (paginated)."""
        return self.client.fetch(
            PlaylistItems,
            f"playlists/{playlist_uuid}/items",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.PLAYLIST_ITEMS_MAX),
                "offset": offset,
            },
            skip_cache=True,
        )

    def get_all_playlist_tracks(self, playlist_uuid: str) -> list[Track]:
        """
        Fetch ALL tracks in a playlist, handling pagination automatically.
        Videos are filtered out — only audio tracks are returned.
        """
        tracks: list[Track] = []
        offset = 0
        limit = Limits.PLAYLIST_ITEMS_MAX

        while True:
            page = self.get_playlist_items(playlist_uuid, limit=limit, offset=offset)
            for item in page.items:
                t = item.as_track()
                if t is not None:
                    tracks.append(t)
            if not page.has_more:
                break
            offset = page.next_offset

        log.debug(
            "Fetched %d tracks from playlist %s",
            len(tracks),
            playlist_uuid,
        )
        return tracks

    # =====================================================================
    # Mixes (Tidal's radio/recommendation engine — replaces AI Sonic Analysis)
    # =====================================================================

    def get_mix_items(
        self,
        mix_id: str,
        limit: int = Limits.MIX_ITEMS,
        offset: int = 0,
    ) -> MixItems:
        """
        Fetch items in a Tidal mix (paginated).

        Mixes are Tidal's pre-curated radio/recommendation playlists.
        They are identified by mix IDs found in track.mixes or artist.mixes.

        This is how we replace PlexAmp's AI-based Sonic Analysis and DJ
        features — by leveraging Tidal's own curation and recommendation
        engine through your existing subscription.
        """
        return self.client.fetch(
            MixItems,
            f"mixes/{mix_id}/items",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.MIX_ITEMS_MAX),
                "offset": offset,
            },
            cache_ttl=3600,
        )

    def get_all_mix_tracks(self, mix_id: str) -> list[Track]:
        """
        Fetch ALL tracks in a Tidal mix, handling pagination.
        This is the core method for generating DJ/radio playlists
        from Tidal's recommendation engine instead of AI.
        """
        tracks: list[Track] = []
        offset = 0
        limit = Limits.MIX_ITEMS_MAX

        while True:
            page = self.get_mix_items(mix_id, limit=limit, offset=offset)
            for item in page.items:
                t = item.as_track()
                if t is not None:
                    tracks.append(t)
            if not page.has_more:
                break
            offset = page.next_offset

        log.debug("Fetched %d tracks from mix %s", len(tracks), mix_id)
        return tracks

    # =====================================================================
    # Favorites / Collection
    # =====================================================================

    def get_favorite_ids(self) -> FavoriteIds:
        """
        Fetch IDs of all favorited items (tracks, albums, artists, playlists).
        This is a lightweight call — just IDs, no full metadata.
        """
        return self.client.fetch(
            FavoriteIds,
            f"users/{self.user_id}/favorites/ids",
            {"countryCode": self.country_code},
            skip_cache=True,
        )

    def get_favorite_tracks(
        self,
        limit: int = Limits.FAVORITES,
        offset: int = 0,
        order: str = "DATE",
        order_direction: str = "DESC",
    ) -> FavoriteTracks:
        """
        Fetch favorited tracks with full metadata (paginated).

        Args:
            order: Sort field — "DATE", "NAME", "ARTIST", "ALBUM".
            order_direction: "ASC" or "DESC".
        """
        return self.client.fetch(
            FavoriteTracks,
            f"users/{self.user_id}/favorites/tracks",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.FAVORITES_MAX),
                "offset": offset,
                "order": order,
                "orderDirection": order_direction,
            },
            skip_cache=True,
        )

    def get_all_favorite_tracks(self) -> list[Track]:
        """Fetch ALL favorite tracks, handling pagination."""
        tracks: list[Track] = []
        offset = 0
        limit = Limits.FAVORITES_MAX

        while True:
            page = self.get_favorite_tracks(limit=limit, offset=offset)
            for fav in page.items:
                tracks.append(fav.item)
            if not page.has_more:
                break
            offset = page.next_offset

        log.info("Fetched %d favorite tracks", len(tracks))
        return tracks

    def get_favorite_albums(
        self,
        limit: int = Limits.FAVORITES,
        offset: int = 0,
    ) -> FavoriteAlbums:
        """Fetch favorited albums with full metadata (paginated)."""
        return self.client.fetch(
            FavoriteAlbums,
            f"users/{self.user_id}/favorites/albums",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.FAVORITES_MAX),
                "offset": offset,
            },
            skip_cache=True,
        )

    def get_favorite_artists(
        self,
        limit: int = Limits.FAVORITES,
        offset: int = 0,
    ) -> FavoriteArtists:
        """Fetch favorited artists with full metadata (paginated)."""
        return self.client.fetch(
            FavoriteArtists,
            f"users/{self.user_id}/favorites/artists",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.FAVORITES_MAX),
                "offset": offset,
            },
            skip_cache=True,
        )

    def get_favorite_playlists(
        self,
        limit: int = Limits.FAVORITES,
        offset: int = 0,
    ) -> FavoritePlaylists:
        """Fetch favorited playlists (paginated)."""
        return self.client.fetch(
            FavoritePlaylists,
            f"users/{self.user_id}/favorites/playlists",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.FAVORITES_MAX),
                "offset": offset,
            },
            skip_cache=True,
        )

    # =====================================================================
    # User Playlists
    # =====================================================================

    def get_user_playlists(
        self,
        limit: int = Limits.USER_PLAYLISTS,
        offset: int = 0,
    ) -> PlaylistItems:
        """
        Fetch playlists created by the authenticated user (paginated).
        Note: This is different from favorite playlists — these are playlists
        the user owns/created.
        """
        # The Tidal API uses a different response format here, but it's
        # structurally similar to PlaylistItems. We fetch raw and adapt.
        data = self.client.fetch_raw(
            f"users/{self.user_id}/playlists",
            {
                "countryCode": self.country_code,
                "limit": min(limit, Limits.USER_PLAYLISTS_MAX),
                "offset": offset,
            },
            skip_cache=True,
        )
        # The response is a paginated list of Playlist objects
        # We wrap them in the generic PaginatedList structure
        from core.models import PaginatedList
        return PaginatedList[Playlist].model_validate(data)

    def get_all_user_playlists(self) -> list[Playlist]:
        """Fetch ALL playlists owned by the authenticated user."""
        playlists: list[Playlist] = []
        offset = 0
        limit = Limits.USER_PLAYLISTS_MAX

        while True:
            page = self.get_user_playlists(limit=limit, offset=offset)
            # Items here are Playlist objects directly (not wrapped in MixedItem)
            for item in page.items:
                if isinstance(item, Playlist):
                    playlists.append(item)
                elif isinstance(item, dict):
                    playlists.append(Playlist.model_validate(item))
                else:
                    # It might be a MixedItem wrapper — try to extract
                    try:
                        playlists.append(Playlist.model_validate(item.model_dump()))
                    except Exception:
                        log.debug("Skipping non-playlist item: %s", type(item))
            if not page.has_more:
                break
            offset = page.next_offset

        log.info("Fetched %d user playlists", len(playlists))
        return playlists

    # =====================================================================
    # Search
    # =====================================================================

    def search(
        self,
        query: str,
        limit: int = Limits.SEARCH,
        types: Optional[str] = None,
    ) -> SearchResults:
        """
        Search across Tidal for tracks, albums, artists, playlists.

        Args:
            query: Search query string.
            limit: Max results per type.
            types: Comma-separated list of types to search, e.g. "TRACKS,ALBUMS".
                   If None, searches all types.

        Returns:
            SearchResults with paginated lists per type.
        """
        params: dict = {
            "countryCode": self.country_code,
            "query": query,
            "limit": min(limit, Limits.SEARCH_MAX),
        }
        if types:
            params["types"] = types

        return self.client.fetch(
            SearchResults,
            "search",
            params,
            skip_cache=True,
        )

    def search_tracks(self, query: str, limit: int = Limits.SEARCH) -> list[Track]:
        """Search for tracks only."""
        results = self.search(query, limit=limit, types="TRACKS")
        return results.tracks.items

    def search_albums(self, query: str, limit: int = Limits.SEARCH) -> list[Album]:
        """Search for albums only."""
        results = self.search(query, limit=limit, types="ALBUMS")
        return results.albums.items

    def search_artists(self, query: str, limit: int = Limits.SEARCH) -> list[Artist]:
        """Search for artists only."""
        results = self.search(query, limit=limit, types="ARTISTS")
        return results.artists.items

    # =====================================================================
    # Session
    # =====================================================================

    def get_session(self) -> SessionResponse:
        """
        Fetch current session info — confirms auth is working.
        Returns user ID, country code, partner info.
        """
        return self.client.fetch(
            SessionResponse,
            "sessions",
            skip_cache=True,
        )

    # =====================================================================
    # Stream Manifest Helpers
    # =====================================================================

    @staticmethod
    def decode_stream_manifest(stream: TrackStream) -> Optional[StreamManifest]:
        """
        Decode a base64-encoded BTS stream manifest into a StreamManifest object.

        Tidal's playbackinfopostpaywall endpoint returns the manifest as a
        base64-encoded JSON string (for BTS type manifests). This method
        decodes it and parses into the StreamManifest model which contains
        the actual CDN stream URLs.

        Args:
            stream: TrackStream response from get_track_stream().

        Returns:
            StreamManifest with URLs, codec info, and encryption status.
            Returns None if decoding fails.
        """
        if not stream.is_bts:
            log.warning(
                "Stream manifest for track %s is not BTS type (mime=%s) — "
                "DASH manifests require separate handling",
                stream.track_id,
                stream.manifest_mime_type,
            )
            return None

        try:
            decoded_bytes = base64.b64decode(stream.manifest)
            manifest_data = json.loads(decoded_bytes)
            return StreamManifest.model_validate(manifest_data)
        except Exception as e:
            log.error(
                "Failed to decode stream manifest for track %s: %s",
                stream.track_id,
                e,
            )
            return None

    # =====================================================================
    # Normalized Export Helpers (for Plex sync)
    # =====================================================================

    def export_playlist_for_plex(self, playlist_uuid: str) -> NormalizedPlaylist:
        """
        Fetch a Tidal playlist and all its tracks, returning a NormalizedPlaylist
        ready for the Go plex-sync service to consume.

        This is the primary method the bridge REST API exposes for playlist sync.
        """
        playlist = self.get_playlist(playlist_uuid)
        tracks = self.get_all_playlist_tracks(playlist_uuid)

        normalized_tracks = [NormalizedTrack.from_tidal_track(t) for t in tracks]

        return NormalizedPlaylist.from_tidal_playlist(playlist, normalized_tracks)

    def export_favorites_for_plex(self, playlist_name: str = "Tidal Favorites") -> NormalizedPlaylist:
        """
        Fetch all favorite tracks and package them as a NormalizedPlaylist
        for syncing to Plex as a single playlist.
        """
        tracks = self.get_all_favorite_tracks()
        normalized_tracks = [NormalizedTrack.from_tidal_track(t) for t in tracks]

        return NormalizedPlaylist(
            tidal_uuid="__favorites__",
            title=playlist_name,
            description="All favorited tracks from your Tidal collection.",
            track_count=len(normalized_tracks),
            tracks=normalized_tracks,
        )

    def export_mix_for_plex(
        self,
        mix_id: str,
        playlist_name: Optional[str] = None,
    ) -> NormalizedPlaylist:
        """
        Fetch a Tidal mix and package it as a NormalizedPlaylist for Plex.

        This is how we replace PlexAmp's AI-based Sonic Analysis / DJ features:
        Tidal's own curated mixes and radio stations are used directly.
        """
        tracks = self.get_all_mix_tracks(mix_id)
        normalized_tracks = [NormalizedTrack.from_tidal_track(t) for t in tracks]

        title = playlist_name or f"Tidal Mix: {mix_id}"

        return NormalizedPlaylist(
            tidal_uuid=f"__mix__{mix_id}",
            title=title,
            description=f"Tidal mix ({mix_id}) — curated radio/recommendations.",
            track_count=len(normalized_tracks),
            tracks=normalized_tracks,
        )

    def export_all_playlists_for_plex(self) -> list[NormalizedPlaylist]:
        """
        Fetch ALL user-owned and favorited playlists and export them
        as NormalizedPlaylists for bulk Plex sync.
        """
        exported: list[NormalizedPlaylist] = []

        # User-created playlists
        try:
            user_playlists = self.get_all_user_playlists()
            for pl in user_playlists:
                try:
                    normalized = self.export_playlist_for_plex(pl.uuid)
                    exported.append(normalized)
                except Exception as e:
                    log.warning("Failed to export playlist '%s' (%s): %s", pl.title, pl.uuid, e)
        except Exception as e:
            log.warning("Failed to fetch user playlists: %s", e)

        # Favorited playlists (that the user didn't create)
        try:
            offset = 0
            while True:
                page = self.get_favorite_playlists(limit=Limits.FAVORITES_MAX, offset=offset)
                for fav in page.items:
                    pl = fav.item
                    # Skip if we already exported this one (user-created)
                    if any(e.tidal_uuid == pl.uuid for e in exported):
                        continue
                    try:
                        normalized = self.export_playlist_for_plex(pl.uuid)
                        exported.append(normalized)
                    except Exception as e:
                        log.warning("Failed to export favorited playlist '%s' (%s): %s", pl.title, pl.uuid, e)
                if not page.has_more:
                    break
                offset = page.next_offset
        except Exception as e:
            log.warning("Failed to fetch favorited playlists: %s", e)

        log.info("Exported %d total playlists for Plex sync", len(exported))
        return exported

    # =====================================================================
    # Artist Radio / Mix Discovery
    # =====================================================================

    def get_artist_mix_ids(self, artist_id: int | str) -> dict[str, str]:
        """
        Get available mix IDs for an artist.

        Returns a dict like {'ARTIST_MIX': 'abc123def', 'TRACK_MIX': 'xyz789'}
        that can be passed to get_mix_items() or export_mix_for_plex().

        This is the entry point for generating "artist radio" playlists
        without AI — using Tidal's native recommendation engine.
        """
        artist = self.get_artist(artist_id)
        return artist.mixes or {}

    def get_track_mix_ids(self, track_id: int | str) -> dict[str, str]:
        """
        Get available mix IDs for a track (e.g., TRACK_MIX for "track radio").

        Returns a dict of mix type → mix
