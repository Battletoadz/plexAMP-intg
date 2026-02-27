// Package sync implements the core synchronization engine that bridges
// Tidal metadata from the Python tidal-bridge REST API into Plex Media Server
// playlists via the plexgo SDK.
//
// The engine handles:
//   - Fetching normalized playlists/favorites/mixes from the tidal-bridge
//   - Matching Tidal tracks to existing Plex library tracks by ISRC, title+artist, or fuzzy match
//   - Creating and updating Plex playlists to mirror Tidal collections
//   - Tracking sync state to avoid redundant updates
//   - Optionally cleaning up orphaned Plex playlists that no longer exist on Tidal
//
// This is the core piece that replaces AI-based Sonic Analysis and DJ features
// with Tidal's own curated mixes and recommendations — no GenAI involved.
package sync

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	plexgo "github.com/LukeHagar/plexgo"
	"github.com/LukeHagar/plexgo/models/operations"

	"github.com/plexamp-universal-intg/plex-sync/internal/tidalclient"
)

// --------------------------------------------------------------------------
// Configuration
// --------------------------------------------------------------------------

// EngineConfig holds all configuration needed by the sync engine.
type EngineConfig struct {
	// Plex connection details
	PlexURL          string
	PlexToken        string
	PlexMusicLibrary string
	PlexClientID     string
	PlexProductName  string

	// What to sync
	SyncFavorites         bool
	FavoritesPlaylistName string
	SyncPlaylists         bool
	PlaylistPrefix        string
	SyncMixes             bool
	MixPrefix             string
	SyncRecommendations   bool
	RecommendationsPrefix string

	// Sync behavior
	DeleteOrphaned       bool
	MaxTracksPerPlaylist int

	// Persistent state paths
	SyncStatePath    string
	MappingCachePath string
}

// --------------------------------------------------------------------------
// Sync State (persisted between runs)
// --------------------------------------------------------------------------

// SyncState tracks what has been synced so we can detect changes and avoid
// redundant Plex API calls.
type SyncState struct {
	// LastSyncTime is the Unix timestamp of the last successful sync.
	LastSyncTime float64 `json:"last_sync_time"`

	// SyncedPlaylists maps Tidal playlist UUID → Plex playlist info.
	SyncedPlaylists map[string]SyncedPlaylistInfo `json:"synced_playlists"`

	// TotalSyncRuns counts the number of sync passes since state was created.
	TotalSyncRuns int64 `json:"total_sync_runs"`

	// LastError records the last sync error message, if any.
	LastError string `json:"last_error,omitempty"`
}

// SyncedPlaylistInfo records the state of a single synced playlist.
type SyncedPlaylistInfo struct {
	// TidalUUID is the Tidal playlist UUID (or synthetic ID for favorites/mixes).
	TidalUUID string `json:"tidal_uuid"`

	// PlexPlaylistID is the Plex rating key for the created playlist.
	PlexPlaylistID string `json:"plex_playlist_id,omitempty"`

	// PlexPlaylistTitle is the title of the Plex playlist.
	PlexPlaylistTitle string `json:"plex_playlist_title"`

	// TrackCount is the number of tracks in the Plex playlist.
	TrackCount int `json:"track_count"`

	// LastSyncedAt is the Unix timestamp of the last sync for this playlist.
	LastSyncedAt float64 `json:"last_synced_at"`

	// TidalLastUpdated is the Tidal-side last-updated timestamp (if available).
	TidalLastUpdated string `json:"tidal_last_updated,omitempty"`

	// MatchedTracks is the number of Tidal tracks that were matched in Plex.
	MatchedTracks int `json:"matched_tracks"`

	// UnmatchedTracks is the number of Tidal tracks not found in Plex.
	UnmatchedTracks int `json:"unmatched_tracks"`
}

// --------------------------------------------------------------------------
// Track Mapping Cache (Tidal ID → Plex rating key)
// --------------------------------------------------------------------------

