"""
Tidal API resource models.

Pydantic models for all Tidal API resources: tracks, albums, artists,
playlists, mixes, favorites, search results, and stream manifests.

These models normalize the Tidal API JSON responses into typed Python
objects that the bridge service can work with. Field aliases handle
the camelCase ↔ snake_case mapping from the Tidal API.

Inspired by tiddl (https://github.com/oskvr37/tiddl) models but
extended to cover collection sync, recommendations, and playlist
mirroring for the PlexAmp integration.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

# =============================================================================
# Enums
# =============================================================================


class AudioQuality(StrEnum):
    """Tidal audio quality tiers."""

    LOW = "LOW"
    HIGH = "HIGH"
    LOSSLESS = "LOSSLESS"
    HI_RES = "HI_RES"
    HI_RES_LOSSLESS = "HI_RES_LOSSLESS"


class AudioMode(StrEnum):
    """Audio playback modes."""

    STEREO = "STEREO"
    DOLBY_ATMOS = "DOLBY_ATMOS"
    SONY_360RA = "SONY_360RA"


class VideoQuality(StrEnum):
    """Tidal video quality tiers."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class MediaType(StrEnum):
    """Types of media items in Tidal."""

    TRACK = "track"
    VIDEO = "video"
    ALBUM = "album"
    ARTIST = "artist"
    PLAYLIST = "playlist"
    MIX = "mix"


class PlaylistType(StrEnum):
    """Tidal playlist ownership types."""

    USER = "USER"
    EDITORIAL = "EDITORIAL"
    ARTIST = "ARTIST"


class AlbumType(StrEnum):
    """Tidal album release types."""

    ALBUM = "ALBUM"
    EP = "EP"
    SINGLE = "SINGLE"
    COMPILATION = "COMPILATION"


class ArtistRole(StrEnum):
    """Roles an artist can have on a track or album."""

    MAIN = "MAIN"
    FEATURED = "FEATURED"
    CONTRIBUTOR = "CONTRIBUTOR"
    PRODUCER = "PRODUCER"
    COMPOSER = "COMPOSER"
    LYRICIST = "LYRICIST"
    ENGINEER = "ENGINEER"
    MIXER = "MIXER"
    REMIXER = "REMIXER"


class ManifestMimeType(StrEnum):
    """MIME types for stream manifests."""

    BTS = "application/vnd.tidal.bts"
    DASH = "application/dash+xml"


# =============================================================================
# Base / Shared Models
# =============================================================================


class TidalImage(BaseModel):
    """Image reference with dimensions. Used for cover art, artist pictures, etc."""

    url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

    @staticmethod
    def build_url(image_id: Optional[str], width: int = 640, height: int = 640) -> Optional[str]:
        """
        Build a Tidal image URL from an image UUID.
        Tidal stores image IDs as UUIDs with dashes; the URL uses slashes.
        """
        if not image_id:
            return None
        path = image_id.replace("-", "/")
        return f"https://resources.tidal.com/images/{path}/{width}x{height}.jpg"


T = TypeVar("T")


class PaginatedList(BaseModel, Generic[T]):
    """
    Generic paginated list response from Tidal API.
    Most list endpoints return items in this wrapper.
    """

    limit: int = Field(default=50, description="Requested page size.")
    offset: int = Field(default=0, description="Offset into the full result set.")
    total_number_of_items: int = Field(
        default=0,
        alias="totalNumberOfItems",
        description="Total items available across all pages.",
    )
    items: list[T] = Field(default_factory=list, description="Items in this page.")

    model_config = {"populate_by_name": True}

    @property
    def has_more(self) -> bool:
        """Whether there are more pages of results."""
        return (self.offset + self.limit) < self.total_number_of_items

    @property
    def next_offset(self) -> int:
        """Offset for the next page."""
        return self.offset + self.limit


# =============================================================================
# Core Resource Models
# =============================================================================


class ArtistSummary(BaseModel):
    """
    Minimal artist reference as embedded in tracks, albums, etc.
    For the full artist object, see Artist.
    """

    id: int
    name: str
    type: Optional[str] = None
    picture: Optional[str] = Field(default=None, description="Image UUID for artist picture.")

    model_config = {"populate_by_name": True}

    @property
    def picture_url(self) -> Optional[str]:
        return TidalImage.build_url(self.picture, 480, 480)


