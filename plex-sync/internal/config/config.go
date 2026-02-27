// Package config handles loading and validating configuration for the plex-sync service.
//
// Configuration is loaded from two sources (in order of precedence):
//  1. Environment variables (from .env file or actual environment)
//  2. YAML config file (config.yaml)
//
// Environment variables always override YAML values. This matches the pattern
// used by mediasage and other Plex integration tools.
package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/joho/godotenv"
	"gopkg.in/yaml.v3"
)

// Config holds the fully resolved configuration for the plex-sync service.
type Config struct {
	// Plex server configuration
	Plex PlexConfig `yaml:"plex"`

	// Tidal Bridge (Python service) connection info
	Bridge BridgeConfig `yaml:"bridge"`

	// Sync behavior settings
	Sync SyncConfig `yaml:"sync"`

	// Logging configuration
	Log LogConfig `yaml:"log"`

	// Data directory for persistent state (sync status, mapping cache)
	DataDir string `yaml:"data_dir"`
}

// PlexConfig holds Plex Media Server connection details.
type PlexConfig struct {
	// URL of the Plex server, e.g. "http://localhost:32400"
	URL string `yaml:"url"`

	// Plex authentication token (X-Plex-Token)
	Token string `yaml:"token"`

	// Name of the music library section in Plex
	MusicLibrary string `yaml:"music_library"`

	// Client identifier for this integration (shown in Plex dashboard)
	ClientID string `yaml:"client_id"`

	// Product name shown in Plex dashboard
	ProductName string `yaml:"product_name"`

	// Request timeout for Plex API calls
	Timeout time.Duration `yaml:"timeout"`
}

// BridgeConfig holds connection info for the Python tidal-bridge REST API.
type BridgeConfig struct {
	// Host where the tidal-bridge service is running
	Host string `yaml:"host"`

	// Port for the tidal-bridge REST API
	Port int `yaml:"port"`

	// Request timeout for bridge API calls
	Timeout time.Duration `yaml:"timeout"`

	// Number of retries for failed bridge requests
	MaxRetries int `yaml:"max_retries"`

	// Delay between retries (increases exponentially)
	RetryBaseDelay time.Duration `yaml:"retry_base_delay"`
}

// BaseURL returns the full base URL for the tidal-bridge REST API.
func (b BridgeConfig) BaseURL() string {
	return fmt.Sprintf("http://%s:%d", b.Host, b.Port)
}

// SyncConfig controls what gets synced and how.
type SyncConfig struct {
	// How often to poll the bridge for changes (in minutes)
	IntervalMinutes int `yaml:"interval_minutes"`

	// Whether to sync Tidal favorites as a Plex playlist
	Favorites SyncFavoritesConfig `yaml:"favorites"`

	// Whether to sync Tidal playlists to Plex playlists
	Playlists SyncPlaylistsConfig `yaml:"playlists"`

	// Whether to sync Tidal mixes as Plex playlists (replaces AI Sonic Analysis/DJ)
	Mixes SyncMixesConfig `yaml:"mixes"`

	// Whether to sync Tidal recommendations as Plex playlists
	Recommendations SyncRecommendationsConfig `yaml:"recommendations"`

	// Delete Plex playlists that no longer exist on Tidal
	DeleteOrphaned bool `yaml:"delete_orphaned"`

	// Maximum number of tracks per synced playlist (0 = unlimited)
	MaxTracksPerPlaylist int `yaml:"max_tracks_per_playlist"`

	// Run one sync immediately on startup before entering the periodic loop
	SyncOnStartup bool `yaml:"sync_on_startup"`
}

// Interval returns the sync interval as a time.Duration.
func (s SyncConfig) Interval() time.Duration {
	if s.IntervalMinutes <= 0 {
		return 30 * time.Minute
	}
	return time.Duration(s.IntervalMinutes) * time.Minute
}

// SyncFavoritesConfig controls favorite track syncing.
type SyncFavoritesConfig struct {
	Enabled      bool   `yaml:"enabled"`
	PlaylistName string `yaml:"playlist_name"`
}

// SyncPlaylistsConfig controls playlist mirroring.
type SyncPlaylistsConfig struct {
	Enabled bool   `yaml:"enabled"`
	Prefix  string `yaml:"prefix"`
}

// SyncMixesConfig controls Tidal mix syncing (the AI/DJ replacement).
type SyncMixesConfig struct {
	Enabled bool   `yaml:"enabled"`
	Prefix  string `yaml:"prefix"`
}

// SyncRecommendationsConfig controls Tidal recommendation syncing.
type SyncRecommendationsConfig struct {
	Enabled bool   `yaml:"enabled"`
	Prefix  string `yaml:"prefix"`
}

// LogConfig controls logging behavior.
type LogConfig struct {
	// Log level: "debug", "info", "warn", "error"
	Level string `yaml:"level"`

	// Whether to output structured JSON logs
	JSON bool `yaml:"json"`
}