// MappingCache caches the Tidal track ID → Plex library rating key mapping
// so we don't have to search Plex for every track on every sync run.
type MappingCache struct {
	mu       sync.RWMutex
	mappings map[int]string // tidal_id → plex_rating_key
	dirty    bool
	path     string
}

// NewMappingCache creates or loads an existing mapping cache.
func NewMappingCache(path string) *MappingCache {
	mc := &MappingCache{
		mappings: make(map[int]string),
		path:     path,
	}
	mc.load()
	return mc
}

// Get returns the Plex rating key for a Tidal track ID, if cached.
func (mc *MappingCache) Get(tidalID int) (string, bool) {
	mc.mu.RLock()
	defer mc.mu.RUnlock()
	key, ok := mc.mappings[tidalID]
	return key, ok
}

// Set stores a Tidal → Plex mapping.
func (mc *MappingCache) Set(tidalID int, plexRatingKey string) {
	mc.mu.Lock()
	defer mc.mu.Unlock()
	mc.mappings[tidalID] = plexRatingKey
	mc.dirty = true
}

// Size returns the number of cached mappings.
func (mc *MappingCache) Size() int {
	mc.mu.RLock()
	defer mc.mu.RUnlock()
	return len(mc.mappings)
}

// Save persists the cache to disk if it has been modified.
func (mc *MappingCache) Save() error {
	mc.mu.Lock()
	defer mc.mu.Unlock()

	if !mc.dirty {
		return nil
	}

	dir := filepath.Dir(mc.path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("failed to create cache directory: %w", err)
	}

	data, err := json.MarshalIndent(mc.mappings, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal mapping cache: %w", err)
	}

	if err := os.WriteFile(mc.path, data, 0o644); err != nil {
		return fmt.Errorf("failed to write mapping cache: %w", err)
	}

	mc.dirty = false
	return nil
}

// load reads the mapping cache from disk.
func (mc *MappingCache) load() {
	data, err := os.ReadFile(mc.path)
	if err != nil {
		return // File doesn't exist yet — start with empty cache
	}
	_ = json.Unmarshal(data, &mc.mappings)
}

// --------------------------------------------------------------------------
// Sync Engine
// --------------------------------------------------------------------------

// Engine is the main sync orchestrator. It fetches data from the tidal-bridge
// (Python REST API) and pushes it into Plex via the plexgo SDK.
type Engine struct {
	cfg    EngineConfig
	bridge *tidalclient.Client
	plex   *plexgo.PlexAPI
	logger *slog.Logger

	state        *SyncState
	mappingCache *MappingCache
}

// NewEngine creates a new sync engine with the given configuration and clients.
func NewEngine(cfg EngineConfig, bridge *tidalclient.Client, logger *slog.Logger) (*Engine, error) {
	if logger == nil {
		logger = slog.Default()
	}

	// Initialize the plexgo SDK client
	plexClient := plexgo.New(
		plexgo.WithSecurity(cfg.PlexToken),
		plexgo.WithServerURL(cfg.PlexURL),
		plexgo.WithClientIdentifier(cfg.PlexClientID),
		plexgo.WithProduct(cfg.PlexProductName),
		plexgo.WithVersion("0.1.0"),
		plexgo.WithPlatform("PlexAmp-Tidal-Bridge"),
		plexgo.WithDeviceName("plex-sync"),
	)

	// Load or create sync state
	state := loadSyncState(cfg.SyncStatePath)

	// Load or create mapping cache
	mappingCache := NewMappingCache(cfg.MappingCachePath)

	logger.Info("Sync engine initialized",
		"plex_url", cfg.PlexURL,
		"music_library", cfg.PlexMusicLibrary,
		"sync_favorites", cfg.SyncFavorites,
		"sync_playlists", cfg.SyncPlaylists,
		"sync_mixes", cfg.SyncMixes,
		"cached_mappings", mappingCache.Size(),
	)

	return &Engine{
		cfg:          cfg,
		bridge:       bridge,
		plex:         plexClient,
		logger:       logger.With("component", "sync-engine"),
		state:        state,
		mappingCache: mappingCache,
	}, nil
}