class ArtistCredit(BaseModel):
    """Artist credit with a specific role (e.g., MAIN, FEATURED)."""

    id: int
    name: str
    type: Optional[str] = None
    role: Optional[str] = None  # One of ArtistRole values


class Artist(BaseModel):
    """Full artist resource from GET /artists/{id}."""

    id: int
    name: str
    artist_types: Optional[list[str]] = Field(default=None, alias="artistTypes")
    url: Optional[str] = None
    picture: Optional[str] = Field(default=None, description="Image UUID.")
    popularity: Optional[int] = None
    artist_roles: Optional[list[dict[str, str]]] = Field(default=None, alias="artistRoles")
    mixes: Optional[dict[str, str]] = Field(
        default=None,
        description="Map of mix type to mix ID. E.g., {'ARTIST_MIX': 'abc123'}.",
    )

    model_config = {"populate_by_name": True}

    @property
    def picture_url(self) -> Optional[str]:
        return TidalImage.build_url(self.picture, 480, 480)


class Album(BaseModel):
    """Album resource from GET /albums/{id}."""

    id: int
    title: str
    duration: Optional[int] = Field(default=None, description="Total duration in seconds.")
    number_of_tracks: Optional[int] = Field(default=None, alias="numberOfTracks")
    number_of_volumes: Optional[int] = Field(default=None, alias="numberOfVolumes")
    number_of_videos: Optional[int] = Field(default=None, alias="numberOfVideos")
    release_date: Optional[str] = Field(default=None, alias="releaseDate")
    copyright: Optional[str] = None
    type: Optional[str] = Field(default=None, description="ALBUM, EP, SINGLE, COMPILATION.")
    version: Optional[str] = Field(default=None, description="Album version string, e.g. 'Deluxe'.")
    url: Optional[str] = None
    cover: Optional[str] = Field(default=None, description="Image UUID for album cover.")
    video_cover: Optional[str] = Field(default=None, alias="videoCover")
    explicit: bool = Field(default=False)
    upc: Optional[str] = None
    popularity: Optional[int] = None
    audio_quality: Optional[str] = Field(default=None, alias="audioQuality")
    audio_modes: Optional[list[str]] = Field(default=None, alias="audioModes")
    media_metadata: Optional[dict[str, Any]] = Field(default=None, alias="mediaMetadata")
    artist: Optional[ArtistSummary] = None
    artists: Optional[list[ArtistSummary]] = None

    model_config = {"populate_by_name": True}

    @property
    def cover_url(self) -> Optional[str]:
        return TidalImage.build_url(self.cover, 640, 640)

    @property
    def cover_url_large(self) -> Optional[str]:
        return TidalImage.build_url(self.cover, 1280, 1280)

    @property
    def primary_artist_name(self) -> str:
        if self.artist:
            return self.artist.name
        if self.artists:
            return self.artists[0].name
        return "Unknown Artist"


class Track(BaseModel):
    """Track resource from GET /tracks/{id}."""

    id: int
    title: str
    duration: int = Field(description="Track duration in seconds.")
    track_number: int = Field(default=1, alias="trackNumber")
    volume_number: int = Field(default=1, alias="volumeNumber")
    version: Optional[str] = Field(default=None, description="Track version, e.g. 'Remastered'.")
    url: Optional[str] = None
    isrc: Optional[str] = None
    explicit: bool = Field(default=False)
    audio_quality: Optional[str] = Field(default=None, alias="audioQuality")
    audio_modes: Optional[list[str]] = Field(default=None, alias="audioModes")
    media_metadata: Optional[dict[str, Any]] = Field(default=None, alias="mediaMetadata")
    copyright: Optional[str] = None
    popularity: Optional[int] = None
    replay_gain: Optional[float] = Field(default=None, alias="replayGain")
    peak: Optional[float] = None
    artist: Optional[ArtistSummary] = None
    artists: Optional[list[ArtistSummary]] = None
    album: Optional[Album] = None
    mixes: Optional[dict[str, str]] = Field(
        default=None,
        description="Map of mix type to mix ID. E.g., {'TRACK_MIX': 'abc123'}.",
    )
    editable: Optional[bool] = None
    allow_streaming: Optional[bool] = Field(default=None, alias="allowStreaming")

    model_config = {"populate_by_name": True}

    @property
    def full_title(self) -> str:
        """Title with version suffix if present."""
        if self.version:
            return f"{self.title} ({self.version})"
        return self.title

    @property
    def primary_artist_name(self) -> str:
        if self.artist:
            return self.artist.name
        if self.artists:
            return self.artists[0].name
        return "Unknown Artist"

    @property
    def all_artist_names(self) -> list[str]:
        if self.artists:
            return [a.name for a in self.artists]
        if self.artist:
            return [self.artist.name]
        return []

    @property
    def album_title(self) -> Optional[str]:
        return self.album.title if self.album else None

    @property
    def cover_url(self) -> Optional[str]:
        if self.album:
            return self.album.cover_url
        return None


