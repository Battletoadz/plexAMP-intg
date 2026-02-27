// Package tidalclient provides a Go HTTP client for communicating with the
// Python tidal-bridge REST API service. This is the Go-side counterpart that
// consumes the FastAPI endpoints exposed by the tidal-bridge to fetch Tidal
// collections, playlists, metadata, mixes, and stream info — all authenticated
// through YOUR OWN Tidal developer credentials on the Python side.
//
// The client handles:
//   - Health checking and auth status polling
//   - Fetching normalized playlists/favorites/mixes for Plex sync
//   - Retry logic with exponential backoff for transient failures
//   - Structured logging for debugging sync issues
//
// This client does NOT talk to the Tidal API directly — it relies entirely
// on the Python tidal-bridge service for that. The separation keeps the Tidal
// OAuth2 complexity in Python (forked from tiddl) and the Plex SDK work in Go
// (using plexgo).
package tidalclient

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// --------------------------------------------------------------------------
// Configuration
// --------------------------------------------------------------------------

// ClientConfig holds connection parameters for the tidal-bridge REST API.
type ClientConfig struct {
	// BaseURL is the full base URL of the tidal-bridge service,
	// e.g. "http://127.0.0.1:9120"
	BaseURL string

	// Timeout is the HTTP request timeout for individual API calls.
	Timeout time.Duration

	// MaxRetries is the number of retry attempts for transient failures (5xx, timeouts).
	MaxRetries int

	// RetryBaseDelay is the base delay for exponential backoff between retries.
	RetryBaseDelay time.Duration
}

// DefaultConfig returns a ClientConfig with sensible defaults matching .env.example.
func DefaultConfig() ClientConfig {
	return ClientConfig{
		BaseURL:        "http://127.0.0.1:9120",
		Timeout:        60 * time.Second,
		MaxRetries:     3,
		RetryBaseDelay: 2 * time.Second,
	}
}

// --------------------------------------------------------------------------
// Client
// --------------------------------------------------------------------------

// Client is an HTTP client for the Python tidal-bridge REST API.
type Client struct {
	cfg    ClientConfig
	http   *http.Client
	logger *slog.Logger
}

// New creates a new tidal-bridge API client.
func New(cfg ClientConfig, logger *slog.Logger) *Client {
	if logger == nil {
		logger = slog.Default()
	}
	return &Client{
		cfg: cfg,
		http: &http.Client{
			Timeout: cfg.Timeout,
		},
		logger: logger.With("component", "tidalclient"),
	}
}

// --------------------------------------------------------------------------
// Response Models
// --------------------------------------------------------------------------

// HealthResponse represents the GET /health response from the bridge.
type HealthResponse struct {
	Status        string  `json:"status"`
	Service       string  `json:"service"`
	Version       string  `json:"version"`
	Authenticated bool    `json:"authenticated"`
	UptimeSeconds float64 `json:"uptime_seconds"`
	UserID        string  `json:"user_id,omitempty"`
	CountryCode   string  `json:"country_code,omitempty"`
}

// AuthStatusResponse represents the GET /auth/status response.
type AuthStatusResponse struct {
	Authenticated     bool     `json:"authenticated"`
	TokenValid        bool     `json:"token_valid"`
	CanRefresh        bool     `json:"can_refresh"`
	ExpiresAt         *float64 `json:"expires_at,omitempty"`
	SecondsUntilExpiry *float64 `json:"seconds_until_expiry,omitempty"`
	UserID            string   `json:"user_id,omitempty"`
	Scopes            []string `json:"scopes,omitempty"`
	ClientIDPrefix    string   `json:"client_id_prefix,omitempty"`
}

// DeviceAuthResponse represents the POST /auth/login response.
type DeviceAuthResponse struct {
	UserCode        string `json:"user_code"`
	VerificationURI string `json:"verification_uri"`
	ExpiresIn       int    `json:"expires_in"`
	Message         string `json:"message"`
}

// SessionResponse represents the GET /session response.
type SessionResponse struct {
	SessionID   string `json:"sessionId"`
	UserID      int64  `json:"userId"`
	CountryCode string `json:"countryCode"`
	ChannelID   *int64 `json:"channelId,omitempty"`
	PartnerID   *int64 `json:"partnerId,omitempty"`
}