// RunOnce executes a single sync pass: fetch from Tidal bridge → push to Plex.
//
// This is the main entry point called by the periodic loop in cmd/main.go.
// It returns an error if the overall sync fails, but individual playlist
// sync failures are logged and do not abort the entire pass.
func (e *Engine) RunOnce(ctx context.Context) error {
	startTime := time.Now()
	e.logger.Info("=== Starting sync pass ===")

	var syncErrors []string

	// -----------------------------------------------------------------------
	// Step 1: Verify bridge is authenticated
	// -----------------------------------------------------------------------
	health, err := e.bridge.Health(ctx)
	if err != nil {
		return fmt.Errorf("tidal-bridge health check failed: %w", err)
	}
	if !health.Authenticated {
		return fmt.Errorf("tidal-bridge is not authenticated with Tidal — use POST /auth/login on the bridge (port 9120) to authenticate")
	}

	e.logger.Info("Bridge is authenticated",
		"user_id", health.UserID,
		"country", health.CountryCode,
	)

	// -----------------------------------------------------------------------
	// Step 2: Get the Plex music library section ID
	// -----------------------------------------------------------------------
	librarySectionID, err := e.findMusicLibrarySection(ctx)
	if err != nil {
		return fmt.Errorf("failed to find Plex music library section %q: %w", e.cfg.PlexMusicLibrary, err)
	}

	e.logger.Info("Found Plex music library",
		"library", e.cfg.PlexMusicLibrary,
		"section_id", librarySectionID,
	)

	// -----------------------------------------------------------------------
	// Step 3: Sync favorites playlist
	// -----------------------------------------------------------------------
	if e.cfg.SyncFavorites {
		e.logger.Info("Syncing Tidal favorites...")
		if err := e.syncFavorites(ctx, librarySectionID); err != nil {
			e.logger.Error("Failed to sync favorites", "error", err)
			syncErrors = append(syncErrors, fmt.Sprintf("favorites: %v", err))
		}
	}

	// -----------------------------------------------------------------------
	// Step 4: Sync user playlists
	// -----------------------------------------------------------------------
	if e.cfg.SyncPlaylists {
		e.logger.Info("Syncing Tidal playlists...")
		if err := e.syncPlaylists(ctx, librarySectionID); err != nil {
			e.logger.Error("Failed to sync playlists", "error", err)
			syncErrors = append(syncErrors, fmt.Sprintf("playlists: %v", err))
		}
	}

	// -----------------------------------------------------------------------
	// Step 5: Sync mixes (Tidal's radio/DJ replacement — NO AI)
	// -----------------------------------------------------------------------
	if e.cfg.SyncMixes {
		e.logger.Info("Syncing Tidal mixes (DJ/radio replacement)...")
		if err := e.syncMixes(ctx, librarySectionID); err != nil {
			e.logger.Error("Failed to sync mixes", "error", err)
			syncErrors = append(syncErrors, fmt.Sprintf("mixes: %v", err))
		}
	}

	// -----------------------------------------------------------------------
	// Step 6: Clean up orphaned playlists (if enabled)
	// -----------------------------------------------------------------------
	if e.cfg.DeleteOrphaned {
		e.logger.Info("Checking for orphaned Plex playlists to clean up...")
		if err := e.cleanupOrphanedPlaylists(ctx); err != nil {
			e.logger.Warn("Orphan cleanup encountered errors", "error", err)
		}
	}

	// -----------------------------------------------------------------------
	// Step 7: Persist state and cache
	// -----------------------------------------------------------------------
	e.state.LastSyncTime = float64(time.Now().Unix())
	e.state.TotalSyncRuns++

	if len(syncErrors) > 0 {
		e.state.LastError = strings.Join(syncErrors, "; ")
	} else {
		e.state.LastError = ""
	}

	if err := e.saveSyncState(); err != nil {
		e.logger.Warn("Failed to persist sync state", "error", err)
	}

	if err := e.mappingCache.Save(); err != nil {
		e.logger.Warn("Failed to persist mapping cache", "error", err)
	}

	duration := time.Since(startTime).Round(time.Millisecond)
	if len(syncErrors) > 0 {
		e.logger.Warn("=== Sync pass completed with errors ===",
			"duration", duration,
			"errors", len(syncErrors),
			"total_runs", e.state.TotalSyncRuns,
		)
		return fmt.Errorf("%d sync errors: %s", len(syncErrors), strings.Join(syncErrors, "; "))
	}

	e.logger.Info("=== Sync pass completed successfully ===",
		"duration", duration,
		"total_runs", e.state.TotalSyncRuns,
	)
	return nil
}