class Video(BaseModel):
    """Video resource from GET /videos/{id}."""

    id: int
    title: str
    duration: int = Field(description="Video duration in seconds.")
    image_id: Optional[str] = Field(default=None, alias="imageId")
    image_path: Optional[str] = Field(default=None, alias="imagePath")
    url: Optional[str] = None
    explicit: bool = Field(default=False)
    quality: Optional[str] = None
    popularity: Optional[int] = None
    artist: Optional[ArtistSummary] = None
    artists: Optional[list[ArtistSummary]] = None
    album: Optional[Album] = None

    model_config = {"populate_by_name": True}


# =============================================================================
# Playlist Models
# =============================================================================


class PlaylistCreator(BaseModel):
    """Creator info embedded in playlist resources."""

    id: Optional[int] = None
    name: Optional[str] = None
    picture: Optional[str] = None
    type: Optional[str] = None


class Playlist(BaseModel):
    """Playlist resource from GET /playlists/{uuid}."""

    uuid: str
    title: str
    description: Optional[str] = None
    duration: Optional[int] = Field(default=None, description="Total duration in seconds.")
    number_of_tracks: int = Field(default=0, alias="numberOfTracks")
    number_of_videos: int = Field(default=0, alias="numberOfVideos")
    last_updated: Optional[str] = Field(default=None, alias="lastUpdated")
    created: Optional[str] = None
    type: Optional[str] = Field(default=None, description="USER, EDITORIAL, or ARTIST.")
    public_playlist: Optional[bool] = Field(default=None, alias="publicPlaylist")
    url: Optional[str] = None
    image: Optional[str] = Field(default=None, description="Image UUID for playlist cover.")
    square_image: Optional[str] = Field(default=None, alias="squareImage")
    popularity: Optional[int] = None
    promoted_artists: Optional[list[ArtistSummary]] = Field(default=None, alias="promotedArtists")
    creator: Optional[PlaylistCreator] = None
    last_item_added_at: Optional[str] = Field(default=None, alias="lastItemAddedAt")

    model_config = {"populate_by_name": True}

    @property
    def image_url(self) -> Optional[str]:
        img = self.square_image or self.image
        return TidalImage.build_url(img, 640, 640) if img else None

    @property
    def total_items(self) -> int:
        return self.number_of_tracks + self.number_of_videos


# =============================================================================
# Item Wrapper Models (used in paginated list endpoints)
# =============================================================================


class TrackItem(BaseModel):
    """
    Wrapper for tracks in list responses (e.g., album items, playlist items).
    Some endpoints wrap the actual Track in an 'item' field with a 'type' discriminator.
    """

    item: Track
    type: str = Field(default="track", description="Always 'track' for track items.")
    cut: Optional[Any] = None


class VideoItem(BaseModel):
    """Wrapper for videos in list responses."""

    item: Video
    type: str = Field(default="video", description="Always 'video' for video items.")
    cut: Optional[Any] = None


class MixedItem(BaseModel):
    """
    Generic wrapper that can hold either a track or a video.
    Used in playlist/album items endpoints where both types can appear.
    """

    item: dict[str, Any] = Field(description="Raw item data — parse based on 'type' field.")
    type: str = Field(description="'track' or 'video'.")
    cut: Optional[Any] = None

    def as_track(self) -> Optional[Track]:
        """Parse the inner item as a Track if type is 'track'."""
        if self.type == "track":
            return Track.model_validate(self.item)
        return None

    def as_video(self) -> Optional[Video]:
        """Parse the inner item as a Video if type is 'video'."""
        if self.type == "video":
            return Video.model_validate(self.item)
        return None