// Defaults returns a Config populated with sensible default values.
// These match the defaults in .env.example.
func Defaults() *Config {
	return &Config{
		Plex: PlexConfig{
			URL:          "http://localhost:32400",
			Token:        "",
			MusicLibrary: "Music",
			ClientID:     "plexamp-tidal-bridge",
			ProductName:  "PlexAmp Tidal Bridge",
			Timeout:      30 * time.Second,
		},
		Bridge: BridgeConfig{
			Host:           "127.0.0.1",
			Port:           9120,
			Timeout:        60 * time.Second,
			MaxRetries:     3,
			RetryBaseDelay: 2 * time.Second,
		},
		Sync: SyncConfig{
			IntervalMinutes: 30,
			Favorites: SyncFavoritesConfig{
				Enabled:      true,
				PlaylistName: "Tidal Favorites",
			},
			Playlists: SyncPlaylistsConfig{
				Enabled: true,
				Prefix:  "[Tidal]",
			},
			Mixes: SyncMixesConfig{
				Enabled: true,
				Prefix:  "[Tidal Mix]",
			},
			Recommendations: SyncRecommendationsConfig{
				Enabled: true,
				Prefix:  "[Tidal Radio]",
			},
			DeleteOrphaned:       false,
			MaxTracksPerPlaylist: 0,
			SyncOnStartup:        true,
		},
		Log: LogConfig{
			Level: "info",
			JSON:  false,
		},
		DataDir: "./data",
	}
}

// Load reads configuration from the .env file and optional YAML config file,
// merging them with defaults. Environment variables take precedence over YAML,
// which takes precedence over defaults.
//
// It searches for .env in the following order:
//  1. The current working directory
//  2. The project root (two levels up from the plex-sync binary)
//
// It searches for config.yaml in:
//  1. The path specified by CONFIG_PATH env var
//  2. ./config.yaml
//  3. ../config.yaml (project root)
func Load() (*Config, error) {
	cfg := Defaults()

	// --- Load .env file ---
	envPaths := []string{
		".env",
		filepath.Join("..", ".env"),
		filepath.Join("..", "..", ".env"),
	}
	for _, p := range envPaths {
		if _, err := os.Stat(p); err == nil {
			_ = godotenv.Load(p)
			break
		}
	}

	// --- Load YAML config file ---
	yamlPath := envOrDefault("CONFIG_PATH", "")
	if yamlPath == "" {
		candidates := []string{
			"config.yaml",
			filepath.Join("..", "config.yaml"),
			filepath.Join("..", "..", "config.yaml"),
		}
		for _, p := range candidates {
			if _, err := os.Stat(p); err == nil {
				yamlPath = p
				break
			}
		}
	}

	if yamlPath != "" {
		if err := loadYAML(yamlPath, cfg); err != nil {
			return nil, fmt.Errorf("failed to load config YAML %q: %w", yamlPath, err)
		}
	}

	// --- Apply environment variable overrides ---
	applyEnvOverrides(cfg)

	// --- Validate ---
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("configuration validation failed: %w", err)
	}

	return cfg, nil
}

// Validate checks that all required configuration values are present and sensible.
func (c *Config) Validate() error {
	var errs []string

	if c.Plex.URL == "" {
		errs = append(errs, "plex.url (PLEX_URL) is required")
	}
	if c.Plex.Token == "" {
		errs = append(errs, "plex.token (PLEX_TOKEN) is required — see https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/")
	}
	if c.Plex.MusicLibrary == "" {
		errs = append(errs, "plex.music_library (PLEX_MUSIC_LIBRARY) is required")
	}
	if c.Bridge.Port <= 0 || c.Bridge.Port > 65535 {
		errs = append(errs, fmt.Sprintf("bridge.port (BRIDGE_PORT) must be 1-65535, got %d", c.Bridge.Port))
	}
	if c.Sync.IntervalMinutes < 1 {
		errs = append(errs, fmt.Sprintf("sync.interval_minutes (SYNC_INTERVAL_MINUTES) must be >= 1, got %d", c.Sync.IntervalMinutes))
	}

	validLogLevels := map[string]bool{"debug": true, "info": true, "warn": true, "warning": true, "error": true}
	if !validLogLevels[strings.ToLower(c.Log.Level)] {
		errs = append(errs, fmt.Sprintf("log.level (LOG_LEVEL) must be one of debug/info/warn/error, got %q", c.Log.Level))
	}

	if len(errs) > 0 {
		return errors.New(strings.Join(errs, "; "))
	}
	return nil
}

// loadYAML reads a YAML config file and merges it into the provided Config.
func loadYAML(path string, cfg *Config) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return yaml.Unmarshal(data, cfg)
}