// NormalizedTrack is a simplified track model optimized for Plex sync.
type NormalizedTrack struct {
	TidalID         int      `json:"tidal_id"`
	Title           string   `json:"title"`
	Version         string   `json:"version,omitempty"`
	DurationSeconds int      `json:"duration_seconds"`
	TrackNumber     int      `json:"track_number"`
	DiscNumber      int      `json:"disc_number"`
	ISRC            string   `json:"isrc,omitempty"`
	Explicit        bool     `json:"explicit"`
	ArtistName      string   `json:"artist_name"`
	ArtistNames     []string `json:"artist_names,omitempty"`
	AlbumTitle      string   `json:"album_title,omitempty"`
	AlbumID         *int     `json:"album_id,omitempty"`
	CoverURL        string   `json:"cover_url,omitempty"`
	AudioQuality    string   `json:"audio_quality,omitempty"`
	Popularity      *int     `json:"popularity,omitempty"`
	ReplayGain      *float64 `json:"replay_gain,omitempty"`
}

// NormalizedPlaylist is a simplified playlist model for Plex sync.
type NormalizedPlaylist struct {
	TidalUUID       string            `json:"tidal_uuid"`
	Title           string            `json:"title"`
	Description     string            `json:"description,omitempty"`
	TrackCount      int               `json:"track_count"`
	DurationSeconds *int              `json:"duration_seconds,omitempty"`
	CoverURL        string            `json:"cover_url,omitempty"`
	PlaylistType    string            `json:"playlist_type,omitempty"`
	LastUpdated     string            `json:"last_updated,omitempty"`
	Created         string            `json:"created,omitempty"`
	Tracks          []NormalizedTrack `json:"tracks,omitempty"`
}

// StreamURLResponse represents the GET /tracks/{id}/stream response.
type StreamURLResponse struct {
	TrackID      int    `json:"track_id"`
	URL          string `json:"url,omitempty"`
	AudioQuality string `json:"audio_quality,omitempty"`
	AudioMode    string `json:"audio_mode,omitempty"`
	BitDepth     *int   `json:"bit_depth,omitempty"`
	SampleRate   *int   `json:"sample_rate,omitempty"`
	IsEncrypted  bool   `json:"is_encrypted"`
	Error        string `json:"error,omitempty"`
}

// FavoriteIDs represents the GET /favorites/ids response.
type FavoriteIDs struct {
	Track    []int    `json:"TRACK"`
	Video    []int    `json:"VIDEO"`
	Album    []int    `json:"ALBUM"`
	Artist   []int    `json:"ARTIST"`
	Playlist []string `json:"PLAYLIST"`
}

// ArtistMixesResponse represents the GET /artists/{id}/mixes response.
type ArtistMixesResponse struct {
	ArtistID int               `json:"artist_id"`
	Mixes    map[string]string `json:"mixes"`
	Hint     string            `json:"hint,omitempty"`
}

// PlaylistListResponse wraps a list of playlists from list endpoints.
type PlaylistListResponse struct {
	Total     int                  `json:"total"`
	Playlists []NormalizedPlaylist `json:"playlists"`
}

// TrackListResponse wraps a list of tracks from various endpoints.
type TrackListResponse struct {
	Total  int               `json:"total"`
	Tracks []json.RawMessage `json:"tracks"`
}

// SyncSnapshotResponse is the full export for the sync engine.
type SyncSnapshotResponse struct {
	Timestamp     float64              `json:"timestamp"`
	UserID        string               `json:"user_id"`
	CountryCode   string               `json:"country_code"`
	Favorites     *NormalizedPlaylist   `json:"favorites,omitempty"`
	Playlists     []NormalizedPlaylist  `json:"playlists,omitempty"`
	MixPlaylists  []NormalizedPlaylist  `json:"mix_playlists,omitempty"`
}

// --------------------------------------------------------------------------
// Health & Auth Endpoints
// --------------------------------------------------------------------------

// Health checks if the tidal-bridge service is running and returns its status.
func (c *Client) Health(ctx context.Context) (*HealthResponse, error) {
	var resp HealthResponse
	if err := c.get(ctx, "/health", nil, &resp); err != nil {
		return nil, fmt.Errorf("health check failed: %w", err)
	}
	return &resp, nil
}

