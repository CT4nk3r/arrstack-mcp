"""
arrstack-mcp — MCP server for Sonarr, Radarr, Prowlarr, qBittorrent & Jellyfin.

Exposes your *arr media stack as MCP tools so any AI assistant
(Claude Desktop, Cursor, VS Code Copilot, OpenClaw, etc.) can
search, add, and manage your media library.
"""

import os
import sys
import argparse
import logging
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger("arrstack-mcp")

# ── Configuration ──

SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
QBT_URL = os.environ.get("QBT_URL", "").rstrip("/")
QBT_USER = os.environ.get("QBT_USER", "admin")
QBT_PASS = os.environ.get("QBT_PASS", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

mcp = FastMCP(
    "arrstack",
    instructions=(
        "Homelab media stack tools for Sonarr (TV), Radarr (Movies), "
        "Prowlarr (Indexers), qBittorrent (Downloads), and Jellyfin (Streaming). "
        "Use these tools to search, add, and manage media."
    ),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# ── HTTP helpers ──


def _sonarr(path: str, method: str = "GET", json=None):
    if not SONARR_URL:
        return "Sonarr is not configured. Set SONARR_URL and SONARR_API_KEY."
    r = httpx.request(
        method,
        f"{SONARR_URL}/api/v3{path}",
        headers={"X-Api-Key": SONARR_API_KEY},
        json=json,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _radarr(path: str, method: str = "GET", json=None):
    if not RADARR_URL:
        return "Radarr is not configured. Set RADARR_URL and RADARR_API_KEY."
    r = httpx.request(
        method,
        f"{RADARR_URL}/api/v3{path}",
        headers={"X-Api-Key": RADARR_API_KEY},
        json=json,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


_qbt_sid = None


def _qbt(path: str, method: str = "GET", data=None):
    global _qbt_sid
    if not QBT_URL:
        return "qBittorrent is not configured. Set QBT_URL and QBT_PASS."
    if not _qbt_sid:
        r = httpx.post(
            f"{QBT_URL}/api/v2/auth/login",
            data={"username": QBT_USER, "password": QBT_PASS},
            timeout=10,
        )
        _qbt_sid = r.cookies.get("SID")
    r = httpx.request(
        method, f"{QBT_URL}/api/v2{path}", cookies={"SID": _qbt_sid}, data=data, timeout=30
    )
    if r.status_code == 403:
        _qbt_sid = None
        return _qbt(path, method, data)
    try:
        return r.json()
    except Exception:
        return r.text


def _jellyfin(path: str):
    if not JELLYFIN_URL:
        return "Jellyfin is not configured. Set JELLYFIN_URL."
    headers = {}
    if JELLYFIN_API_KEY:
        headers["X-Emby-Token"] = JELLYFIN_API_KEY
    r = httpx.get(f"{JELLYFIN_URL}{path}", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def _prowlarr(path: str, method: str = "GET", json=None):
    if not PROWLARR_URL:
        return "Prowlarr is not configured. Set PROWLARR_URL and PROWLARR_API_KEY."
    r = httpx.request(
        method,
        f"{PROWLARR_URL}/api/v1{path}",
        headers={"X-Api-Key": PROWLARR_API_KEY},
        json=json,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ════════════════════════════════════════════════════════════════
#  Sonarr Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def sonarr_list_series() -> str:
    """List all TV series in Sonarr with monitoring status, episode counts, and disk usage."""
    data = _sonarr("/series")
    if isinstance(data, str):
        return data
    lines = []
    for s in sorted(data, key=lambda x: x["title"]):
        stats = s.get("statistics", {})
        have = stats.get("episodeFileCount", 0)
        total = stats.get("episodeCount", 0)
        size_gb = stats.get("sizeOnDisk", 0) / 1e9
        icon = "✅" if s.get("monitored") else "⏸️"
        lines.append(
            f"{icon} {s['title']} ({s.get('year', '?')}) — "
            f"{have}/{total} episodes, {size_gb:.1f} GB"
        )
    return "\n".join(lines) or "No series found."


@mcp.tool()
def sonarr_get_series(series_id: int) -> str:
    """Get detailed info about a specific TV series by its Sonarr ID."""
    s = _sonarr(f"/series/{series_id}")
    if isinstance(s, str):
        return s
    stats = s.get("statistics", {})
    lines = [
        f"Title: {s['title']} ({s.get('year', '?')})",
        f"Status: {s.get('status', '?')}",
        f"Network: {s.get('network', '?')}",
        f"Monitored: {s.get('monitored', False)}",
        f"Seasons: {stats.get('seasonCount', '?')}",
        f"Episodes: {stats.get('episodeFileCount', 0)}/{stats.get('episodeCount', 0)}",
        f"Size: {stats.get('sizeOnDisk', 0) / 1e9:.1f} GB",
        f"Path: {s.get('path', '?')}",
        f"Overview: {(s.get('overview') or 'N/A')[:300]}",
    ]
    return "\n".join(lines)


@mcp.tool()
def sonarr_search(term: str) -> str:
    """Search for a TV series to add to Sonarr. Returns title, year, TVDB ID, and overview."""
    data = _sonarr(f"/series/lookup?term={term}")
    if isinstance(data, str):
        return data
    lines = []
    for r in data[:10]:
        overview = (r.get("overview") or "No description.")[:150]
        lines.append(
            f"• {r['title']} ({r.get('year', '?')}) "
            f"[tvdbId: {r.get('tvdbId', '?')}]\n  {overview}"
        )
    return "\n".join(lines) or "No results found."


@mcp.tool()
def sonarr_add_series(
    tvdb_id: int, quality_profile_id: int = 1, monitor: str = "all"
) -> str:
    """Add a TV series to Sonarr by its TVDB ID. Use sonarr_search to find the TVDB ID first.

    Args:
        tvdb_id: The TVDB ID of the series.
        quality_profile_id: Quality profile to use (default: 1).
        monitor: Episodes to monitor — "all", "future", "missing", "pilot", "none".
    """
    lookup = _sonarr(f"/series/lookup?term=tvdb:{tvdb_id}")
    if isinstance(lookup, str):
        return lookup
    if not lookup:
        return "Series not found for that TVDB ID."
    series_data = lookup[0]
    root = _sonarr("/rootfolder")
    root_path = root[0]["path"] if isinstance(root, list) and root else "/tv"
    series_data.update(
        {
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "addOptions": {"monitor": monitor, "searchForMissingEpisodes": True},
        }
    )
    result = _sonarr("/series", method="POST", json=series_data)
    if isinstance(result, dict):
        return f"✅ Added: {result['title']} ({result.get('year', '?')})"
    return str(result)


@mcp.tool()
def sonarr_upcoming(days: int = 7) -> str:
    """Show upcoming TV episodes within the next N days.

    Args:
        days: Number of days to look ahead (default: 7).
    """
    from datetime import datetime, timedelta

    start = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    data = _sonarr(f"/calendar?start={start}&end={end}")
    if isinstance(data, str):
        return data
    lines = []
    for ep in data:
        series = ep.get("series", {}).get("title", "?")
        s_num = ep.get("seasonNumber", 0)
        e_num = ep.get("episodeNumber", 0)
        air = ep.get("airDateUtc", "?")[:10]
        title = ep.get("title", "")
        lines.append(f"• {series} S{s_num:02d}E{e_num:02d} \"{title}\" — {air}")
    return "\n".join(lines) or "Nothing upcoming."


@mcp.tool()
def sonarr_list_quality_profiles() -> str:
    """List all quality profiles in Sonarr with their allowed qualities."""
    data = _sonarr("/qualityprofile")
    if isinstance(data, str):
        return data
    lines = []
    for p in data:
        qualities = [
            q.get("quality", q).get("name", "?")
            for q in p.get("items", [])
            if q.get("allowed")
        ]
        lines.append(
            f"• [{p['id']}] {p['name']} — "
            f"Cutoff: {p.get('cutoff', {}).get('name', '?') if isinstance(p.get('cutoff'), dict) else p.get('cutoffFormatScore', '?')}\n"
            f"  Allowed: {', '.join(qualities) or 'none'}"
        )
    return "\n".join(lines) or "No quality profiles found."


@mcp.tool()
def sonarr_get_quality_definitions() -> str:
    """Get quality size limits (min/max MB per minute) for each quality tier in Sonarr.
    These limits control what file sizes Sonarr will accept for downloads."""
    data = _sonarr("/qualitydefinition")
    if isinstance(data, str):
        return data
    lines = []
    for d in data:
        name = d.get("quality", {}).get("name", "?")
        qid = d.get("quality", {}).get("id", "?")
        min_size = d.get("minSize", 0)
        max_size = d.get("maxSize", 0)
        pref_size = d.get("preferredSize", 0)
        max_str = f"{max_size:.1f}" if max_size else "unlimited"
        pref_str = f"{pref_size:.1f}" if pref_size else "unlimited"
        lines.append(
            f"• [{qid}] {name} — "
            f"min: {min_size:.1f}, preferred: {pref_str}, max: {max_str} MB/min"
        )
    return "\n".join(lines) or "No quality definitions found."


@mcp.tool()
def sonarr_set_quality_definition(
    quality_id: int, min_size: float = -1, preferred_size: float = -1, max_size: float = -1
) -> str:
    """Set the min/preferred/max file size (in MB per minute of runtime) for a Sonarr quality tier.

    Use sonarr_get_quality_definitions to see current values and quality IDs.
    For a ~45min episode, 5 MB/min ≈ 225 MB, 10 MB/min ≈ 450 MB.
    Set to 0 for unlimited (max/preferred only). Pass -1 to leave unchanged.

    Args:
        quality_id: Quality ID from sonarr_get_quality_definitions.
        min_size: Minimum MB per minute (-1 to keep current).
        preferred_size: Preferred MB per minute (-1 to keep current, 0 for unlimited).
        max_size: Maximum MB per minute (-1 to keep current, 0 for unlimited).
    """
    defs = _sonarr("/qualitydefinition")
    if isinstance(defs, str):
        return defs
    target = None
    for d in defs:
        if d.get("quality", {}).get("id") == quality_id:
            target = d
            break
    if not target:
        return f"Quality ID {quality_id} not found. Use sonarr_get_quality_definitions to list IDs."
    if min_size >= 0:
        target["minSize"] = min_size
    if preferred_size >= 0:
        target["preferredSize"] = preferred_size
    if max_size >= 0:
        target["maxSize"] = max_size
    result = _sonarr(f"/qualitydefinition/{target['id']}", method="PUT", json=target)
    if isinstance(result, dict):
        name = result.get("quality", {}).get("name", "?")
        return (
            f"✅ Updated {name}: min={result.get('minSize', 0):.1f}, "
            f"preferred={result.get('preferredSize', 0):.1f}, "
            f"max={result.get('maxSize', 0):.1f} MB/min"
        )
    return str(result)


@mcp.tool()
def sonarr_list_custom_formats() -> str:
    """List all custom formats in Sonarr with their specifications."""
    data = _sonarr("/customformat")
    if isinstance(data, str):
        return data
    if not data:
        return "No custom formats configured."
    lines = []
    for cf in data:
        specs = [s.get("name", "?") for s in cf.get("specifications", [])]
        lines.append(
            f"• [{cf['id']}] {cf['name']}\n"
            f"  Specs: {', '.join(specs) or 'none'}"
        )
    return "\n".join(lines)


@mcp.tool()
def sonarr_queue() -> str:
    """Show the current Sonarr download queue with status and queue IDs for each item."""
    data = _sonarr("/queue?pageSize=50&includeUnknownSeriesItems=true")
    if isinstance(data, str):
        return data
    records = data.get("records", [])
    lines = []
    for r in records:
        title = r.get("title", "?")
        status = r.get("status", "?")
        sizeleft = r.get("sizeleft", 0) / 1e9
        lines.append(f"• [queueId: {r['id']}] {title} — {status} ({sizeleft:.1f} GB remaining)")
    return "\n".join(lines) or "Queue is empty."


@mcp.tool()
def sonarr_delete_queue_item(queue_id: int, blocklist: bool = True) -> str:
    """Remove an item from the Sonarr download queue.

    Args:
        queue_id: Queue item ID (use sonarr_queue to find it).
        blocklist: If True, adds the release to the blocklist so it won't be grabbed again.
    """
    try:
        r = httpx.delete(
            f"{SONARR_URL}/api/v3/queue/{queue_id}",
            headers={"X-Api-Key": SONARR_API_KEY},
            params={"removeFromClient": "true", "blocklist": str(blocklist).lower()},
            timeout=30,
        )
        r.raise_for_status()
        return f"✅ Removed from queue." + (" (blocklisted)" if blocklist else "")
    except httpx.HTTPStatusError as e:
        return f"❌ Failed: {e.response.status_code} — {e.response.text[:200]}"


@mcp.tool()
def sonarr_delete_episode_file(episode_file_id: int) -> str:
    """Delete a downloaded episode file, marking it as missing in Sonarr.
    This allows Sonarr to re-search and download a new version.

    Args:
        episode_file_id: Episode file ID (use sonarr_get_series to find file IDs).
    """
    try:
        r = httpx.delete(
            f"{SONARR_URL}/api/v3/episodefile/{episode_file_id}",
            headers={"X-Api-Key": SONARR_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        return f"✅ Deleted episode file (id: {episode_file_id}). Episode is now marked as missing."
    except httpx.HTTPStatusError as e:
        return f"❌ Failed: {e.response.status_code} — {e.response.text[:200]}"


@mcp.tool()
def sonarr_search_missing(series_id: int = 0) -> str:
    """Trigger a search for missing episodes in Sonarr.

    Args:
        series_id: Sonarr series ID to search. Set to 0 to search ALL series with missing episodes.
    """
    if series_id:
        result = _sonarr("/command", method="POST", json={"name": "SeriesSearch", "seriesId": series_id})
    else:
        result = _sonarr("/command", method="POST", json={"name": "MissingEpisodeSearch"})
    if isinstance(result, dict):
        scope = f"series {series_id}" if series_id else "all missing episodes"
        return f"🔍 Search triggered for {scope}."
    return str(result)


# ════════════════════════════════════════════════════════════════
#  Radarr Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def radarr_list_movies() -> str:
    """List all movies in Radarr with download status and disk usage."""
    data = _radarr("/movie")
    if isinstance(data, str):
        return data
    lines = []
    for m in sorted(data, key=lambda x: x["title"]):
        has_file = "✅" if m.get("hasFile") else "❌"
        monitored = "👁" if m.get("monitored") else "⏸️"
        size_gb = m.get("sizeOnDisk", 0) / 1e9
        lines.append(
            f"{has_file}{monitored} {m['title']} ({m.get('year', '?')}) — {size_gb:.1f} GB"
        )
    return "\n".join(lines) or "No movies found."


@mcp.tool()
def radarr_get_movie(movie_id: int) -> str:
    """Get detailed info about a specific movie by its Radarr ID."""
    m = _radarr(f"/movie/{movie_id}")
    if isinstance(m, str):
        return m
    lines = [
        f"Title: {m['title']} ({m.get('year', '?')})",
        f"Status: {m.get('status', '?')}",
        f"Studio: {m.get('studio', '?')}",
        f"Has File: {m.get('hasFile', False)}",
        f"Monitored: {m.get('monitored', False)}",
        f"Size: {m.get('sizeOnDisk', 0) / 1e9:.1f} GB",
        f"Path: {m.get('path', '?')}",
        f"TMDB: {m.get('tmdbId', '?')} | IMDB: {m.get('imdbId', '?')}",
        f"Overview: {(m.get('overview') or 'N/A')[:300]}",
    ]
    return "\n".join(lines)


@mcp.tool()
def radarr_search(term: str) -> str:
    """Search for a movie to add to Radarr. Returns title, year, TMDB ID, and overview."""
    data = _radarr(f"/movie/lookup?term={term}")
    if isinstance(data, str):
        return data
    lines = []
    for r in data[:10]:
        overview = (r.get("overview") or "No description.")[:150]
        lines.append(
            f"• {r['title']} ({r.get('year', '?')}) "
            f"[tmdbId: {r.get('tmdbId', '?')}]\n  {overview}"
        )
    return "\n".join(lines) or "No results found."


@mcp.tool()
def radarr_add_movie(tmdb_id: int, quality_profile_id: int = 1) -> str:
    """Add a movie to Radarr by its TMDB ID. Use radarr_search to find the TMDB ID first.

    Args:
        tmdb_id: The TMDB ID of the movie.
        quality_profile_id: Quality profile to use (default: 1).
    """
    lookup = _radarr(f"/movie/lookup/tmdb?tmdbId={tmdb_id}")
    if isinstance(lookup, str):
        return lookup
    movie_data = lookup if isinstance(lookup, dict) else lookup[0]
    root = _radarr("/rootfolder")
    root_path = root[0]["path"] if isinstance(root, list) and root else "/movies"
    movie_data.update(
        {
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
    )
    result = _radarr("/movie", method="POST", json=movie_data)
    if isinstance(result, dict):
        return f"✅ Added: {result['title']} ({result.get('year', '?')})"
    return str(result)


@mcp.tool()
def radarr_list_quality_profiles() -> str:
    """List all quality profiles in Radarr with their allowed qualities."""
    data = _radarr("/qualityprofile")
    if isinstance(data, str):
        return data
    lines = []
    for p in data:
        qualities = [
            q.get("quality", q).get("name", "?")
            for q in p.get("items", [])
            if q.get("allowed")
        ]
        lines.append(
            f"• [{p['id']}] {p['name']} — "
            f"Cutoff: {p.get('cutoff', {}).get('name', '?') if isinstance(p.get('cutoff'), dict) else p.get('cutoffFormatScore', '?')}\n"
            f"  Allowed: {', '.join(qualities) or 'none'}"
        )
    return "\n".join(lines) or "No quality profiles found."


@mcp.tool()
def radarr_get_quality_definitions() -> str:
    """Get quality size limits (min/max MB per minute) for each quality tier in Radarr.
    These limits control what file sizes Radarr will accept for downloads."""
    data = _radarr("/qualitydefinition")
    if isinstance(data, str):
        return data
    lines = []
    for d in data:
        name = d.get("quality", {}).get("name", "?")
        qid = d.get("quality", {}).get("id", "?")
        min_size = d.get("minSize", 0)
        max_size = d.get("maxSize", 0)
        pref_size = d.get("preferredSize", 0)
        max_str = f"{max_size:.1f}" if max_size else "unlimited"
        pref_str = f"{pref_size:.1f}" if pref_size else "unlimited"
        lines.append(
            f"• [{qid}] {name} — "
            f"min: {min_size:.1f}, preferred: {pref_str}, max: {max_str} MB/min"
        )
    return "\n".join(lines) or "No quality definitions found."


@mcp.tool()
def radarr_set_quality_definition(
    quality_id: int, min_size: float = -1, preferred_size: float = -1, max_size: float = -1
) -> str:
    """Set the min/preferred/max file size (in MB per minute of runtime) for a Radarr quality tier.

    Use radarr_get_quality_definitions to see current values and quality IDs.
    For a ~2hr movie, 40 MB/min ≈ 5 GB, 85 MB/min ≈ 10 GB.
    Set to 0 for unlimited (max/preferred only). Pass -1 to leave unchanged.

    Args:
        quality_id: Quality ID from radarr_get_quality_definitions.
        min_size: Minimum MB per minute (-1 to keep current).
        preferred_size: Preferred MB per minute (-1 to keep current, 0 for unlimited).
        max_size: Maximum MB per minute (-1 to keep current, 0 for unlimited).
    """
    defs = _radarr("/qualitydefinition")
    if isinstance(defs, str):
        return defs
    target = None
    for d in defs:
        if d.get("quality", {}).get("id") == quality_id:
            target = d
            break
    if not target:
        return f"Quality ID {quality_id} not found. Use radarr_get_quality_definitions to list IDs."
    if min_size >= 0:
        target["minSize"] = min_size
    if preferred_size >= 0:
        target["preferredSize"] = preferred_size
    if max_size >= 0:
        target["maxSize"] = max_size
    result = _radarr(f"/qualitydefinition/{target['id']}", method="PUT", json=target)
    if isinstance(result, dict):
        name = result.get("quality", {}).get("name", "?")
        return (
            f"✅ Updated {name}: min={result.get('minSize', 0):.1f}, "
            f"preferred={result.get('preferredSize', 0):.1f}, "
            f"max={result.get('maxSize', 0):.1f} MB/min"
        )
    return str(result)


@mcp.tool()
def radarr_list_custom_formats() -> str:
    """List all custom formats in Radarr with their specifications."""
    data = _radarr("/customformat")
    if isinstance(data, str):
        return data
    if not data:
        return "No custom formats configured."
    lines = []
    for cf in data:
        specs = [s.get("name", "?") for s in cf.get("specifications", [])]
        lines.append(
            f"• [{cf['id']}] {cf['name']}\n"
            f"  Specs: {', '.join(specs) or 'none'}"
        )
    return "\n".join(lines)


@mcp.tool()
def radarr_queue() -> str:
    """Show the current Radarr download queue with status and queue IDs for each item."""
    data = _radarr("/queue?pageSize=50&includeUnknownMovieItems=true")
    if isinstance(data, str):
        return data
    records = data.get("records", [])
    lines = []
    for r in records:
        title = r.get("title", "?")
        status = r.get("status", "?")
        sizeleft = r.get("sizeleft", 0) / 1e9
        lines.append(f"• [queueId: {r['id']}] {title} — {status} ({sizeleft:.1f} GB remaining)")
    return "\n".join(lines) or "Queue is empty."


@mcp.tool()
def radarr_delete_queue_item(queue_id: int, blocklist: bool = True) -> str:
    """Remove an item from the Radarr download queue.

    Args:
        queue_id: Queue item ID (use radarr_queue to find it).
        blocklist: If True, adds the release to the blocklist so it won't be grabbed again.
    """
    try:
        r = httpx.delete(
            f"{RADARR_URL}/api/v3/queue/{queue_id}",
            headers={"X-Api-Key": RADARR_API_KEY},
            params={"removeFromClient": "true", "blocklist": str(blocklist).lower()},
            timeout=30,
        )
        r.raise_for_status()
        return f"✅ Removed from queue." + (" (blocklisted)" if blocklist else "")
    except httpx.HTTPStatusError as e:
        return f"❌ Failed: {e.response.status_code} — {e.response.text[:200]}"


@mcp.tool()
def radarr_delete_movie_file(movie_id: int) -> str:
    """Delete the downloaded file for a movie, marking it as missing in Radarr.
    This allows Radarr to re-search and download a new version.

    Args:
        movie_id: Radarr movie ID (use radarr_list_movies or radarr_get_movie to find it).
    """
    movie = _radarr(f"/movie/{movie_id}")
    if isinstance(movie, str):
        return movie
    movie_file = movie.get("movieFile")
    if not movie_file:
        return f"Movie '{movie.get('title', '?')}' has no file to delete."
    fid = movie_file.get("id")
    try:
        r = httpx.delete(
            f"{RADARR_URL}/api/v3/moviefile/{fid}",
            headers={"X-Api-Key": RADARR_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        return f"✅ Deleted file for '{movie['title']}' (file id: {fid}). Movie is now marked as missing."
    except httpx.HTTPStatusError as e:
        return f"❌ Failed: {e.response.status_code} — {e.response.text[:200]}"


@mcp.tool()
def radarr_search_missing(movie_ids: str = "") -> str:
    """Trigger a search for missing movies in Radarr.

    Args:
        movie_ids: Comma-separated Radarr movie IDs to search. Leave empty to search ALL missing movies.
    """
    if movie_ids:
        ids = [int(x.strip()) for x in movie_ids.split(",")]
    else:
        movies = _radarr("/movie")
        if isinstance(movies, str):
            return movies
        ids = [m["id"] for m in movies if not m.get("hasFile")]
        if not ids:
            return "All movies have files — nothing to search."
    result = _radarr("/command", method="POST", json={"name": "MoviesSearch", "movieIds": ids})
    if isinstance(result, dict):
        return f"🔍 Search triggered for {len(ids)} movie(s)."
    return str(result)


# ════════════════════════════════════════════════════════════════
#  Prowlarr Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def prowlarr_list_indexers() -> str:
    """List all configured indexers in Prowlarr with their status and priority."""
    data = _prowlarr("/indexer")
    if isinstance(data, str):
        return data
    lines = []
    for idx in data:
        enabled = "✅" if idx.get("enable") else "❌"
        name = idx.get("name", "?")
        protocol = idx.get("protocol", "?")
        priority = idx.get("priority", "?")
        lines.append(f"{enabled} {name} ({protocol}) — priority: {priority}, id: {idx['id']}")
    return "\n".join(lines) or "No indexers configured."


@mcp.tool()
def prowlarr_test_indexer(indexer_id: int) -> str:
    """Test an indexer connection in Prowlarr. Use this to reset a failing indexer.

    Args:
        indexer_id: The indexer ID (use prowlarr_list_indexers to find it).
    """
    try:
        _prowlarr(f"/indexer/{indexer_id}/test", method="POST")
        return "✅ Indexer test passed."
    except httpx.HTTPStatusError as e:
        return f"❌ Indexer test failed: {e.response.status_code} — {e.response.text[:200]}"


@mcp.tool()
def prowlarr_test_all_indexers() -> str:
    """Test all enabled indexers in Prowlarr and report their status."""
    data = _prowlarr("/indexer")
    if isinstance(data, str):
        return data
    results = []
    for idx in data:
        if not idx.get("enable"):
            continue
        try:
            _prowlarr(f"/indexer/{idx['id']}/test", method="POST")
            results.append(f"✅ {idx['name']} — OK")
        except httpx.HTTPStatusError as e:
            results.append(f"❌ {idx['name']} — {e.response.status_code}")
    return "\n".join(results) or "No enabled indexers."


@mcp.tool()
def prowlarr_search(query: str, indexer_ids: str = "") -> str:
    """Search across Prowlarr indexers for releases.

    Args:
        query: Search term.
        indexer_ids: Comma-separated indexer IDs to search (empty = all).
    """
    params = f"/search?query={query}&type=search"
    if indexer_ids:
        params += f"&indexerIds={indexer_ids}"
    data = _prowlarr(params)
    if isinstance(data, str):
        return data
    lines = []
    for r in data[:15]:
        title = r.get("title", "?")
        size_gb = r.get("size", 0) / 1e9
        seeders = r.get("seeders", "?")
        indexer = r.get("indexer", "?")
        lines.append(f"• {title} — {size_gb:.1f} GB, {seeders} seeds [{indexer}]")
    return "\n".join(lines) or "No results."


@mcp.tool()
def prowlarr_health() -> str:
    """Check Prowlarr system health for warnings and errors."""
    data = _prowlarr("/health")
    if isinstance(data, str):
        return data
    if not data:
        return "✅ No health issues."
    lines = []
    for h in data:
        icon = "⚠️" if h.get("type") == "warning" else "❌"
        lines.append(f"{icon} {h.get('message', '?')}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  qBittorrent Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def qbt_list_torrents(filter: str = "all") -> str:
    """List torrents in qBittorrent.

    Args:
        filter: Filter torrents — "all", "downloading", "seeding", "completed", "paused", "active", "stalled".
    """
    data = _qbt(f"/torrents/info?filter={filter}")
    if isinstance(data, str):
        return data
    lines = []
    for t in data:
        progress = t.get("progress", 0) * 100
        state = t.get("state", "?")
        size_gb = t.get("size", 0) / 1e9
        name = t.get("name", "?")
        dl = t.get("dlspeed", 0) / 1e6
        up = t.get("upspeed", 0) / 1e6
        speed = ""
        if dl > 0.01:
            speed += f" ↓{dl:.1f} MB/s"
        if up > 0.01:
            speed += f" ↑{up:.1f} MB/s"
        h = t.get("hash", "?")
        lines.append(f"• [{h}] {name} — {progress:.0f}% [{state}] {size_gb:.1f} GB{speed}")
    return "\n".join(lines) or "No torrents."


@mcp.tool()
def qbt_torrent_details(torrent_hash: str) -> str:
    """Get detailed info about a specific torrent by its hash.

    Args:
        torrent_hash: The info hash of the torrent.
    """
    props = _qbt(f"/torrents/properties?hash={torrent_hash}")
    if isinstance(props, str):
        return props
    lines = [
        f"Save path: {props.get('save_path', '?')}",
        f"Total size: {props.get('total_size', 0) / 1e9:.2f} GB",
        f"Downloaded: {props.get('total_downloaded', 0) / 1e9:.2f} GB",
        f"Uploaded: {props.get('total_uploaded', 0) / 1e9:.2f} GB",
        f"Ratio: {props.get('share_ratio', 0):.2f}",
        f"Seeds: {props.get('seeds', 0)} | Peers: {props.get('peers', 0)}",
        f"Added on: {props.get('addition_date', '?')}",
        f"Comment: {props.get('comment', 'N/A')}",
    ]
    return "\n".join(lines)


@mcp.tool()
def qbt_add_magnet(magnet_url: str, category: str = "") -> str:
    """Add a magnet link to qBittorrent.

    Args:
        magnet_url: The magnet URI to add.
        category: Optional category to assign (e.g. "tv", "movies").
    """
    result = _qbt("/torrents/add", method="POST", data={"urls": magnet_url, "category": category})
    if result == "Ok.":
        return "✅ Torrent added successfully."
    return f"Result: {result}"


@mcp.tool()
def qbt_pause(torrent_hash: str) -> str:
    """Pause a torrent. Use 'all' to pause everything.

    Args:
        torrent_hash: Hash of the torrent, or "all" to pause all.
    """
    _qbt("/torrents/pause", method="POST", data={"hashes": torrent_hash})
    return "⏸️ Paused."


@mcp.tool()
def qbt_resume(torrent_hash: str) -> str:
    """Resume a torrent. Use 'all' to resume everything.

    Args:
        torrent_hash: Hash of the torrent, or "all" to resume all.
    """
    _qbt("/torrents/resume", method="POST", data={"hashes": torrent_hash})
    return "▶️ Resumed."


@mcp.tool()
def qbt_delete(torrent_hash: str, delete_files: bool = False) -> str:
    """Delete a torrent from qBittorrent.

    Args:
        torrent_hash: Hash of the torrent to delete.
        delete_files: If True, also delete downloaded files from disk.
    """
    _qbt(
        "/torrents/delete",
        method="POST",
        data={"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()},
    )
    return "🗑️ Deleted." + (" (files removed)" if delete_files else " (files kept)")


@mcp.tool()
def qbt_transfer_info() -> str:
    """Get global qBittorrent transfer statistics (speeds, totals, connection status)."""
    info = _qbt("/transfer/info")
    if isinstance(info, dict):
        return (
            f"↓ {info.get('dl_info_speed', 0) / 1e6:.1f} MB/s "
            f"(session: {info.get('dl_info_data', 0) / 1e9:.1f} GB)\n"
            f"↑ {info.get('up_info_speed', 0) / 1e6:.1f} MB/s "
            f"(session: {info.get('up_info_data', 0) / 1e9:.1f} GB)\n"
            f"Connection: {info.get('connection_status', '?')}\n"
            f"DHT nodes: {info.get('dht_nodes', 0)}"
        )
    return str(info)


# ════════════════════════════════════════════════════════════════
#  Jellyfin Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def jellyfin_libraries() -> str:
    """List all Jellyfin media libraries with their types and paths."""
    data = _jellyfin("/Library/VirtualFolders")
    if isinstance(data, str):
        return data
    lines = []
    for lib in data:
        name = lib.get("Name", "?")
        ctype = lib.get("CollectionType", "mixed")
        paths = ", ".join(lib.get("Locations", []))
        lines.append(f"• {name} ({ctype}) — {paths}")
    return "\n".join(lines) or "No libraries found."


@mcp.tool()
def jellyfin_recent(limit: int = 10) -> str:
    """Show recently added items in Jellyfin.

    Args:
        limit: Number of items to return (default: 10, max: 50).
    """
    limit = min(limit, 50)
    data = _jellyfin(f"/Items/Latest?Limit={limit}&EnableImages=false")
    if isinstance(data, str):
        return data
    lines = []
    for item in data:
        name = item.get("Name", "?")
        itype = item.get("Type", "?")
        year = item.get("ProductionYear", "")
        year_str = f" ({year})" if year else ""
        lines.append(f"• {name}{year_str} [{itype}]")
    return "\n".join(lines) or "Nothing recent."


@mcp.tool()
def jellyfin_system_info() -> str:
    """Get Jellyfin server system information (version, OS, etc.)."""
    data = _jellyfin("/System/Info/Public")
    if isinstance(data, str):
        return data
    lines = [
        f"Server: {data.get('ServerName', '?')}",
        f"Version: {data.get('Version', '?')}",
        f"OS: {data.get('OperatingSystem', '?')}",
        f"Architecture: {data.get('SystemArchitecture', '?')}",
        f"Local Address: {data.get('LocalAddress', '?')}",
    ]
    return "\n".join(lines)


@mcp.tool()
def jellyfin_scan_library() -> str:
    """Trigger a library scan in Jellyfin to detect new, changed, or removed media files.
    Requires JELLYFIN_API_KEY to be set."""
    if not JELLYFIN_URL:
        return "Jellyfin is not configured. Set JELLYFIN_URL."
    if not JELLYFIN_API_KEY:
        return "JELLYFIN_API_KEY is required for library scans. Set it in the environment."
    try:
        r = httpx.post(
            f"{JELLYFIN_URL}/Library/Refresh",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        return "✅ Library scan triggered."
    except httpx.HTTPStatusError as e:
        return f"❌ Failed: {e.response.status_code} — {e.response.text[:200]}"


# ── Entrypoint ──

def main():
    parser = argparse.ArgumentParser(
        description="arrstack-mcp — MCP server for Sonarr, Radarr, qBittorrent & Jellyfin"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default: 0.0.0.0)")
    args = parser.parse_args()

    enabled = []
    if SONARR_URL:
        enabled.append("Sonarr")
    if RADARR_URL:
        enabled.append("Radarr")
    if PROWLARR_URL:
        enabled.append("Prowlarr")
    if QBT_URL:
        enabled.append("qBittorrent")
    if JELLYFIN_URL:
        enabled.append("Jellyfin")

    if not enabled:
        print(
            "⚠️  No services configured. Set at least one of: "
            "SONARR_URL, RADARR_URL, QBT_URL, JELLYFIN_URL",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"🎬 arrstack-mcp starting ({', '.join(enabled)})", file=sys.stderr)
    print(f"   Transport: {args.transport}", file=sys.stderr)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