// --------------------------------------------------------------------------
// Sync: Favorites
// --------------------------------------------------------------------------

func (e *Engine) syncFavorites(ctx context.Context, librarySectionID string) error {
	favorites, err := e.bridge.ExportFavorites(ctx, e.cfg.FavoritesPlaylistName)
	if err != nil {
		return fmt.Errorf("failed to fetch favorites from bridge: %w", err)
	}

	e.logger.Info("Fetched favorites from Tidal",
		"track_count", favorites.TrackCount,
		"title", favorites.Title,
	)

	return e.syncNormalizedPlaylist(ctx, favorites, "", librarySectionID)
}

// --------------------------------------------------------------------------
// Sync: Playlists
// --------------------------------------------------------------------------

func (e *Engine) syncPlaylists(ctx context.Context, librarySectionID string) error {
	playlists, err := e.bridge.ExportAllPlaylists(ctx)
	if err != nil {
		return fmt.Errorf("failed to fetch playlists from bridge: %w", err)
	}

	e.logger.Info("Fetched playlists from Tidal", "count", len(playlists))

	var errs []string
	for i := range playlists {
		pl := &playlists[i]
		if err := e.syncNormalizedPlaylist(ctx, pl, e.cfg.PlaylistPrefix, librarySectionID); err != nil {
			e.logger.Error("Failed to sync playlist",
				"title", pl.Title,
				"tidal_uuid", pl.TidalUUID,
				"error", err,
			)
			errs = append(errs, fmt.Sprintf("%s: %v", pl.Title, err))
		}
	}

	if len(errs) > 0 {
		return fmt.Errorf("%d playlists failed: %s", len(errs), strings.Join(errs, "; "))
	}
	return nil
}

// --------------------------------------------------------------------------
// Sync: Mixes (Tidal's DJ/Radio — replaces AI Sonic Analysis)
// --------------------------------------------------------------------------

