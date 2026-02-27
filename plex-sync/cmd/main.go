// Command plex-sync is the Go service that bridges Tidal metadata from the
// Python tidal-bridge REST API into your Plex Media Server using the plexgo SDK.
//
// It periodically polls the tidal-bridge for your Tidal collections, playlists,
// favorites, and mixes — then creates/updates matching playlists in Plex so they
// appear in PlexAmp.
//
// This replaces AI-based Sonic Analysis and DJ features with Tidal's own
// curated mixes and recommendations, accessed through YOUR OWN Tidal developer
// credentials (managed by the Python tidal-bridge service).
//
// Usage:
//
//	# Start the sync service (reads config from .env / config.yaml):
//	go run ./cmd/main.go
//
//	# Run a single sync pass and exit:
//	go run ./cmd/main.go --once
//
//	# Show loaded config and exit (for debugging):
//	go run ./cmd/main.go --check-config
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/plexamp-universal-intg/plex-sync/internal/config"
	"github.com/plexamp-universal-intg/plex-sync/internal/sync"
	"github.com/plexamp-universal-intg/plex-sync/internal/tidalclient"
)

const (
	banner = `
╔══════════════════════════════════════════════════════════════╗
║            PlexAmp ↔ Tidal Bridge — plex-sync               ║
║                                                              ║
║  Syncs your Tidal collections, playlists, and mixes into     ║
║  Plex playlists for PlexAmp — using YOUR credentials.        ║
║  No AI. No third-party hosts. Just your music.               ║
╚══════════════════════════════════════════════════════════════╝`
)