# =============================================================================
# Paginated Response Models (typed wrappers around PaginatedList)
# =============================================================================


class AlbumItems(PaginatedList[MixedItem]):
    """Paginated list of items (tracks + videos) in an album."""

    pass


class PlaylistItems(PaginatedList[MixedItem]):
    """Paginated list of items (tracks + videos) in a playlist."""

    pass


class MixItems(PaginatedList[MixedItem]):
    """Paginated list of items in a Tidal mix."""

    pass


class ArtistAlbums(PaginatedList[Album]):
    """Paginated list of albums by an artist."""

    pass


class ArtistVideos(PaginatedList[Video]):
    """Paginated list of videos by an artist."""

    pass


# =============================================================================
# Favorites / Collection Models
# =============================================================================


class FavoriteIds(BaseModel):
    """
    Response from GET /users/{userId}/favorites/ids.
    Contains lists of IDs for all favorited items.
    """

    TRACK: list[int] = Field(default_factory=list)
    VIDEO: list[int] = Field(default_factory=list)
    ALBUM: list[int] = Field(default_factory=list)
    ARTIST: list[int] = Field(default_factory=list)
    PLAYLIST: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def total_items(self) -> int:
        return len(self.TRACK) + len(self.VIDEO) + len(self.ALBUM) + len(self.ARTIST) + len(self.PLAYLIST)


class FavoriteTrack(BaseModel):
    """A favorited track with the date it was added."""

    created: str = Field(description="ISO 8601 timestamp when favorited.")
    item: Track


class FavoriteAlbum(BaseModel):
    """A favorited album with the date it was added."""

    created: str
    item: Album


class FavoriteArtist(BaseModel):
    """A favorited artist with the date it was added."""

    created: str
    item: Artist


class FavoritePlaylist(BaseModel):
    """A favorited playlist with the date it was added."""

    created: str
    item: Playlist  # Note: may only be a summary; uuid is the key field


class FavoriteTracks(PaginatedList[FavoriteTrack]):
    """Paginated list of favorite tracks."""

    pass


class FavoriteAlbums(PaginatedList[FavoriteAlbum]):
    """Paginated list of favorite albums."""

    pass


class FavoriteArtists(PaginatedList[FavoriteArtist]):
    """Paginated list of favorite artists."""

    pass


class FavoritePlaylists(PaginatedList[FavoritePlaylist]):
    """Paginated list of favorite playlists."""

    pass


# =============================================================================
# Search Models
# =============================================================================


class SearchResults(BaseModel):
    """Response from GET /search with combined results across types."""

    artists: PaginatedList[Artist] = Field(default_factory=lambda: PaginatedList[Artist](items=[]))
    albums: PaginatedList[Album] = Field(default_factory=lambda: PaginatedList[Album](items=[]))
    tracks: PaginatedList[Track] = Field(default_factory=lambda: PaginatedList[Track](items=[]))
    videos: PaginatedList[Video] = Field(default_factory=lambda: PaginatedList[Video](items=[]))
    playlists: PaginatedList[Playlist] = Field(default_factory=lambda: PaginatedList[Playlist](items=[]))
    top_hit: Optional[dict[str, Any]] = Field(default=None, alias="topHit")

    model_config = {"populate_by_name": True}


# =============================================================================
# Stream / Playback Models
# =============================================================================