// WaitForReady polls the health endpoint until the bridge service is reachable
// and authenticated, or until the context is cancelled.
func (c *Client) WaitForReady(ctx context.Context, pollInterval time.Duration) error {
	c.logger.Info("Waiting for tidal-bridge to become ready...", "url", c.cfg.BaseURL)

	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return fmt.Errorf("tidal-bridge did not become ready: %w", ctx.Err())
		case <-ticker.C:
			health, err := c.Health(ctx)
			if err != nil {
				c.logger.Debug("Bridge not ready yet", "error", err)
				continue
			}

			if health.Status != "ok" {
				c.logger.Debug("Bridge status is not ok", "status", health.Status)
				continue
			}

			if !health.Authenticated {
				c.logger.Warn("Bridge is running but NOT authenticated with Tidal. Use POST /auth/login on the bridge to authenticate.")
				// Return nil so the sync service can start — it will get 401 errors
				// from the bridge until auth is completed, which is handled gracefully.
				return nil
			}

			c.logger.Info("Tidal bridge is ready",
				"user_id", health.UserID,
				"country", health.CountryCode,
				"uptime", fmt.Sprintf("%.0fs", health.UptimeSeconds),
			)
			return nil
		}
	}
}

// AuthStatus checks the current authentication status of the bridge.
func (c *Client) AuthStatus(ctx context.Context) (*AuthStatusResponse, error) {
	var resp AuthStatusResponse
	if err := c.get(ctx, "/auth/status", nil, &resp); err != nil {
		return nil, fmt.Errorf("auth status check failed: %w", err)
	}
	return &resp, nil
}

// AuthLogin initiates the Tidal device authorization flow through the bridge.
// Returns the user code and verification URL for the user to complete auth.
func (c *Client) AuthLogin(ctx context.Context) (*DeviceAuthResponse, error) {
	var resp DeviceAuthResponse
	if err := c.post(ctx, "/auth/login", nil, &resp); err != nil {
		return nil, fmt.Errorf("auth login request failed: %w", err)
	}
	return &resp, nil
}

// AuthRefresh forces an immediate token refresh on the bridge.
func (c *Client) AuthRefresh(ctx context.Context) error {
	var resp map[string]interface{}
	if err := c.post(ctx, "/auth/refresh", nil, &resp); err != nil {
		return fmt.Errorf("auth refresh failed: %w", err)
	}
	return nil
}

// GetSession fetches the current Tidal session info from the bridge.
func (c *Client) GetSession(ctx context.Context) (*SessionResponse, error) {
	var resp SessionResponse
	if err := c.get(ctx, "/session", nil, &resp); err != nil {
		return nil, fmt.Errorf("session fetch failed: %w", err)
	}
	return &resp, nil
}

// --------------------------------------------------------------------------
// Favorites / Collection Endpoints
// --------------------------------------------------------------------------

// GetFavoriteIDs fetches IDs of all favorited items (lightweight, no metadata).
func (c *Client) GetFavoriteIDs(ctx context.Context) (*FavoriteIDs, error) {
	var resp FavoriteIDs
	if err := c.get(ctx, "/favorites/ids", nil, &resp); err != nil {
		return nil, fmt.Errorf("failed to fetch favorite IDs: %w", err)
	}
	return &resp, nil
}

// ExportFavorites fetches all favorite tracks as a NormalizedPlaylist for Plex sync.
func (c *Client) ExportFavorites(ctx context.Context, playlistName string) (*NormalizedPlaylist, error) {
	params := url.Values{}
	if playlistName != "" {
		params.Set("playlist_name", playlistName)
	}

	var resp NormalizedPlaylist
	if err := c.get(ctx, "/favorites/export", params, &resp); err != nil {
		return nil, fmt.Errorf("failed to export favorites: %w", err)
	}
	return &resp, nil
}

// --------------------------------------------------------------------------
// Playlist Endpoints
// --------------------------------------------------------------------------

// ExportAllPlaylists fetches ALL user playlists as NormalizedPlaylist slice.
// This is the primary bulk-sync call for the sync engine.
func (c *Client) ExportAllPlaylists(ctx context.Context) ([]NormalizedPlaylist, error) {
	var resp PlaylistListResponse
	if err := c.get(ctx, "/playlists/export/all", nil, &resp); err != nil {
		return nil, fmt.Errorf("failed to export all playlists: %w", err)
	}
	return resp.Playlists, nil
}

