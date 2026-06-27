"""
Agentic Resource Discovery (ARD) support for arrstack-mcp.

Implements the ARD specification (https://agenticresourcediscovery.org/,
schema: https://github.com/ards-project/ard-spec) so this MCP server can be
*published*, *discovered*, and *verified* across the agentic web.

Two documents are produced from the live server:

* ``ai-catalog.json`` — the ARD capability manifest. It is hosted at
  ``/.well-known/ai-catalog.json`` and advertises this server as a single
  ``application/mcp-server-card+json`` entry whose ``capabilities`` and
  ``representativeQueries`` let registries index it for semantic search.
* ``mcp-server-card.json`` — the MCP server card the catalog entry references
  (the "Solo Developer Path" from the spec). It lists every advertised tool
  with its ``inputSchema`` plus the connection endpoint and transport.

Everything is generated from the *enabled* tool set, so the catalog always
mirrors whatever ``ENABLED_SERVICES`` advertises. The builders are pure
functions (no I/O, no globals) so they are trivially unit-testable; ``server``
wires them into the HTTP transports and the ``--print-catalog`` CLI flag.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ── Spec constants ──

SPEC_VERSION = "1.0"
WELL_KNOWN_CATALOG_PATH = "/.well-known/ai-catalog.json"
WELL_KNOWN_SERVER_CARD_PATH = "/.well-known/mcp-server-card.json"

#: IANA-style media types defined by the ARD / ai-catalog data model.
MCP_SERVER_CARD_MEDIA_TYPE = "application/mcp-server-card+json"
CATALOG_MEDIA_TYPE = "application/ai-catalog+json"

DEFAULT_HOST_NAME = "arrstack-mcp"
DEFAULT_TRANSPORT = "streamable-http"
DOCUMENTATION_URL = "https://github.com/ct4nk3r/arrstack-mcp"

#: Fallback publisher used when no real domain is configured. It keeps the
#: emitted URN schema-valid even on a LAN-only homelab deployment.
FALLBACK_PUBLISHER = "localhost"

#: RFC 8141 domain-anchored URN format mandated by the ai-catalog schema.
URN_PATTERN = re.compile(r"^urn:air:[a-zA-Z0-9.-]+(:[a-zA-Z0-9._-]+)+$")

#: Characters allowed in the ``<publisher>`` URN segment (a bare FQDN/IP).
_PUBLISHER_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+$")

# MCP endpoint path for each HTTP transport, used to build connection hints.
_TRANSPORT_ENDPOINTS = {
    "streamable-http": "/mcp",
    "sse": "/sse",
}

# Natural-language queries used to seed registry vector embeddings. One per
# service, plus generic fallbacks. ``representative_queries`` selects from these
# in a variety-first order and caps the result at the schema's 5-item maximum.
_SERVICE_QUERIES = {
    "radarr": "add the movie Inception to my Radarr library",
    "sonarr": "what upcoming TV episodes are airing this week",
    "lidarr": "add a new music artist to my library",
    "prowlarr": "search my indexers for a 1080p release",
    "qbittorrent": "what torrents are downloading right now",
    "sabnzbd": "pause my usenet download queue",
    "rdtclient": "add a magnet link to my Real-Debrid downloader",
    "jellyfin": "what was recently added to my media server",
    "romm": "list the games in my ROM library",
    "gamevault": "pick a random PC game from my GameVault library",
    "bookshelf": "search for a book to add to my library",
}
_QUERY_PRIORITY = [
    "radarr", "sonarr", "qbittorrent", "romm", "lidarr",
    "jellyfin", "gamevault", "prowlarr", "sabnzbd", "rdtclient", "bookshelf",
]
_GENERAL_QUERIES = [
    "search and manage my homelab media library",
    "what is downloading in my media stack right now",
]

MIN_REPRESENTATIVE_QUERIES = 2
MAX_REPRESENTATIVE_QUERIES = 5


# ── Helpers ──


def normalize_public_url(public_url: str | None) -> str | None:
    """Return ``public_url`` without a trailing slash, or ``None`` if unset."""
    if not public_url:
        return None
    return public_url.strip().rstrip("/") or None


def resolve_publisher(domain: str | None = None, public_url: str | None = None) -> str:
    """Resolve the URN ``<publisher>`` segment from config.

    Preference order: explicit ``domain`` → host of ``public_url`` → fallback.
    The result is always a schema-valid publisher token; a ``host:port`` value
    is reduced to its host, and anything that is still not a bare FQDN/IP (e.g.
    an IPv6 literal) degrades to :data:`FALLBACK_PUBLISHER`.
    """
    candidate = (domain or "").strip().lower()
    if candidate:
        # Accept a bare host, a host:port, or a URL-ish value and keep the host.
        try:
            host = urlparse(candidate if "//" in candidate else "//" + candidate).hostname
        except ValueError:
            host = None
        if host:
            candidate = host
    if not candidate and public_url:
        candidate = (urlparse(public_url).hostname or "").strip().lower()
    if candidate and _PUBLISHER_PATTERN.match(candidate):
        return candidate
    return FALLBACK_PUBLISHER


def endpoint_url(public_url: str | None, transport: str = DEFAULT_TRANSPORT) -> str | None:
    """Absolute MCP endpoint URL for ``transport``, or ``None`` without a base URL."""
    base = normalize_public_url(public_url)
    if not base:
        return None
    return base + _TRANSPORT_ENDPOINTS.get(transport, "/mcp")


def server_card_url(public_url: str | None) -> str | None:
    """Absolute URL of the hosted MCP server card, or ``None`` without a base URL."""
    base = normalize_public_url(public_url)
    if not base:
        return None
    return base + WELL_KNOWN_SERVER_CARD_PATH


def representative_queries(enabled_services) -> list[str]:
    """2–5 natural-language sample queries reflecting the enabled services."""
    enabled = set(enabled_services or ())
    queries = [
        _SERVICE_QUERIES[key]
        for key in _QUERY_PRIORITY
        if key in enabled and key in _SERVICE_QUERIES
    ]
    for fallback in _GENERAL_QUERIES:
        if len(queries) >= MIN_REPRESENTATIVE_QUERIES:
            break
        queries.append(fallback)
    return queries[:MAX_REPRESENTATIVE_QUERIES]


def _tool_entries(mcp) -> list[dict]:
    """MCP server-card tool descriptors for every advertised tool."""
    entries = []
    for tool in sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name):
        entries.append(
            {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "inputSchema": tool.parameters,
            }
        )
    return entries


def tool_names(mcp) -> list[str]:
    """Sorted list of advertised tool names (used as catalog ``capabilities``)."""
    return sorted(tool.name for tool in mcp._tool_manager.list_tools())


def _enabled_service_names(enabled_services, service_config) -> list[str]:
    """Display names of the enabled services, in ``service_config`` order."""
    return [
        display
        for key, (display, _url, _prefix) in service_config.items()
        if key in enabled_services
    ]


def _describe(enabled_services, service_config) -> str:
    names = _enabled_service_names(enabled_services, service_config)
    if names:
        return (
            "Configurable MCP server exposing homelab media and game services "
            f"({', '.join(names)}) as tools for AI assistants to search, add, "
            "and manage media and game libraries."
        )
    return "Configurable MCP server for homelab media and game services."


# ── Document builders ──


def build_server_card(
    mcp,
    *,
    enabled_services,
    service_config,
    public_url: str | None = None,
    transport: str = DEFAULT_TRANSPORT,
    include_instructions: bool = True,
) -> dict:
    """Build the MCP server card referenced by the catalog entry.

    Mirrors the spec's "Solo Developer Path" card shape — ``name``,
    ``description``, ``tools[]`` — and, when a public base URL is known, enriches
    it with an actionable ``url``/``transport`` connection hint.
    """
    card: dict = {
        "name": mcp.name,
        "description": _describe(enabled_services, service_config),
    }
    if include_instructions and getattr(mcp, "instructions", None):
        card["instructions"] = mcp.instructions

    connect_url = endpoint_url(public_url, transport)
    if connect_url:
        card["url"] = connect_url
        card["transport"] = transport

    card["tools"] = _tool_entries(mcp)
    return card


def build_catalog(
    mcp,
    *,
    enabled_services,
    service_config,
    public_url: str | None = None,
    domain: str | None = None,
    host_name: str = DEFAULT_HOST_NAME,
    transport: str = DEFAULT_TRANSPORT,
    updated_at: str | None = None,
    embed_card: bool | None = None,
) -> dict:
    """Build the ``ai-catalog.json`` capability manifest for this server.

    The single entry advertises the whole arrstack MCP server. Per the spec's
    strict value-or-reference rule, the entry carries **exactly one** of:

    * ``url`` — a link to the hosted server card (used when ``public_url`` is
      configured, so registries fetch the heavyweight card separately), or
    * ``data`` — the server card embedded inline (used for static/offline
      hosting so the manifest is self-contained).

    ``embed_card`` forces one mode; by default it follows ``public_url``.
    """
    base_url = normalize_public_url(public_url)
    publisher = resolve_publisher(domain, base_url)
    if embed_card is None:
        embed_card = base_url is None

    entry: dict = {
        "identifier": f"urn:air:{publisher}:server:arrstack",
        "displayName": "arrstack-mcp",
        "type": MCP_SERVER_CARD_MEDIA_TYPE,
        "description": _describe(enabled_services, service_config),
        "tags": _build_tags(enabled_services, service_config),
        "capabilities": tool_names(mcp),
        "representativeQueries": representative_queries(enabled_services),
    }

    card = build_server_card(
        mcp,
        enabled_services=enabled_services,
        service_config=service_config,
        public_url=base_url,
        transport=transport,
    )
    # Strict value-or-reference: reference the hosted card by URL only when we
    # actually have one, otherwise embed it inline so we never emit a null url.
    card_url = None if embed_card else server_card_url(base_url)
    if card_url:
        entry["url"] = card_url
        connect_url = endpoint_url(base_url, transport)
        if connect_url:
            entry["metadata"] = {
                "endpoint": connect_url,
                "transport": transport,
                "protocol": "mcp",
            }
    else:
        entry["data"] = card

    if updated_at:
        entry["updatedAt"] = updated_at

    host: dict = {
        "displayName": host_name,
        "documentationUrl": DOCUMENTATION_URL,
    }
    if publisher != FALLBACK_PUBLISHER:
        host["identifier"] = f"did:web:{publisher}"

    return {
        "specVersion": SPEC_VERSION,
        "host": host,
        "entries": [entry],
    }


def _build_tags(enabled_services, service_config) -> list[str]:
    service_tags = [key for key in service_config if key in enabled_services]
    return service_tags + ["mcp", "homelab", "media", "self-hosted"]


# ── Self-validation (no external dependency) ──


def iter_validation_errors(catalog: dict):
    """Yield human-readable problems with ``catalog`` against the ARD invariants.

    A lightweight conformance check covering the rules the ai-catalog JSON
    Schema enforces, without pulling in a ``jsonschema`` dependency. Used by the
    ``--print-catalog`` CLI and the test-suite.
    """
    if catalog.get("specVersion") != SPEC_VERSION:
        yield f"specVersion must be {SPEC_VERSION!r}, got {catalog.get('specVersion')!r}"

    host = catalog.get("host")
    if not isinstance(host, dict) or not host.get("displayName"):
        yield "host.displayName is required"

    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        yield "entries must be a non-empty array"
        return

    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        for field in ("identifier", "displayName", "type"):
            if not entry.get(field):
                yield f"{where}.{field} is required"

        identifier = entry.get("identifier", "")
        if identifier and not URN_PATTERN.match(identifier):
            yield f"{where}.identifier {identifier!r} is not a valid urn:air URN"

        # Exactly one of a non-empty 'url' string or a 'data' object (schema oneOf).
        has_url = isinstance(entry.get("url"), str) and bool(entry.get("url"))
        has_data = isinstance(entry.get("data"), dict)
        if has_url == has_data:
            yield f"{where} must have exactly one of a non-empty 'url' string or a 'data' object"

        queries = entry.get("representativeQueries")
        if queries is not None:
            if not (MIN_REPRESENTATIVE_QUERIES <= len(queries) <= MAX_REPRESENTATIVE_QUERIES):
                yield (
                    f"{where}.representativeQueries must have "
                    f"{MIN_REPRESENTATIVE_QUERIES}-{MAX_REPRESENTATIVE_QUERIES} items"
                )


def validate_catalog(catalog: dict) -> list[str]:
    """Return a list of ARD conformance problems (empty when the catalog is valid)."""
    return list(iter_validation_errors(catalog))