class TrackStream(BaseModel):
    """
    Response from GET /tracks/{id}/playbackinfopostpaywall.
    Contains the stream manifest for a track.
    """

    track_id: int = Field(alias="trackId")
    asset_presentation: str = Field(alias="assetPresentation")
    audio_mode: str = Field(alias="audioMode")
    audio_quality: str = Field(alias="audioQuality")
    streaming_session_id: Optional[str] = Field(default=None, alias="streamingSessionId")
    manifest_mime_type: str = Field(alias="manifestMimeType")
    manifest_hash: Optional[str] = Field(default=None, alias="manifestHash")
    manifest: str = Field(description="Base64-encoded manifest JSON (for BTS type) or raw DASH XML.")
    album_replay_gain: Optional[float] = Field(default=None, alias="albumReplayGain")
    album_peak_amplitude: Optional[float] = Field(default=None, alias="albumPeakAmplitude")
    track_replay_gain: Optional[float] = Field(default=None, alias="trackReplayGain")
    track_peak_amplitude: Optional[float] = Field(default=None, alias="trackPeakAmplitude")
    bit_depth: Optional[int] = Field(default=None, alias="bitDepth")
    sample_rate: Optional[int] = Field(default=None, alias="sampleRate")

    model_config = {"populate_by_name": True}

    @property
    def is_bts(self) -> bool:
        """Check if this is a BTS (base64-encoded JSON) manifest."""
        return "bts" in self.manifest_mime_type.lower()


class StreamManifest(BaseModel):
    """
    Decoded BTS manifest from TrackStream.manifest (after base64 decode).
    Contains the actual stream URLs and codec info.
    """

    mime_type: str = Field(alias="mimeType")
    codecs: str
    encryption_type: Optional[str] = Field(default=None, alias="encryptionType")
    urls: list[str] = Field(default_factory=list)
    key_id: Optional[str] = Field(default=None, alias="keyId")

    model_config = {"populate_by_name": True}

    @property
    def primary_url(self) -> Optional[str]:
        """Get the first (usually only) stream URL."""
        return self.urls[0] if self.urls else None

    @property
    def is_encrypted(self) -> bool:
        """Check if the stream is DRM-encrypted."""
        return self.encryption_type is not None and self.encryption_type != "NONE"


class VideoStream(BaseModel):
    """Response from GET /videos/{id}/playbackinfopostpaywall."""

    video_id: int = Field(alias="videoId")
    asset_presentation: str = Field(alias="assetPresentation")
    video_quality: str = Field(alias="videoQuality")
    streaming_session_id: Optional[str] = Field(default=None, alias="streamingSessionId")
    manifest_mime_type: str = Field(alias="manifestMimeType")
    manifest_hash: Optional[str] = Field(default=None, alias="manifestHash")
    manifest: str

    model_config = {"populate_by_name": True}


# =============================================================================
# Lyrics
# =============================================================================


class TrackLyrics(BaseModel):
    """Lyrics response from GET /tracks/{id}/lyrics."""

    track_id: int = Field(alias="trackId")
    lyrics_provider: Optional[str] = Field(default=None, alias="lyricsProvider")
    provider_commontrack_id: Optional[str] = Field(default=None, alias="providerCommontrackId")
    provider_lyrics_id: Optional[str] = Field(default=None, alias="providerLyricsId")
    lyrics: Optional[str] = None
    subtitles: Optional[str] = Field(
        default=None,
        description="Time-synced lyrics in LRC or similar format.",
    )
    is_right_to_left: bool = Field(default=False, alias="isRightToLeft")

    model_config = {"populate_by_name": True}


# =============================================================================
# Album Review
# =============================================================================


class AlbumReview(BaseModel):
    """Review/editorial text for an album."""

    source: Optional[str] = None
    text: Optional[str] = None
    summary: Optional[str] = None
    last_updated: Optional[str] = Field(default=None, alias="lastUpdated")

    model_config = {"populate_by_name": True}


# =============================================================================
# Credits
# =============================================================================


class CreditEntry(BaseModel):
    """A single contributor credit on a track."""

    type: str = Field(description="Credit type, e.g. 'Producer', 'Composer'.")
    contributors: list[ArtistSummary] = Field(default_factory=list)