// ExportPlaylist fetches a single playlist as a NormalizedPlaylist for Plex sync.
func (c *Client) ExportPlaylist(ctx context.Context, uuid string) (*NormalizedPlaylist, error) {
	var resp NormalizedPlaylist
	path := fmt.Sprintf("/playlists/%s/export", url.PathEscape(uuid))
	if err := c.get(ctx, path, nil, &resp); err != nil {
		return nil, fmt.Errorf("failed to export playlist %s: %w", uuid, err)
	}
	return &resp, nil
}

// --------------------------------------------------------------------------
// Mix / Radio Endpoints (Tidal's DJ/Sonic Analysis replacement — NO AI)
// --------------------------------------------------------------------------

// GetArtistMixes fetches available Tidal mix IDs for an artist.
// These can be passed to ExportMix to create radio/DJ playlists in Plex
// without any AI — using Tidal's native recommendation engine.
func (c *Client) GetArtistMixes(ctx context.Context, artistID int) (*ArtistMixesResponse, error) {
	var resp ArtistMixesResponse
	path := fmt.Sprintf("/artists/%d/mixes", artistID)
	if err := c.get(ctx, path, nil, &resp); err != nil {
		return nil, fmt.Errorf("failed to fetch artist mixes for %d: %w", artistID, err)
	}
	return &resp, nil
}

// ExportMix fetches a Tidal mix and returns it as a NormalizedPlaylist for Plex sync.
// This is the core method for generating radio/DJ playlists from Tidal's
// recommendation engine instead of GenAI.
func (c *Client) ExportMix(ctx context.Context, mixID string, playlistName string) (*NormalizedPlaylist, error) {
	params := url.Values{}
	if playlistName != "" {
		params.Set("playlist_name", playlistName)
	}

	var resp NormalizedPlaylist
	path := fmt.Sprintf("/mixes/%s/export", url.PathEscape(mixID))
	if err := c.get(ctx, path, params, &resp); err != nil {
		return nil, fmt.Errorf("failed to export mix %s: %w", mixID, err)
	}
	return &resp, nil
}

// --------------------------------------------------------------------------
// Track Endpoints
// --------------------------------------------------------------------------

// GetTrackStream fetches the stream URL and audio info for a track.
func (c *Client) GetTrackStream(ctx context.Context, trackID int, quality string) (*StreamURLResponse, error) {
	params := url.Values{}
	if quality != "" {
		params.Set("quality", quality)
	}

	var resp StreamURLResponse
	path := fmt.Sprintf("/tracks/%d/stream", trackID)
	if err := c.get(ctx, path, params, &resp); err != nil {
		return nil, fmt.Errorf("failed to get stream for track %d: %w", trackID, err)
	}
	return &resp, nil
}

// --------------------------------------------------------------------------
// Sync Snapshot
// --------------------------------------------------------------------------

// GetSyncSnapshot fetches a full export snapshot containing favorites,
// playlists, and mixes — everything the sync engine needs in one call.
func (c *Client) GetSyncSnapshot(ctx context.Context) (*SyncSnapshotResponse, error) {
	var resp SyncSnapshotResponse
	if err := c.get(ctx, "/sync/snapshot", nil, &resp); err != nil {
		return nil, fmt.Errorf("failed to fetch sync snapshot: %w", err)
	}
	return &resp, nil
}

// --------------------------------------------------------------------------
// Search
// --------------------------------------------------------------------------

// SearchResult holds raw search results from the bridge. The inner fields
// are left as json.RawMessage so the caller can parse them into the
// appropriate types.
type SearchResult struct {
	Artists   json.RawMessage `json:"artists"`
	Albums    json.RawMessage `json:"albums"`
	Tracks    json.RawMessage `json:"tracks"`
	Videos    json.RawMessage `json:"videos"`
	Playlists json.RawMessage `json:"playlists"`
}

// Search performs a Tidal search through the bridge.
func (c *Client) Search(ctx context.Context, query string, limit int) (*SearchResult, error) {
	params := url.Values{
		"q": {query},
	}
	if limit > 0 {
		params.Set("limit", strconv.Itoa(limit))
	}

	var resp SearchResult
	if err := c.get(ctx, "/search", params, &resp); err != nil {
		return nil, fmt.Errorf("search failed: %w", err)
	}
	return &resp, nil
}

// --------------------------------------------------------------------------
// HTTP Transport Layer (with retries + exponential backoff)
// --------------------------------------------------------------------------