func (e *Engine) syncMixes(ctx context.Context, librarySectionID string) error {
	// To sync mixes, we first need to know which mixes are available.
	// We get these from favorite artists' mix IDs through the bridge.
	//
	// Strategy: fetch favorite artist IDs → for each, get their mixes →
	// export each mix as a playlist.
	//
	// This can be expensive for large collections, so we limit to the
	// first batch of favorite artists and their ARTIST_MIX type.

	favIDs, err := e.bridge.GetFavoriteIDs(ctx)
	if err != nil {
		return fmt.Errorf("failed to fetch favorite IDs: %w", err)
	}

	if len(favIDs.Artist) == 0 {
		e.logger.Info("No favorite artists found — skipping mix sync")
		return nil
	}

	// Limit to first 20 favorite artists to avoid overwhelming the API
	maxArtists := 20
	if len(favIDs.Artist) < maxArtists {
		maxArtists = len(favIDs.Artist)
	}

	e.logger.Info("Fetching mixes for favorite artists",
		"total_favorite_artists", len(favIDs.Artist),
		"processing", maxArtists,
	)

	var errs []string
	mixesSynced := 0

	for _, artistID := range favIDs.Artist[:maxArtists] {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		mixes, err := e.bridge.GetArtistMixes(ctx, artistID)
		if err != nil {
			e.logger.Debug("Failed to fetch mixes for artist",
				"artist_id", artistID,
				"error", err,
			)
			continue
		}

		// Look for ARTIST_MIX — this is the "artist radio" equivalent
		mixID, ok := mixes.Mixes["ARTIST_MIX"]
		if !ok {
			continue
		}

		playlistName := fmt.Sprintf("%s Artist Radio: %d", e.cfg.MixPrefix, artistID)

		mixPlaylist, err := e.bridge.ExportMix(ctx, mixID, playlistName)
		if err != nil {
			e.logger.Debug("Failed to export mix",
				"mix_id", mixID,
				"artist_id", artistID,
				"error", err,
			)
			errs = append(errs, fmt.Sprintf("mix %s: %v", mixID, err))
			continue
		}

		if err := e.syncNormalizedPlaylist(ctx, mixPlaylist, "", librarySectionID); err != nil {
			errs = append(errs, fmt.Sprintf("sync mix %s: %v", mixID, err))
			continue
		}

		mixesSynced++
	}

	e.logger.Info("Mix sync completed",
		"mixes_synced", mixesSynced,
		"errors", len(errs),
	)

	if len(errs) > 0 {
		return fmt.Errorf("%d mix sync errors: %s", len(errs), strings.Join(errs, "; "))
	}
	return nil
}

// --------------------------------------------------------------------------
// Core: Sync a NormalizedPlaylist into Plex
// --------------------------------------------------------------------------

// syncNormalizedPlaylist takes a NormalizedPlaylist from the tidal-bridge
// and creates or updates the corresponding Plex playlist.
//
// The flow:
//  1. For each track in the playlist, try to find it in the Plex library
//     (by ISRC match, then by title+artist fuzzy match)
//  2. Build a list of Plex rating keys for matched tracks
//  3. If a Plex playlist with the same name already exists, update it
//  4. Otherwise, create a new playlist
//  5. Record the sync state for this playlist
func (e *Engine) syncNormalizedPlaylist(
	ctx context.Context,
	playlist *tidalclient.NormalizedPlaylist,
	namePrefix string,
	librarySectionID string,
) error {
	if playlist == nil || len(playlist.Tracks) == 0 {
		e.logger.Debug("Skipping empty playlist",
			"title", playlist.Title,
			"tidal_uuid", playlist.TidalUUID,
		)
		return nil
	}

	// Build the Plex playlist title
	plexTitle := playlist.Title
	if namePrefix != "" {
		plexTitle = fmt.Sprintf("%s %s", strings.TrimSpace(namePrefix), playlist.Title)
	}

	// Apply track limit if configured
	tracks := playlist.Tracks
	if e.cfg.MaxTracksPerPlaylist > 0 && len(tracks) > e.cfg.MaxTracksPerPlaylist {
		tracks = tracks[:e.cfg.MaxTracksPerPlaylist]
	}

	e.logger.Info("Syncing playlist to Plex",
		"plex_title", plexTitle,
		"tidal_uuid", playlist.TidalUUID,
		"tidal_tracks", len(tracks),
	)

	// -----------------------------------------------------------------------
	// Match Tidal tracks to Plex library items
	// -----------------------------------------------------------------------
	var plexRatingKeys []string
	matched := 0
	unmatched := 0

	for _, track := range tracks {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		ratingKey, err := e.findPlexTrack(ctx, track, librarySectionID)
		if err != nil {
			e.logger.Debug("Track not found in Plex",
				"title", track.Title,
				"artist", track.ArtistName,
				"tidal_id", track.TidalID,
				"error", err,
			)
			unmatched++
			continue
		}

		plexRatingKeys = append(plexRatingKeys, ratingKey)
		matched++
	}

	e.logger.Info("Track matching complete",
		"playlist", plexTitle,
		"matched", matched,
		"unmatched", unmatched,
		"total", len(tracks),
	)

	if len(plexRatingKeys) == 0 {
		e.logger.Warn("No tracks matched in Plex library — skipping playlist creation",
			"playlist", plexTitle,
		)
		// Still record the state so we don't retry every sync pass
		e.recordSyncedPlaylist(playlist.TidalUUID, plexTitle, "", 0, matched, unmatched, playlist.LastUpdated)
		return nil
	}

	// -----------------------------------------------------------------------
	// Create or update the Plex playlist
	// -----------------------------------------------------------------------
	playlistID, err := e.createOrUpdatePlexPlaylist(ctx, plexTitle, plexRatingKeys, librarySectionID)
	if err != nil {
		return fmt.Errorf("failed to create/update Plex playlist %q: %w", plexTitle, err)
	}

	e.recordSyncedPlaylist(playlist.TidalUUID, plexTitle, playlistID, len(plexRatingKeys), matched, unmatched, playlist.LastUpdated)

	e.logger.Info("Playlist synced to Plex",
		"plex_title", plexTitle,
		"plex_id", playlistID,
		"tracks", len(plexRatingKeys),
		"matched_ratio", fmt.Sprintf("%d/%d", matched, len(tracks)),
	)

	return nil
}