func main() {
	// -----------------------------------------------------------------------
	// CLI flags
	// -----------------------------------------------------------------------
	var (
		flagOnce        = flag.Bool("once", false, "Run a single sync pass and exit (don't loop)")
		flagCheckConfig = flag.Bool("check-config", false, "Load and print config, then exit")
		flagVerbose     = flag.Bool("verbose", false, "Enable debug logging")
	)
	flag.Parse()

	// -----------------------------------------------------------------------
	// Logger setup
	// -----------------------------------------------------------------------
	logLevel := slog.LevelInfo
	if *flagVerbose {
		logLevel = slog.LevelDebug
	}

	handler := slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: logLevel,
	})
	logger := slog.New(handler)
	slog.SetDefault(logger)

	fmt.Println(banner)
	logger.Info("Starting plex-sync service")

	// -----------------------------------------------------------------------
	// Load config
	// -----------------------------------------------------------------------
	cfg, err := config.Load()
	if err != nil {
		logger.Error("Failed to load configuration", "error", err)
		logger.Error("Hint: copy .env.example to .env and fill in your Plex token.")
		os.Exit(1)
	}

	// Override log level from config if not explicitly set via --verbose
	if !*flagVerbose && strings.EqualFold(cfg.Log.Level, "debug") {
		logLevel = slog.LevelDebug
		handler = slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: logLevel})
		logger = slog.New(handler)
		slog.SetDefault(logger)
	}

	logger.Info("Configuration loaded", "config", cfg.String())

	if *flagCheckConfig {
		fmt.Println("\nConfiguration OK. Exiting.")
		os.Exit(0)
	}

	// Ensure data directory exists
	if err := cfg.EnsureDataDir(); err != nil {
		logger.Error("Failed to create data directory", "path", cfg.DataDir, "error", err)
		os.Exit(1)
	}

	// -----------------------------------------------------------------------
	// Build clients
	// -----------------------------------------------------------------------

	// Tidal bridge client (talks to the Python tidal-bridge REST API)
	bridgeClient := tidalclient.New(tidalclient.ClientConfig{
		BaseURL:        cfg.Bridge.BaseURL(),
		Timeout:        cfg.Bridge.Timeout,
		MaxRetries:     cfg.Bridge.MaxRetries,
		RetryBaseDelay: cfg.Bridge.RetryBaseDelay,
	}, logger)

	// -----------------------------------------------------------------------
	// Context for graceful shutdown
	// -----------------------------------------------------------------------
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	go func() {
		sig := <-sigCh
		logger.Info("Received shutdown signal", "signal", sig.String())
		cancel()
	}()

	// -----------------------------------------------------------------------
	// Wait for the tidal-bridge to be ready
	// -----------------------------------------------------------------------
	logger.Info("Checking tidal-bridge availability...", "url", cfg.Bridge.BaseURL())

	waitCtx, waitCancel := context.WithTimeout(ctx, 30*time.Second)
	defer waitCancel()

	if err := bridgeClient.WaitForReady(waitCtx, 2*time.Second); err != nil {
		logger.Error("Tidal bridge is not reachable",
			"url", cfg.Bridge.BaseURL(),
			"error", err,
		)
		logger.Error("Make sure the tidal-bridge Python service is running:")
		logger.Error("  cd tidal-bridge && uvicorn server.app:app --host 127.0.0.1 --port 9120")
		os.Exit(1)
	}

	// -----------------------------------------------------------------------
	// Build sync engine
	// -----------------------------------------------------------------------
	syncEngine, err := sync.NewEngine(sync.EngineConfig{
		PlexURL:          cfg.Plex.URL,
		PlexToken:        cfg.Plex.Token,
		PlexMusicLibrary: cfg.Plex.MusicLibrary,
		PlexClientID:     cfg.Plex.ClientID,
		PlexProductName:  cfg.Plex.ProductName,

		SyncFavorites:            cfg.Sync.Favorites.Enabled,
		FavoritesPlaylistName:    cfg.Sync.Favorites.PlaylistName,
		SyncPlaylists:            cfg.Sync.Playlists.Enabled,
		PlaylistPrefix:           cfg.Sync.Playlists.Prefix,
		SyncMixes:                cfg.Sync.Mixes.Enabled,
		MixPrefix:                cfg.Sync.Mixes.Prefix,
		SyncRecommendations:      cfg.Sync.Recommendations.Enabled,
		RecommendationsPrefix:    cfg.Sync.Recommendations.Prefix,
		DeleteOrphaned:           cfg.Sync.DeleteOrphaned,
		MaxTracksPerPlaylist:     cfg.Sync.MaxTracksPerPlaylist,

		SyncStatePath:   cfg.SyncStatePath(),
		MappingCachePath: cfg.MappingCachePath(),
	}, bridgeClient, logger)
	if err != nil {
		logger.Error("Failed to create sync engine", "error", err)
		os.Exit(1)
	}

	// -----------------------------------------------------------------------
	// Run sync
	// -----------------------------------------------------------------------
	if *flagOnce {
		logger.Info("Running single sync pass (--once mode)")
		if err := syncEngine.RunOnce(ctx); err != nil {
			logger.Error("Sync failed", "error", err)
			os.Exit(1)
		}
		logger.Info("Single sync pass completed successfully")
		return
	}

	// Periodic sync loop
	interval := cfg.Sync.Interval()
	logger.Info("Entering periodic sync loop",
		"interval", interval.String(),
		"sync_on_startup", cfg.Sync.SyncOnStartup,
	)

	if cfg.Sync.SyncOnStartup {
		logger.Info("Running initial sync pass...")
		if err := syncEngine.RunOnce(ctx); err != nil {
			logger.Warn("Initial sync pass encountered errors (will retry next interval)", "error", err)
		} else {
			logger.Info("Initial sync pass completed successfully")
		}
	}

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			logger.Info("Shutting down plex-sync gracefully...")
			return

		case <-ticker.C:
			logger.Info("Starting periodic sync pass...")
			startTime := time.Now()

			if err := syncEngine.RunOnce(ctx); err != nil {
				if ctx.Err() != nil {
					// Context was cancelled during sync — this is a clean shutdown
					logger.Info("Sync interrupted by shutdown signal")
					return
				}
				logger.Error("Periodic sync pass failed", "error", err, "duration", time.Since(startTime).Round(time.Second))
			} else {
				logger.Info("Periodic sync pass completed",
					"duration", time.Since(startTime).Round(time.Second),
				)
			}
		}
	}
}
