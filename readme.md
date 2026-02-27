# PlexAmp ↔ Tidal Universal Integration

> Sync your Tidal collections, playlists, and mixes into Plex playlists for PlexAmp — using **YOUR** credentials. No AI. No third-party hosts. Just your music.

---

## Why This Exists

In 2024, Plex officially ended its Tidal integration. If you're a PlexAmp user who also subscribes to Tidal, you lost the ability to browse and play your Tidal library through PlexAmp. Several community projects have attempted to fill this gap:

- **[MediaSage](https://github.com/ecwilsonaz/mediasage)** — AI-powered playlist generator for Plex (uses LLMs to pick tracks)
- **[HiFi](https://github.com/sachinsenal0x64/hifi)** — Go-based Subsonic proxy that translates Subsonic API calls into Tidal API calls

Both are interesting, but neither does exactly what we want:

1. **MediaSage** requires an LLM API key and uses GenAI to analyze and recommend music. We don't want AI — we already have a curated Tidal collection and Tidal's own recommendation engine.
2. **HiFi** implements a Subsonic-compatible proxy, which is clever but requires trusting a managed host or running their full stack. We want to use our own Tidal developer credentials directly.

**This project** takes a different approach:

- **Python service** (`tidal-bridge`) — forked from the excellent [tiddl](https://github.com/oskvr37/tiddl) — handles all Tidal API communication using **your own** developer credentials from <https://developer.tidal.com/dashboard>
- **Go service** (`plex-sync`) — uses the [plexgo SDK](https://github.com/LukeHagar/plexgo) to push Tidal metadata into your Plex server as native playlists
- **No AI** — PlexAmp's Sonic Analysis and DJ features are replaced by Tidal's own mixes, artist radio, and recommendations (accessed via the `recommendations.read` and `playback` scopes)
- **No third-party auth** — you authenticate with your normal Tidal subscription through your own registered developer app

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────────────┐
│                         Your Machine / LAN                          │
│                                                                      │
│  ┌─────────────────────┐         ┌──────────────────────┐            │
│  │   tidal-bridge       │  HTTP   │    plex-sync          │           │
│  │   (Python/FastAPI)   │◄───────►│    (Go/plexgo SDK)    │           │
│  │   :9120              │  REST   │    :9121              │           │
│  │                      │         │                       │           │
│  │  • OAuth2 device     │         │  • Fetches normalized │           │
│  │    auth with YOUR    │         │    playlists from     │           │
│  │    Tidal dev creds   │         │    tidal-bridge       │           │
│  │  • Tidal API client  │         │  • Matches tracks to  │           │
│  │  • Collections,      │         │    Plex library items  │           │
│  │    playlists, mixes  │         │  • Creates/updates    │           │
│  │  • Stream URL        │         │    Plex playlists     │           │
│  │    resolution        │         │  • Periodic sync loop │           │
│  │  • Metadata caching  │         │  • Mapping cache      │           │
│  └──────────┬───────────┘         └──────────┬────────────┘           │
│             │                                │                        │
│             ▼                                ▼                        │
│  ┌─────────────────────┐         ┌──────────────────────┐            │
│  │   Tidal API          │         │   Plex Media Server   │           │
│  │   api.tidal.com      │         │   :32400              │           │
│  │   auth.tidal.com     │         │                       │           │
│  │                      │         │   ┌──────────────┐    │           │
│  │   YOUR subscription  │         │   │  PlexAmp     │    │           │
│  │   YOUR dev app       │         │   │  (sees the   │    │           │
│  │                      │         │   │   synced     │    │           │
│  │                      │         │   │   playlists) │    │           │
│  └──────────────────────┘         │   └──────────────┘    │           │
│                                   └──────────────────────┘            │
└──────────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Authentication** — The Python `tidal-bridge` service performs OAuth2 device authorization against `auth.tidal.com` using your own `client_id` and `client_secret` from the Tidal Developer Dashboard. Tokens are persisted to disk so you only auth once.

2. **Collection Fetch** — `tidal-bridge` exposes REST endpoints that return your Tidal favorites, playlists, mixes, and track metadata as normalized JSON. All Tidal API complexity (pagination, caching, token refresh, rate limiting) is handled here.

3. **Sync** — The Go `plex-sync` service periodically calls the `tidal-bridge` REST API, gets your Tidal data as `NormalizedPlaylist` objects, matches each track to your Plex music library (by ISRC, then by title+artist), and creates or updates Plex playlists.

4. **PlexAmp** — The synced playlists appear as native Plex playlists in PlexAmp. Your Tidal favorites become a "Tidal Favorites" playlist. Your Tidal playlists become `[Tidal] Playlist Name` playlists. Tidal artist mixes become `[Tidal Mix] Artist Radio` playlists — replacing AI-based Sonic Analysis.

### Why Python + Go?

- **Python** for the Tidal side because `tiddl` already has a clean, well-tested Tidal API client. OAuth2 device flow, token refresh, stream manifest decoding, and all the API models are already implemented. Forking this saves months of work.
- **Go** for the Plex side because `plexgo` is a comprehensive, auto-generated Go SDK covering the entire Plex API. It's strongly typed, well-documented, and actively maintained. The sync engine benefits from Go's concurrency model for parallel track matching.
- The two services communicate over a simple local REST API, so they're independently deployable and debuggable.

---

## Tidal Developer Dashboard Setup

Before anything else, you need a Tidal developer app:

1. Go to <https://developer.tidal.com/dashboard>
2. Create a new app (or use the one at `e3613480-eb98-4b75-af05-ebb9e6bf1e30` if you already created it)
3. Configure it as follows:

| Field | Value |
|---|---|
| **Name** | `PlexAmp Tidal Bridge` |
| **Description** | A bridge for Plex that syncs Tidal music metadata |
| **Platform preset** | `WEB` (or `ANDROID` / `IOS` if you want mobile playback) |
| **Redirect URI 1** | `https://example.com/callback` (not actually used for device auth flow, but required) |

4. Enable these **scopes** (all are recommended):

| Scope | Why |
|---|---|
| `user.read` | Read your account info (country code, user ID) |
| `collection.read` | Read your "My Collection" (favorites) |
| `collection.write` | (Optional) Write to your collection |
| `search.read` | Personalized search results |
| `search.write` | (Optional) Update search history |
| `playlists.read` | List your playlists |
| `playlists.write` | (Optional) Create playlists on Tidal side |
| `entitlements.read` | Check subscription capabilities |
| `recommendations.read` | **Key scope** — access personalized recommendations, mixes, and radio |
| `playback` | **Key scope** — required to resolve stream URLs and manifests |

5. Copy your **Client ID** and **Client Secret** — you'll put these in the `.env` file.

> **Important:** The `recommendations.read` and `playback` scopes are what make the DJ/radio replacement work. Without them, you can sync playlists but can't access Tidal's curated mixes or resolve stream URLs.

---

## Prerequisites

- **Python 3.11+** with `uv` or `pip`
- **Go 1.22+**
- **Plex Media Server** with a music library and a valid Plex token
- **Tidal subscription** (HiFi or HiFi Plus for lossless)
- **ffmpeg** (optional, for any audio format conversion)

---

## Installation

### 1. Clone and configure

```bash
git clone <this-repo-url> plexamp-universal-intg
cd plexamp-universal-intg

# Create your .env from the template
cp .env.example .env
```

Edit `.env` and fill in:
- `TIDAL_CLIENT_ID` and `TIDAL_CLIENT_SECRET` from your Tidal Developer Dashboard
- `PLEX_URL` (usually `http://localhost:32400` or your server's LAN IP)
- `PLEX_TOKEN` (see [Finding your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/))
- `PLEX_MUSIC_LIBRARY` (the name of your Plex music library section, usually `Music`)

### 2. Install and start the Python tidal-bridge

```bash
cd tidal-bridge

# Using uv (recommended)
uv venv
uv pip install -e ".[dev]"

# Or using pip
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"

# Start the service
uvicorn server.app:app --host 127.0.0.1 --port 9120 --reload
```

The bridge will start and attempt to load existing tokens from `./data/tidal_tokens.json`. If no tokens exist, the service starts but reports `authenticated: false`.

### 3. Authenticate with Tidal

Open a browser or use curl to initiate the device auth flow:

```bash
# Start device authorization
curl -X POST http://127.0.0.1:9120/auth/login
```

You'll get a response like:
```json
{
  "user_code": "ABCD1234",
  "verification_uri": "https://link.tidal.com/AAAAA",
  "expires_in": 300,
  "message": "Visit https://link.tidal.com/AAAAA and enter code: ABCD1234"
}
```

1. Open the `verification_uri` in your browser
2. Log in with your Tidal account
3. Enter the `user_code`
4. Then poll for completion:

```bash
curl -X POST "http://127.0.0.1:9120/auth/poll?device_code=<device_code_from_login>&timeout=120"
```

Once authorized, tokens are persisted and the bridge is fully operational.

### 4. Verify the bridge is working

```bash
# Health check
curl http://127.0.0.1:9120/health

# Your session info
curl http://127.0.0.1:9120/session

# Your favorite track count
curl http://127.0.0.1:9120/favorites/ids

# Your playlists
curl http://127.0.0.1:9120/playlists
```

### 5. Build and start the Go plex-sync service

```bash
cd plex-sync

# Download dependencies
go mod tidy

# Build
go build -o plex-sync ./cmd/main.go

# Run (reads config from ../.env)
./plex-sync

# Or run a single sync pass and exit
./plex-sync --once

# Or just check that config loads correctly
./plex-sync --check-config
```

The sync service will:
1. Connect to the tidal-bridge at `http://127.0.0.1:9120`
2. Verify it's authenticated
3. Find your Plex music library section
4. Sync favorites, playlists, and mixes into Plex playlists
5. Enter a periodic loop (default: every 30 minutes)

---

## Configuration Reference

All configuration is done through the `.env` file and optional `config.yaml`. Environment variables always take precedence.

### Tidal Settings

| Variable | Default | Description |
|---|---|---|
| `TIDAL_CLIENT_ID` | (required) | Your Tidal developer app client ID |
| `TIDAL_CLIENT_SECRET` | (required) | Your Tidal developer app client secret |
| `TIDAL_REDIRECT_URI` | `https://example.com/callback` | OAuth redirect URI (must match dashboard config) |
| `TIDAL_COUNTRY_CODE` | `US` | ISO 3166-1 alpha-2 country code |
| `TIDAL_AUDIO_QUALITY` | `LOSSLESS` | Audio quality: `LOW`, `HIGH`, `LOSSLESS`, `HI_RES`, `HI_RES_LOSSLESS` |
| `TIDAL_CACHE_ENABLED` | `true` | Enable HTTP response caching for Tidal API |
| `TIDAL_CACHE_TTL` | `3600` | Cache TTL in seconds |

### Plex Settings

| Variable | Default | Description |
|---|---|---|
| `PLEX_URL` | `http://localhost:32400` | Plex Media Server URL |
| `PLEX_TOKEN` | (required) | Plex authentication token |
| `PLEX_MUSIC_LIBRARY` | `Music` | Name of your music library section |
| `PLEX_CLIENT_ID` | `plexamp-tidal-bridge` | Client identifier (shown in Plex dashboard) |
| `PLEX_PRODUCT_NAME` | `PlexAmp Tidal Bridge` | Product name shown in Plex dashboard |

### Bridge Service Settings

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_HOST` | `127.0.0.1` | Host for the Python tidal-bridge REST API |
| `BRIDGE_PORT` | `9120` | Port for the tidal-bridge |
| `SYNC_HOST` | `127.0.0.1` | Host for the Go plex-sync service |
| `SYNC_PORT` | `9121` | Port for plex-sync |

### Sync Behavior

| Variable | Default | Description |
|---|---|---|
| `SYNC_INTERVAL_MINUTES` | `30` | How often to poll Tidal for changes |
| `SYNC_FAVORITES` | `true` | Sync Tidal favorites as a Plex playlist |
| `SYNC_FAVORITES_PLAYLIST_NAME` | `Tidal Favorites` | Name of the favorites playlist in Plex |
| `SYNC_PLAYLISTS` | `true` | Sync all Tidal playlists to Plex |
| `SYNC_PLAYLIST_PREFIX` | `[Tidal]` | Prefix for synced playlist names |
| `SYNC_MIXES` | `true` | Sync Tidal mixes as playlists (DJ/radio replacement) |
| `SYNC_MIXES_PREFIX` | `[Tidal Mix]` | Prefix for mix playlist names |
| `SYNC_RECOMMENDATIONS` | `true` | Sync Tidal recommendations as playlists |
| `SYNC_RECOMMENDATIONS_PREFIX` | `[Tidal Radio]` | Prefix for recommendation playlists |
| `SYNC_DELETE_ORPHANED` | `false` | Delete Plex playlists that no longer exist on Tidal |
| `SYNC_MAX_TRACKS_PER_PLAYLIST` | `0` | Max tracks per playlist (0 = unlimited) |

---

## How DJ / Sonic Analysis Replacement Works

PlexAmp has features called "Sonic Analysis" and "PlexAmp DJ" that analyze your music library and generate smart playlists and radio stations. These features historically required either:
- Plex's built-in audio analysis (which is CPU-intensive and limited)
- The now-defunct Tidal integration
- Third-party AI/LLM analysis (what MediaSage does)

**We replace these with Tidal's native curation engine:**

### Tidal Mixes → Plex Playlists

Every artist and track on Tidal has associated "mixes" — curated playlists generated by Tidal's recommendation engine. These are accessible via the Tidal API:

- **Artist mixes** (`ARTIST_MIX`) — "If you like this artist, you'll also like these tracks"
- **Track mixes** (`TRACK_MIX`) — "Songs similar to this track"
- **Discovery mixes** — Personalized new music recommendations
- **My mixes** — Curated playlists based on your listening history

The `tidal-bridge` fetches these mixes through the standard Tidal API (using the `recommendations.read` scope) and the `plex-sync` service creates corresponding Plex playlists. The result:

- Open PlexAmp → browse your playlists → see `[Tidal Mix] Artist Radio: Radiohead`
- That playlist contains tracks curated by Tidal's recommendation engine for that artist
- No AI API key needed. No compute cost. Just your existing Tidal subscription at work.

### Discovering Mix IDs

Mix IDs are embedded in track and artist metadata from the Tidal API:

```bash
# Get an artist's available mixes
curl http://127.0.0.1:9120/artists/3529977/mixes
# Response: {"artist_id": 3529977, "mixes": {"ARTIST_MIX": "abc123def"}}

# Export that mix as a playlist for Plex
curl http://127.0.0.1:9120/mixes/abc123def/export
```

The sync engine automatically discovers mixes for your favorite artists and syncs them.

---

## Bridge REST API Reference

The Python `tidal-bridge` exposes these endpoints at `http://127.0.0.1:9120`:

### Health & Auth

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health + auth status |
| `GET` | `/auth/status` | Detailed token validity info |
| `POST` | `/auth/login` | Initiate device auth flow |
| `POST` | `/auth/poll?device_code=...` | Poll for auth completion |
| `POST` | `/auth/refresh` | Force token refresh |
| `POST` | `/auth/logout` | Revoke tokens |

### Session

| Method | Path | Description |
|---|---|---|
| `GET` | `/session` | Tidal session info (user ID, country) |

### Favorites / Collection

| Method | Path | Description |
|---|---|---|
| `GET` | `/favorites/ids` | All favorite item IDs (lightweight) |
| `GET` | `/favorites/tracks?limit=50&offset=0` | Paginated favorite tracks |
| `GET` | `/favorites/tracks/all` | All favorite tracks (auto-paginated) |
| `GET` | `/favorites/albums?limit=50&offset=0` | Paginated favorite albums |
| `GET` | `/favorites/export?playlist_name=...` | Favorites as NormalizedPlaylist |

### Playlists

| Method | Path | Description |
|---|---|---|
| `GET` | `/playlists` | All user playlists |
| `GET` | `/playlists/{uuid}` | Playlist metadata |
| `GET` | `/playlists/{uuid}/tracks` | All tracks in a playlist |
| `GET` | `/playlists/{uuid}/export` | Playlist as NormalizedPlaylist |
| `GET` | `/playlists/export/all` | All playlists as NormalizedPlaylist[] |

### Albums & Artists

| Method | Path | Description |
|---|---|---|
| `GET` | `/albums/{id}` | Album metadata |
| `GET` | `/albums/{id}/tracks` | All tracks in an album |
| `GET` | `/artists/{id}` | Artist metadata |
| `GET` | `/artists/{id}/albums` | All albums by an artist |
| `GET` | `/artists/{id}/mixes` | Available mix IDs for artist radio |

### Tracks & Streams

| Method | Path | Description |
|---|---|---|
| `GET` | `/tracks/{id}` | Track metadata |
| `GET` | `/tracks/{id}/stream?quality=LOSSLESS` | Stream URL + audio info |
| `GET` | `/tracks/{id}/lyrics` | Track lyrics |

### Mixes (DJ/Radio Replacement)

| Method | Path | Description |
|---|---|---|
| `GET` | `/mixes/{id}/tracks` | All tracks in a mix |
| `GET` | `/mixes/{id}/export?playlist_name=...` | Mix as NormalizedPlaylist |

### Search

| Method | Path | Description |
|---|---|---|
| `GET` | `/search?q=...&limit=25` | Search across Tidal |

### Bulk Sync

| Method | Path | Description |
|---|---|---|
| `GET` | `/sync/snapshot` | Full export snapshot for plex-sync |

---

## Project Structure

```
plexamp-universal-intg/
├── .env.example                    # Environment variable template
├── readme.md                       # This file
│
├── tidal-bridge/                   # Python service (Tidal API client + REST server)
│   ├── pyproject.toml              # Python project config + dependencies
│   ├── core/                       # Core library (forked from tiddl)
│   │   ├── auth/                   # OAuth2 authentication
│   │   │   ├── client.py           # TidalAuthClient — device auth, token refresh
│   │   │   └── models.py           # Token, session, scope models
│   │   ├── api/                    # Tidal API HTTP client
│   │   │   ├── client.py           # TidalAPIClient — HTTP transport, caching, retries
│   │   │   └── tidal_api.py        # TidalAPI — high-level facade over all endpoints
│   │   └── models/                 # Pydantic models for all Tidal API resources
│   │       └── __init__.py         # Track, Album, Artist, Playlist, Stream, etc.
│   └── server/                     # FastAPI REST server
│       └── app.py                  # All REST endpoints exposed for plex-sync
│
├── plex-sync/                      # Go service (Plex sync engine)
│   ├── go.mod                      # Go module definition
│   ├── cmd/
│   │   └── main.go                 # CLI entry point (flags, startup, periodic loop)
│   └── internal/
│       ├── config/
│       │   └── config.go           # Configuration loading (.env, YAML, env vars)
│       ├── tidalclient/
│       │   └── client.go           # HTTP client for tidal-bridge REST API
│       └── sync/
│           └── engine.go           # Sync engine (Tidal → Plex playlist mapping)
│
└── data/                           # Runtime data (created automatically)
    ├── tidal_tokens.json           # Persisted OAuth2 tokens
    ├── sync_state.json             # Sync state (last sync time, playlist mapping)
    ├── tidal_plex_mappings.json    # Track ID mapping cache (Tidal → Plex)
    └── cache/                      # HTTP response cache
        └── tidal_api_cache.sqlite
```

---

## Key Design Decisions

### No hardcoded Tidal credentials

The original `tiddl` project embeds base64-encoded client credentials as a fallback. We removed that entirely. Every request uses YOUR `client_id` and `client_secret` from the `.env` file. If they're missing, the service refuses to start.

### Device authorization flow (not authorization code)

We use the OAuth2 device authorization grant (`urn:ietf:params:oauth:grant-type:device_code`) instead of the authorization code flow. This is better for a CLI/server tool because:
- No need to run a local web server for the redirect callback
- Works in headless/SSH environments
- The user just visits a URL and enters a short code

### Tidal mixes as DJ replacement

Instead of using GenAI to analyze audio features and generate playlists, we use Tidal's built-in mix/recommendation system. Tidal already does this analysis on their servers and exposes it through their API. The `mixes/{mix_id}/items` endpoint returns curated track lists that serve the same purpose as PlexAmp's Sonic Analysis — but powered by Tidal's own engine using your listening history.

### Separation of Python and Go

This could theoretically be a single service, but splitting it has real benefits:
- The Tidal API client (Python) can be debugged and tested independently
- The Plex sync engine (Go) can be debugged and tested independently
- If the Tidal API changes, only the Python side needs updating
- If the plexgo SDK updates, only the Go side needs updating
- You can restart one service without affecting the other

### Track matching strategy

Matching a Tidal track to a Plex library item is non-trivial. We use a three-tier strategy:
1. **Mapping cache** — if we've matched this Tidal ID before, use the cached Plex rating key (instant, no API call)
2. **ISRC match** — if the Tidal track has an ISRC code and the Plex library indexes ISRCs, this is the most reliable match
3. **Title + artist fuzzy match** — search the Plex library by track title and artist name, then compare results

The mapping cache is persisted to disk, so repeated
 syncs get faster over time.

---

## Relevant External Links

### Plex APIs
- [Plex Media Server API Docs](https://developer.plex.tv/pms/index.html)
- [Plex Media Providers API](https://developer.plex.tv/pms/index.html#section/API-Info/Media-Providers)
- [POST Media Providers](https://developer.plex.tv/pms/index.html#tag/Provider/operation/postMediaProviders)
- [Plex Claim Token](https://www.plex.tv/claim/)
- [Finding Your Plex Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- [plexgo Go SDK (v0.28.2)](https://github.com/LukeHagar/plexgo/releases/tag/v0.28.2)

### Tidal APIs
- [Tidal Developer Dashboard](https://developer.tidal.com/dashboard)
- [Tidal API Reference](https://tidal-music.github.io/tidal-api-reference/)
- [Track Manifests Endpoint](https://tidal-music.github.io/tidal-api-reference/#/trackManifests/get_trackManifests__id_)

### Reference Projects
- [tiddl — Python Tidal Downloader (our Python-side foundation)](https://github.com/oskvr37/tiddl)
- [MediaSage — AI Playlist Generator for Plex](https://github.com/ecwilsonaz/mediasage)
- [MediaSage Forum Thread](https://forums.plex.tv/t/mediasage-library-aware-ai-playlist-generator-for-plex-music/936064)
- [HiFi — Go Subsonic/Tidal Proxy](https://github.com/sachinsenal0x64/hifi)
- [Playlist Radio](https://github.com/cMf94Mfs94/playlistradio)

---

## Current Status

> **🚧 Early Development**

### What's built
- [x] Python `tidal-bridge` — full OAuth2 auth client with device flow
- [x] Python `tidal-bridge` — Tidal API client with caching, retries, token refresh
- [x] Python `tidal-bridge` — High-level TidalAPI facade (albums, artists, tracks, playlists, mixes, favorites, search, streams)
- [x] Python `tidal-bridge` — FastAPI REST server with all endpoints
- [x] Python `tidal-bridge` — Pydantic models for all Tidal API resources
- [x] Python `tidal-bridge` — NormalizedPlaylist/NormalizedTrack export models for Plex sync
- [x] Go `plex-sync` — Configuration system (.env + YAML + env var overrides)
- [x] Go `plex-sync` — HTTP client for tidal-bridge REST API with retries
- [x] Go `plex-sync` — Sync engine scaffolding (fetch → match → create/update playlists)
- [x] Go `plex-sync` — Mapping cache (Tidal ID → Plex rating key persistence)
- [x] Go `plex-sync` — CLI with periodic loop, graceful shutdown, --once mode

### What needs work
- [ ] Go `plex-sync` — Refine plexgo search/library interaction for track matching
- [ ] Go `plex-sync` — Implement Plex playlist create/update via plexgo SDK
- [ ] Go `plex-sync` — ISRC-based track matching in Plex
- [ ] Go `plex-sync` — Orphaned playlist cleanup
- [ ] Testing — Integration tests for both services
- [ ] Docker — Compose file for running both services together
- [ ] Documentation — Troubleshooting guide for common issues (no Plex token, no Tidal auth, track match failures)

---

## Contributing

This is a personal project born from frustration with losing Tidal integration in PlexAmp. Contributions are welcome, especially for:

- Improving the track matching algorithm (the hardest part of the whole system)
- Adding Docker support
- Testing with different Plex server configurations
- Testing with different Tidal regions/subscriptions

---

## License

Apache License 2.0 — same as the tiddl project this forks from.

---

## Tidal Developer App Details

For reference, here's the Tidal developer app configuration this project was built against:

```
Dashboard: https://developer.tidal.com/dashboard/e3613480-eb98-4b75-af05-ebb9e6bf1e30

Name: PlexAmp Tidal Bridge
Description: A plugin for Plex that allows you to play Tidal music.
Platform preset: WEB
Redirect URI 1: https://example.com/callback

Scopes (all enabled):
  user.read            — Read user account info (country, email)
  collection.read      — Read "My Collection"
  collection.write     — Write to "My Collection"
  search.read          — Personalized search results
  search.write         — Update search history
  playlists.read       — List user playlists
  playlists.write      — Write to user playlists
  entitlements.read    — Read subscription capabilities
  recommendations.read — Read personalized recommendations (KEY for DJ/radio)
  playback             — Play media content / resolve stream URLs (KEY for streams)
```