// applyEnvOverrides reads environment variables and applies them to the config,
// overriding any values from YAML or defaults.
func applyEnvOverrides(cfg *Config) {
	// --- Plex ---
	if v := os.Getenv("PLEX_URL"); v != "" {
		cfg.Plex.URL = v
	}
	if v := os.Getenv("PLEX_TOKEN"); v != "" {
		cfg.Plex.Token = v
	}
	if v := os.Getenv("PLEX_MUSIC_LIBRARY"); v != "" {
		cfg.Plex.MusicLibrary = v
	}
	if v := os.Getenv("PLEX_CLIENT_ID"); v != "" {
		cfg.Plex.ClientID = v
	}
	if v := os.Getenv("PLEX_PRODUCT_NAME"); v != "" {
		cfg.Plex.ProductName = v
	}

	// --- Bridge ---
	if v := os.Getenv("BRIDGE_HOST"); v != "" {
		cfg.Bridge.Host = v
	}
	if v, err := strconv.Atoi(os.Getenv("BRIDGE_PORT")); err == nil && v > 0 {
		cfg.Bridge.Port = v
	}

	// --- Sync ---
	if v, err := strconv.Atoi(os.Getenv("SYNC_INTERVAL_MINUTES")); err == nil && v > 0 {
		cfg.Sync.IntervalMinutes = v
	}

	if v := os.Getenv("SYNC_FAVORITES"); v != "" {
		cfg.Sync.Favorites.Enabled = parseBool(v)
	}
	if v := os.Getenv("SYNC_FAVORITES_PLAYLIST_NAME"); v != "" {
		cfg.Sync.Favorites.PlaylistName = v
	}

	if v := os.Getenv("SYNC_PLAYLISTS"); v != "" {
		cfg.Sync.Playlists.Enabled = parseBool(v)
	}
	if v := os.Getenv("SYNC_PLAYLIST_PREFIX"); v != "" {
		cfg.Sync.Playlists.Prefix = v
	}

	if v := os.Getenv("SYNC_MIXES"); v != "" {
		cfg.Sync.Mixes.Enabled = parseBool(v)
	}
	if v := os.Getenv("SYNC_MIXES_PREFIX"); v != "" {
		cfg.Sync.Mixes.Prefix = v
	}

	if v := os.Getenv("SYNC_RECOMMENDATIONS"); v != "" {
		cfg.Sync.Recommendations.Enabled = parseBool(v)
	}
	if v := os.Getenv("SYNC_RECOMMENDATIONS_PREFIX"); v != "" {
		cfg.Sync.Recommendations.Prefix = v
	}

	if v := os.Getenv("SYNC_DELETE_ORPHANED"); v != "" {
		cfg.Sync.DeleteOrphaned = parseBool(v)
	}
	if v, err := strconv.Atoi(os.Getenv("SYNC_MAX_TRACKS_PER_PLAYLIST")); err == nil {
		cfg.Sync.MaxTracksPerPlaylist = v
	}

	// --- Log ---
	if v := os.Getenv("LOG_LEVEL"); v != "" {
		cfg.Log.Level = strings.ToLower(v)
	}

	// --- Data ---
	if v := os.Getenv("DATA_DIR"); v != "" {
		cfg.DataDir = v
	}
}

// envOrDefault returns the value of an environment variable, or the default if unset/empty.
func envOrDefault(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

// parseBool parses a string as a boolean, accepting "true", "1", "yes" (case-insensitive).
func parseBool(s string) bool {
	s = strings.TrimSpace(strings.ToLower(s))
	return s == "true" || s == "1" || s == "yes"
}

// String returns a redacted summary of the config suitable for logging.
// Secrets (tokens) are truncated to avoid leaking them in logs.
func (c *Config) String() string {
	tokenPreview := "(not set)"
	if c.Plex.Token != "" {
		if len(c.Plex.Token) > 8 {
			tokenPreview = c.Plex.Token[:8] + "..."
		} else {
			tokenPreview = "***"
		}
	}

	return fmt.Sprintf(
		"Config{plex=%s token=%s lib=%q bridge=%s sync_interval=%dm favorites=%v playlists=%v mixes=%v log=%s data=%s}",
		c.Plex.URL,
		tokenPreview,
		c.Plex.MusicLibrary,
		c.Bridge.BaseURL(),
		c.Sync.IntervalMinutes,
		c.Sync.Favorites.Enabled,
		c.Sync.Playlists.Enabled,
		c.Sync.Mixes.Enabled,
		c.Log.Level,
		c.DataDir,
	)
}

// EnsureDataDir creates the data directory if it doesn't exist.
func (c *Config) EnsureDataDir() error {
	return os.MkdirAll(c.DataDir, 0o755)
}

// SyncStatePath returns the path to the sync state file within the data directory.
func (c *Config) SyncStatePath() string {
	return filepath.Join(c.DataDir, "sync_state.json")
}

// MappingCachePath returns the path to the Tidal↔Plex ID mapping cache.
func (c *Config) MappingCachePath() string {
	return filepath.Join(c.DataDir, "tidal_plex_mappings.json")
}
