# 🎬 arrstack-mcp

An [MCP](https://modelcontextprotocol.io/) server that gives AI assistants control over your **Sonarr**, **Radarr**, **qBittorrent**, and **Jellyfin** homelab media stack.

Works with **Claude Desktop**, **Cursor**, **VS Code Copilot**, **OpenClaw**, and any other MCP-compatible client.

## Features

| Service | Tools |
|---------|-------|
| **Sonarr** | List series, search & add shows, upcoming episodes, download queue |
| **Radarr** | List movies, search & add movies, download queue |
| **qBittorrent** | List/pause/resume/delete torrents, add magnets, transfer stats |
| **Jellyfin** | List libraries, recent additions, system info |

Only configure the services you use — unconfigured services are gracefully skipped.

## Quick Start

### Option 1: Claude Desktop / Cursor / VS Code (stdio)

1. Install dependencies:

   ```bash
   pip install "mcp[cli]>=1.9.0" httpx
   ```

2. Add to your MCP client config (e.g. `claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "arrstack": {
         "command": "python",
         "args": ["/path/to/arrstack-mcp/server.py"],
         "env": {
           "SONARR_URL": "http://localhost:8989",
           "SONARR_API_KEY": "your-api-key",
           "RADARR_URL": "http://localhost:7878",
           "RADARR_API_KEY": "your-api-key",
           "QBT_URL": "http://localhost:8080",
           "QBT_USER": "admin",
           "QBT_PASS": "your-password",
           "JELLYFIN_URL": "http://localhost:8096"
         }
       }
     }
   }
   ```

3. Restart your MCP client. Done!

### Option 2: Docker (HTTP transport)

For remote setups or when running alongside your *arr stack:

```bash
git clone https://github.com/ct4nk3r/arrstack-mcp.git
cd arrstack-mcp
cp .env.example .env
# Edit .env with your service URLs and API keys
docker compose up -d
```

The server runs on port `8000` with Streamable HTTP transport.

#### Connect to OpenClaw

```bash
openclaw mcp set arrstack '{"url":"http://arrstack-mcp:8000/mcp","transport":"streamable-http"}'
```

#### Connect to other HTTP MCP clients

Point your client to `http://<host>:8000/mcp` using Streamable HTTP transport.

### Option 3: Docker on the same network as your *arr stack

If your media services run in Docker, add `arrstack-mcp` to the same network:

```yaml
services:
  arrstack-mcp:
    build: .
    container_name: arrstack-mcp
    ports:
      - "8000:8000"
    environment:
      - SONARR_URL=http://sonarr:8989
      - SONARR_API_KEY=your-key
      - RADARR_URL=http://radarr:7878
      - RADARR_API_KEY=your-key
      - QBT_URL=http://qbittorrent:8080
      - QBT_USER=admin
      - QBT_PASS=your-password
      - JELLYFIN_URL=http://jellyfin:8096
    networks:
      - your-media-network
```

## Configuration

All configuration is done via environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `SONARR_URL` | No | Sonarr base URL (e.g. `http://localhost:8989`) |
| `SONARR_API_KEY` | If Sonarr | Sonarr API key (Settings → General) |
| `RADARR_URL` | No | Radarr base URL (e.g. `http://localhost:7878`) |
| `RADARR_API_KEY` | If Radarr | Radarr API key (Settings → General) |
| `QBT_URL` | No | qBittorrent Web UI URL (e.g. `http://localhost:8080`) |
| `QBT_USER` | If qBt | qBittorrent username (default: `admin`) |
| `QBT_PASS` | If qBt | qBittorrent password |
| `JELLYFIN_URL` | No | Jellyfin base URL (e.g. `http://localhost:8096`) |
| `JELLYFIN_API_KEY` | No | Jellyfin API key (optional, for authenticated endpoints) |

## Available Tools

### Sonarr (TV Shows)

| Tool | Description |
|------|-------------|
| `sonarr_list_series` | List all series with episode counts and disk usage |
| `sonarr_get_series` | Get detailed info about a specific series |
| `sonarr_search` | Search for new shows to add |
| `sonarr_add_series` | Add a show by TVDB ID |
| `sonarr_upcoming` | Show upcoming episodes |
| `sonarr_queue` | Show current download queue |

### Radarr (Movies)

| Tool | Description |
|------|-------------|
| `radarr_list_movies` | List all movies with download status |
| `radarr_get_movie` | Get detailed info about a specific movie |
| `radarr_search` | Search for new movies to add |
| `radarr_add_movie` | Add a movie by TMDB ID |
| `radarr_queue` | Show current download queue |

### qBittorrent (Downloads)

| Tool | Description |
|------|-------------|
| `qbt_list_torrents` | List torrents with progress and speed |
| `qbt_torrent_details` | Get detailed torrent info |
| `qbt_add_magnet` | Add a magnet link |
| `qbt_pause` | Pause a torrent |
| `qbt_resume` | Resume a torrent |
| `qbt_delete` | Delete a torrent (optionally with files) |
| `qbt_transfer_info` | Global transfer statistics |

### Jellyfin (Media Server)

| Tool | Description |
|------|-------------|
| `jellyfin_libraries` | List media libraries |
| `jellyfin_recent` | Recently added items |
| `jellyfin_system_info` | Server version and system info |

## Transport Options

```bash
# stdio (default) — for Claude Desktop, Cursor, VS Code
python server.py

# Streamable HTTP — for Docker / remote
python server.py --transport streamable-http --port 8000

# SSE — legacy HTTP transport
python server.py --transport sse --port 8000
```

## Finding Your API Keys

- **Sonarr**: Settings → General → API Key
- **Radarr**: Settings → General → API Key
- **qBittorrent**: Settings → Web UI → Authentication
- **Jellyfin**: Dashboard → API Keys → Add

## License

MIT