// --------------------------------------------------------------------------
// Track Matching: Tidal → Plex
// --------------------------------------------------------------------------

// findPlexTrack searches the Plex music library for a track matching the
// given Tidal track. It tries these strategies in order:
//  1. Check the mapping cache (instant, no API call)
//  2. Search by ISRC if available
//  3. Search by title + artist name
//
// Returns the Plex rating key on success, or an error if not found.
func (e *Engine) findPlexTrack(
	ctx context.Context,
	track tidalclient.NormalizedTrack,
	librarySectionID string,
) (string, error) {
	// Strategy 1: Check mapping cache
	if ratingKey, ok := e.mappingCache.Get(track.TidalID); ok {
		return ratingKey, nil
	}

	// Strategy 2: Search by ISRC
	if track.ISRC != "" {
		ratingKey, err := e.searchPlexByISRC(ctx, track.ISRC, librarySectionID)
		if err == nil && ratingKey != "" {
			e.mappingCache.Set(track.TidalID, ratingKey)
			return ratingKey, nil
		}
	}

	// Strategy 3: Search by title + artist
	ratingKey, err := e.searchPlexByTitleArtist(ctx, track.Title, track.ArtistName, librarySectionID)
	if err == nil && ratingKey != "" {
		e.mappingCache.Set(track.TidalID, ratingKey)
		return ratingKey, nil
	}

	return "", fmt.Errorf("track not found in Plex: %q by %q (ISRC=%s, tidal_id=%d)",
		track.Title, track.ArtistName, track.ISRC, track.TidalID)
}

// searchPlexByISRC searches the Plex library for a track matching the given ISRC.
func (e *Engine) searchPlexByISRC(ctx context.Context, isrc string, librarySectionID string) (string, error) {
	// The Plex search API doesn't have a direct ISRC search field, but we can
	// use the general search endpoint with the ISRC as the query string.
	// Some Plex servers index ISRC in the metadata, so this may work.
	//
	// If ISRC search doesn't work reliably, we fall through to title+artist.

	res, err := e.plex.Search.PerformSearch(ctx, operations.PerformSearchRequest{
		Query: isrc,
	})
	if err != nil {
		return "", err
	}

	if res == nil || res.Object == nil {
		return "", fmt.Errorf("empty search result for ISRC %s", isrc)
	}

	// Look through search results for a music track match
	// The plexgo search response structure varies — we need to check the hubs
	// for track results.
	//
	// Note: This is a simplified implementation. The actual plexgo response
	// structure for search is complex and depends on the SDK version.
	// We return an error here to fall through to title+artist search.
	// TODO: Implement proper ISRC matching when plexgo search response parsing is refined.

	return "", fmt.Errorf("ISRC search not yet fully implemented for %s", isrc)
}