class TrackCredits(BaseModel):
    """Credits for a single track within an album credits response."""

    track_id: int = Field(alias="trackId")
    credits: list[CreditEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class AlbumItemsCredits(PaginatedList[TrackCredits]):
    """Paginated list of per-track credits for an album."""

    pass


# =============================================================================
# Session
# =============================================================================


class SessionResponse(BaseModel):
    """Response from GET /sessions — confirms auth is working."""

    session_id: str = Field(alias="sessionId")
    user_id: int = Field(alias="userId")
    country_code: str = Field(alias="countryCode")
    channel_id: Optional[int] = Field(default=None, alias="channelId")
    partner_id: Optional[int] = Field(default=None, alias="partnerId")
    client: Optional[dict[str, Any]] = None

    model_config = {"populate_by_name": True}


# =============================================================================
# Bridge-Specific Normalized Models
# =============================================================================


class NormalizedTrack(BaseModel):
    """
    Simplified track model optimized for PlexAmp bridge sync.
    Strips out Tidal-specific fields and normalizes the structure
    for easy mapping to Plex library metadata.
    """

    tidal_id: int
    title: str
    version: Optional[str] = None
    duration_seconds: int
    track_number: int = 1
    disc_number: int = 1
    isrc: Optional[str] = None
    explicit: bool = False
    artist_name: str = "Unknown Artist"
    artist_names: list[str] = Field(default_factory=list)
    album_title: Optional[str] = None
    album_id: Optional[int] = None
    cover_url: Optional[str] = None
    audio_quality: Optional[str] = None
    popularity: Optional[int] = None
    replay_gain: Optional[float] = None

    @classmethod
    def from_tidal_track(cls, track: Track) -> NormalizedTrack:
        """Convert a Tidal Track to a NormalizedTrack for Plex sync."""
        return cls(
            tidal_id=track.id,
            title=track.title,
            version=track.version,
            duration_seconds=track.duration,
            track_number=track.track_number,
            disc_number=track.volume_number,
            isrc=track.isrc,
            explicit=track.explicit,
            artist_name=track.primary_artist_name,
            artist_names=track.all_artist_names,
            album_title=track.album_title,
            album_id=track.album.id if track.album else None,
            cover_url=track.cover_url,
            audio_quality=track.audio_quality,
            popularity=track.popularity,
            replay_gain=track.replay_gain,
        )


class NormalizedPlaylist(BaseModel):
    """
    Simplified playlist model for PlexAmp bridge sync.
    Contains just the info needed to create/update a Plex playlist.
    """

    tidal_uuid: str
    title: str
    description: Optional[str] = None
    track_count: int = 0
    duration_seconds: Optional[int] = None
    cover_url: Optional[str] = None
    playlist_type: Optional[str] = None
    last_updated: Optional[str] = None
    created: Optional[str] = None
    tracks: list[NormalizedTrack] = Field(default_factory=list)

    @classmethod
    def from_tidal_playlist(
        cls, playlist: Playlist, tracks: Optional[list[NormalizedTrack]] = None
    ) -> NormalizedPlaylist:
        """Convert a Tidal Playlist to a NormalizedPlaylist for Plex sync."""
        return cls(
            tidal_uuid=playlist.uuid,
            title=playlist.title,
            description=playlist.description,
            track_count=playlist.number_of_tracks,
            duration_seconds=playlist.duration,
            cover_url=playlist.image_url,
            playlist_type=playlist.type,
            last_updated=playlist.last_updated,
            created=playlist.created,
            tracks=tracks or [],
        )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Enums
    "AudioQuality",
    "AudioMode",
    "VideoQuality",
    "MediaType",
    "PlaylistType",
    "AlbumType",
    "ArtistRole",
    "ManifestMimeType",
    # Base
    "TidalImage",
    "PaginatedList",
    # Core resources
    "ArtistSummary",
    "ArtistCredit",
    "Artist",
    "Album",
    "Track",
    "Video",
    # Playlist
    "PlaylistCreator",
    "Playlist",
    # Item wrappers
    "TrackItem",
    "VideoItem",
    "MixedItem",
    # Paginated responses
    "AlbumItems",
    "PlaylistItems",
    "MixItems",
    "ArtistAlbums",
    "ArtistVideos",
    # Favorites
    "FavoriteIds",
    "FavoriteTrack",
    "FavoriteAlbum",
    "FavoriteArtist",
    "FavoritePlaylist",
    "FavoriteTracks",
    "FavoriteAlbums",
    "FavoriteArtists",
    "FavoritePlaylists",
    # Search
    "SearchResults",
    # Streams
    "TrackStream",
    "StreamManifest",
    "VideoStream",
    # Lyrics / Review / Credits
    "TrackLyrics",
    "AlbumReview",
    "CreditEntry",
    "TrackCredits",
    "AlbumItemsCredits",
    # Session
    "SessionResponse",
    # Normalized bridge models
    "NormalizedTrack",
    "NormalizedPlaylist",
]