// APIError represents a non-2xx response from the tidal-bridge API.
type APIError struct {
	StatusCode int
	Body       string
	Endpoint   string
}

func (e *APIError) Error() string {
	body := e.Body
	if len(body) > 200 {
		body = body[:200] + "..."
	}
	return fmt.Sprintf("tidal-bridge API error: %s returned HTTP %d: %s", e.Endpoint, e.StatusCode, body)
}

// IsNotAuthenticated returns true if the error is a 401 from the bridge,
// meaning the Tidal tokens are missing or expired.
func (e *APIError) IsNotAuthenticated() bool {
	return e.StatusCode == http.StatusUnauthorized
}

// IsNotFound returns true if the requested resource was not found.
func (e *APIError) IsNotFound() bool {
	return e.StatusCode == http.StatusNotFound
}

// get performs a GET request to the bridge with retry logic.
func (c *Client) get(ctx context.Context, path string, params url.Values, target interface{}) error {
	return c.doRequest(ctx, http.MethodGet, path, params, nil, target)
}

// post performs a POST request to the bridge with retry logic.
func (c *Client) post(ctx context.Context, path string, body interface{}, target interface{}) error {
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("failed to marshal request body: %w", err)
		}
		bodyReader = strings.NewReader(string(data))
	}
	return c.doRequest(ctx, http.MethodPost, path, nil, bodyReader, target)
}

// doRequest executes an HTTP request with retry logic and exponential backoff.
func (c *Client) doRequest(
	ctx context.Context,
	method string,
	path string,
	params url.Values,
	body io.Reader,
	target interface{},
) error {
	fullURL := c.cfg.BaseURL + path
	if params != nil && len(params) > 0 {
		fullURL += "?" + params.Encode()
	}

	var lastErr error

	for attempt := 0; attempt <= c.cfg.MaxRetries; attempt++ {
		if attempt > 0 {
			delay := c.backoffDelay(attempt)
			c.logger.Debug("Retrying bridge request",
				"attempt", attempt,
				"max_retries", c.cfg.MaxRetries,
				"delay", delay,
				"path", path,
			)

			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
		}

		req, err := http.NewRequestWithContext(ctx, method, fullURL, body)
		if err != nil {
			return fmt.Errorf("failed to create request for %s %s: %w", method, path, err)
		}

		req.Header.Set("Accept", "application/json")
		if method == http.MethodPost && body != nil {
			req.Header.Set("Content-Type", "application/json")
		}

		resp, err := c.http.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("request to %s %s failed: %w", method, path, err)
			c.logger.Warn("Bridge request failed",
				"path", path,
				"attempt", attempt+1,
				"error", err,
			)
			continue
		}

		respBody, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()

		if readErr != nil {
			lastErr = fmt.Errorf("failed to read response body from %s: %w", path, readErr)
			continue
		}

		// Success
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			if target != nil {
				if err := json.Unmarshal(respBody, target); err != nil {
					return fmt.Errorf("failed to parse JSON response from %s: %w (body: %s)",
						path, err, truncate(string(respBody), 500))
				}
			}
			return nil
		}

		apiErr := &APIError{
			StatusCode: resp.StatusCode,
			Body:       string(respBody),
			Endpoint:   path,
		}

		// Don't retry client errors (4xx) — these are not transient
		if resp.StatusCode >= 400 && resp.StatusCode < 500 {
			return apiErr
		}

		// Retry server errors (5xx)
		lastErr = apiErr
		c.logger.Warn("Bridge returned server error",
			"path", path,
			"status", resp.StatusCode,
			"attempt", attempt+1,
			"max_retries", c.cfg.MaxRetries,
		)
	}

	return fmt.Errorf("all %d attempts failed for %s %s: %w",
		c.cfg.MaxRetries+1, method, path, lastErr)
}

// backoffDelay computes the exponential backoff delay for a given attempt.
func (c *Client) backoffDelay(attempt int) time.Duration {
	delay := float64(c.cfg.RetryBaseDelay) * math.Pow(2, float64(attempt-1))
	maxDelay := 30 * time.Second
	if time.Duration(delay) > maxDelay {
		return maxDelay
	}
	return time.Duration(delay)
}

// truncate shortens a string to maxLen, appending "..." if truncated.
func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + "..."
}