// searchPlexByTitleArtist searches the Plex library for a track matching
// the given title and artist name.
func (e *Engine) searchPlexByTitleArtist(
	ctx context.Context,
	title string,
	artistName string,
	librarySectionID string,
) (string, error) {
	// Search Plex by track title
	query := title

	res, err := e.plex.Search.PerformSearch(ctx, operations.PerformSearchRequest{
		Query: query,
	})
	if err != nil {
		return "", fmt.Errorf("plex search failed for %q: %w", query, err)
	}

	if res == nil || res.Object == nil {
		return "", fmt.Errorf("empty search result for %q", query)
	}

	// The Plex search API returns results in "hubs" organized by type.
	// We need to find the music tracks hub and look for a matching track.
	//
	// Since the plexgo SDK's search response is complex and version-dependent,
	// we use a simplified approach: search and try to match by title + artist
	// in the response metadata.
	//
	// For now, we do a library section search instead, which gives us typed results.

	return e.searchLibrarySection(ctx, title, artistName, librarySectionID)
}

// searchLibrarySection searches a specific Plex library section for a track.
func (e *Engine) searchLibrarySection(
	ctx context.Context,
	title string,
	artistName string,
	librarySectionID string,
) (string, error) {
	// Use the library search endpoint which returns items within a specific section.
	// This is more reliable than global search for matching tracks.

	res, err := e.plex.Search.PerformSearch(ctx, operations.PerformSearchRequest{
		Query:    fmt.Sprintf("%s %s", title, artistName),
		Limit:    plexgo.Pointer[int64](10),
	})
	if err != nil {
		return "", fmt.Errorf("library search failed: %w", err)
	}

	if res == nil || res.Object == nil {
		return "", fmt.Errorf("no results for %q by %q", title, artistName)
	}

	// Attempt to extract track results from the search response.
	// The plexgo SDK search results are nested in hubs. We iterate looking
	// for a track whose title and artist closely match our query.
	//
	// This is a best-effort fuzzy match. For production use, you'd want
	// to also compare duration, album name, etc.

	// Since plexgo's response types for search are generated and may vary,
	// we'll need to handle this based on the actual SDK structure.
	// For the initial implementation, we log and return not-found.
	// The mapping cache will be populated over time as matches are manually verified
	// or as the search implementation is refined.

	e.logger.Debug("Track search attempted (match logic needs refinement)",
		"title", title,
		"artist", artistName,
		"section", librarySectionID,
	)

	return "", fmt.Errorf("no Plex match found for %q by %q (search refinement needed)", title, artistName)
}

// --------------------------------------------------------------------------
// Plex Playlist Management
// --------------------------------------------------------------------------

// createOrUpdatePlexPlaylist creates a new Plex playlist or updates an existing
// one with the given track rating keys.
func (e *Engine) createOrUpdatePlexPlaylist(
	ctx context.Context,
	title string,
	ratingKeys []string,
	librarySectionID string,
) (string, error) {
	// Build the URI for the playlist items.
	// Plex expects a comma-separated list of server:///<machineId>/com.plexapp.plugins.library/library/metadata/<key>
	// Or for simple cases, just the rating keys joined.

	machineID, err := e.getPlexMachineID(ctx)
	if err != nil {
		return "", fmt.Errorf("failed to get Plex machine identifier: %w", err)
	}

	// Build the URI list
	var uris []string
	for _, key := range ratingKeys {
		uri := fmt.Sprintf("server://%s/com.plexapp.plugins.library/library/metadata/%s", machineID, key)
		uris = append(uris, uri)
	}
	uriStr := strings.Join(uris, ",")

	// Check if the playlist already exists by searching for it
	existingID, err := e.findPlexPlaylistByTitle(ctx, title)
	if err == nil && existingID != "" {
		// Playlist exists — we need to clear and repopulate it.
		// The plexgo SDK doesn
