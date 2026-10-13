"""
arrstack-mcp — configurable MCP server for homelab media and game services.

Exposes your *arr media stack as MCP tools so any AI assistant
(Claude Desktop, Cursor, VS Code Copilot, OpenClaw, etc.) can
search, add, and manage your media library.
"""

import os
import pathlib
import re
import sys
import json
import base64
import argparse
import logging
import binascii
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, Response

from . import ard
from . import config
from . import discover
from . import doctor
from . import setup_ui
from . import wizard

# ── Configuration ──
# Every setting is declared in config.py; values come from the environment,
# <VAR>_FILE secret files, or an optional TOML file (--config / ARRSTACK_CONFIG).
# Loading never raises: problems are reported by main() and --check.
# arrstack_mcp.cli turns `--config PATH` into $ARRSTACK_CONFIG before importing
# this module and marks it as explicit.
_EXPLICIT_CONFIG = os.environ.pop(config.EXPLICIT_CONFIG_ENV, "") == "1"
_CONFIG = config.load(
    os.environ,
    config.config_path(os.environ),
    # A config file named only by $ARRSTACK_CONFIG may not exist yet: the setup
    # page creates it. An explicit --config path must exist (catches typos).
    missing_ok=not _EXPLICIT_CONFIG,
)
SETTINGS = _CONFIG.values
CONFIG_PROBLEMS = _CONFIG.problems

_LOG_LEVEL = SETTINGS["LOG_LEVEL"].strip().upper()
if _LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    CONFIG_PROBLEMS.append(f"LOG_LEVEL: unknown level '{SETTINGS['LOG_LEVEL']}'")
    _LOG_LEVEL = "INFO"
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("arrstack-mcp")
# httpx logs every request URL at INFO, and some APIs (SABnzbd, Prowlarr
# download links) carry the API key in the query string. Our own helpers log
# method + path only.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Settings whose values must never appear in logs or tool output.
_SECRET_SETTINGS = config.SECRET_ENVS
_SECRET_PARAM = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|password|passwd|pass|sid)=([^&\s\"']+)"
)
# Shorter values (e.g. a password like "admin") would mask ordinary words.
_MIN_LITERAL_SECRET = 6


def _redact(text: str) -> str:
    """Mask credentials in text bound for logs or tool output.

    Masks `apikey=...`-style query/form values, plus any configured secret
    value wherever it appears (headers echoed in an error body, etc.).
    """
    text = _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", text)
    for name in _SECRET_SETTINGS:
        value = globals().get(name) or ""
        if len(value) >= _MIN_LITERAL_SECRET and value in text:
            text = text.replace(value, "***")
    return text


_base_record_factory = logging.getLogRecordFactory()


def _redacting_record_factory(*args, **kwargs):
    # Redact at record creation so every handler (ours, uvicorn's, test
    # capture) sees the masked message.
    record = _base_record_factory(*args, **kwargs)
    try:
        message = record.getMessage()
    except Exception:  # malformed format args: leave the record alone
        return record
    masked = _redact(message)
    if masked != message:
        record.msg, record.args = masked, ()
    return record


logging.setLogRecordFactory(_redacting_record_factory)

SONARR_URL = SETTINGS["SONARR_URL"].rstrip("/")
SONARR_API_KEY = SETTINGS["SONARR_API_KEY"]
RADARR_URL = SETTINGS["RADARR_URL"].rstrip("/")
RADARR_API_KEY = SETTINGS["RADARR_API_KEY"]
LIDARR_URL = SETTINGS["LIDARR_URL"].rstrip("/")
LIDARR_API_KEY = SETTINGS["LIDARR_API_KEY"]
QBT_URL = SETTINGS["QBT_URL"].rstrip("/")
QBT_USER = SETTINGS["QBT_USER"]
QBT_PASS = SETTINGS["QBT_PASS"]
RDT_URL = SETTINGS["RDT_URL"].rstrip("/")
RDT_USER = SETTINGS["RDT_USER"]
RDT_PASS = SETTINGS["RDT_PASS"]
PROWLARR_URL = SETTINGS["PROWLARR_URL"].rstrip("/")
PROWLARR_API_KEY = SETTINGS["PROWLARR_API_KEY"]
JELLYFIN_URL = SETTINGS["JELLYFIN_URL"].rstrip("/")
JELLYFIN_API_KEY = SETTINGS["JELLYFIN_API_KEY"]
ROMM_URL = SETTINGS["ROMM_URL"].rstrip("/")
ROMM_API_TOKEN = SETTINGS["ROMM_API_TOKEN"]
ROMM_USER = SETTINGS["ROMM_USER"]
ROMM_PASS = SETTINGS["ROMM_PASS"]
GAMEVAULT_URL = SETTINGS["GAMEVAULT_URL"].rstrip("/")
GAMEVAULT_API_KEY = SETTINGS["GAMEVAULT_API_KEY"]
MCP_ALLOWED_HOSTS = [
    host.strip() for host in SETTINGS["MCP_ALLOWED_HOSTS"].split(",") if host.strip()
]
SAB_URL = SETTINGS["SAB_URL"].rstrip("/")
SAB_API_KEY = SETTINGS["SAB_API_KEY"]
BOOKSHELF_URL = SETTINGS["BOOKSHELF_URL"].rstrip("/")
BOOKSHELF_API_KEY = SETTINGS["BOOKSHELF_API_KEY"]
SEERR_URL = SETTINGS["SEERR_URL"].rstrip("/")
SEERR_API_KEY = SETTINGS["SEERR_API_KEY"]
ENABLED_SERVICES = SETTINGS["ENABLED_SERVICES"]
INSTRUCTION_PRESETS = SETTINGS["INSTRUCTION_PRESETS"]
EXTRA_INSTRUCTIONS = SETTINGS["EXTRA_INSTRUCTIONS"]
ALLOW_DESTRUCTIVE = SETTINGS["ALLOW_DESTRUCTIVE"]

# ── Agentic Resource Discovery (ARD) ──
# Publish this MCP server to ARD registries/crawlers. See ard.py and README.
# ARD publishes discoverable *metadata* only — it does not authenticate the
# server or make it safe to expose. Keep HTTP transports behind Tailscale or an
# authenticated proxy (see the Security section of the README).
# ARD_ENABLED:    auto (default, serve on HTTP transports) | true | false
# ARD_PUBLIC_URL: public base URL clients reach this server at, e.g.
#                 https://arrstack.example.com — advertises an absolute
#                 connection endpoint inside the card.
# ARD_DOMAIN:     publisher domain for the urn:air logical identifier (defaults
#                 to the host of ARD_PUBLIC_URL, else "localhost"). This is a
#                 name, not a resolvable address.
# ARD_HOST_NAME:  human-readable catalog host name.
# ARD_EMBED_CARD: auto (default; embed the server card inline only when no
#                 ARD_PUBLIC_URL is set) | true (always embed — best for static
#                 hosting so the manifest is self-contained) | false (always
#                 reference the hosted server card by URL).
# ARD_DID_WEB:    opt-in did:web host identity, e.g. arrstack.example.com. Only
#                 set this for a domain whose root you control and where you host
#                 a DID document — it is otherwise left off so we never advertise
#                 an identity that can't resolve.
ARD_ENABLED = SETTINGS["ARD_ENABLED"].strip().lower()
ARD_PUBLIC_URL = SETTINGS["ARD_PUBLIC_URL"].strip()
ARD_DOMAIN = SETTINGS["ARD_DOMAIN"].strip()
ARD_HOST_NAME = SETTINGS["ARD_HOST_NAME"].strip() or ard.DEFAULT_HOST_NAME
ARD_EMBED_CARD = SETTINGS["ARD_EMBED_CARD"].strip().lower()
ARD_DID_WEB = SETTINGS["ARD_DID_WEB"].strip()

# key -> (display name, base URL, tool prefix); order and shape kept for callers.
SERVICE_CONFIG = {
    key: (service.name, SETTINGS[service.url.env], service.prefix)
    for key, service in config.SERVICES.items()
}
SERVICE_ALIASES = dict(config.SERVICE_ALIASES)

_BASE_INSTRUCTIONS = (
    "Homelab media stack tools for Sonarr (TV), Radarr (Movies), Lidarr (Music), "
    "Prowlarr (Indexers), qBittorrent, RDTClient and SABnzbd (Downloads), Jellyfin (Streaming), "
    "Seerr (Media requests), Bookshelf (Books), RomM (ROM library), and GameVault (PC game library). "
    "Use these tools to search, add, and manage media and game libraries."
)

# Opt-in workflow guidance appended to the server instructions with
# INSTRUCTION_PRESETS=<name>[,<name>]. Kept out of the default so every client
# isn't told about one deployment's trackers.
INSTRUCTION_PRESET_TEXT = {
    "hun-ncore": (
        "## Hungarian (HuN) / nCore workflow\n"
        "nCore is a Hungarian private tracker with dual-audio (HuN) releases. "
        "When the user wants Hungarian releases:\n"
        "1. Make sure movies/series are on the correct Hungarian quality profile "
        "(use radarr_list_movies / sonarr_list_series — they show profile names and IDs).\n"
        "2. IMPORTANT: Dual-audio releases are 2–3× larger than English-only at the same "
        "quality tier because they carry two full audio tracks. The default 1080p max size "
        "(~83 MB/min) is too low — raise Bluray-1080p, Remux-1080p, WEBDL-1080p, "
        "WEBRip-1080p, and HDTV-1080p max to at least 400 MB/min with "
        "radarr_set_quality_definition / sonarr_set_quality_definition.\n"
        "3. After triggering a search, always check the queue to verify releases contain "
        "'HuN' or 'HUN' in the filename. If Radarr/Sonarr grabbed an English-only release, "
        "delete it from the queue (blocklist=true) and re-search.\n"
        "4. If existing files need replacing (e.g. English → HuN), delete the movie file "
        "with radarr_delete_movie_file / sonarr_delete_episode_file first, then search.\n"
    ),
}

# Tools that delete downloads or library files. ALLOW_DESTRUCTIVE=false hides
# them from the catalog for deployments that want an additive-only agent.
DESTRUCTIVE_TOOLS = frozenset({
    "sonarr_delete_queue_item",
    "sonarr_delete_episode_file",
    "radarr_delete_queue_item",
    "radarr_delete_movie_file",
    "lidarr_delete_queue_item",
    "qbt_delete",
    "rdt_delete",
    "sab_delete_job",
})


def _parse_bool(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def _instruction_presets(value: str) -> list[str]:
    """Resolve INSTRUCTION_PRESETS into known preset names."""
    names = [item.strip().lower() for item in value.split(",") if item.strip()]
    unknown = [name for name in names if name not in INSTRUCTION_PRESET_TEXT]
    if unknown:
        valid = ", ".join(INSTRUCTION_PRESET_TEXT)
        raise ValueError(f"Unknown instruction preset '{unknown[0]}'. Valid presets: {valid}")
    return names


def _build_instructions(presets: str = "", extra: str = "") -> str:
    """Server instructions: the base text, any opt-in presets, then free text."""
    parts = [_BASE_INSTRUCTIONS]
    try:
        parts += [INSTRUCTION_PRESET_TEXT[name] for name in _instruction_presets(presets)]
    except ValueError:
        pass  # reported by main() before the server starts
    if extra.strip():
        parts.append(extra.strip())
    return "\n\n".join(part.strip() for part in parts) + "\n"


mcp = FastMCP(
    "arrstack",
    instructions=_build_instructions(INSTRUCTION_PRESETS, EXTRA_INSTRUCTIONS),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=MCP_ALLOWED_HOSTS,
    ),
)

# ── Agentic Resource Discovery (ARD) endpoints ──
# The catalog and server card are generated from the *enabled* tool set at
# request time, so they always mirror what this server advertises.

# HTTP transport this process is serving (set in main()); shapes the absolute
# MCP endpoint URL advertised in the catalog/server card.
_active_transport = ard.DEFAULT_TRANSPORT

_ARD_DISABLED_VALUES = {"false", "0", "no", "off", "disabled"}
_ARD_TRUE_VALUES = {"true", "1", "yes", "on"}


def _ard_response_headers() -> dict:
    """Response headers for the well-known ARD documents.

    The permissive ``Access-Control-Allow-Origin: *`` header is only emitted
    when a public URL is configured (i.e. the operator has opted into public
    discovery). On a private/LAN deployment we omit it so the documents can't be
    read cross-origin by arbitrary browser JavaScript — server-side crawlers,
    which ignore CORS, are unaffected either way.
    """
    headers = {"Cache-Control": "public, max-age=300"}
    if ARD_PUBLIC_URL:
        headers["Access-Control-Allow-Origin"] = "*"
    return headers


def _ard_disabled() -> bool:
    """Whether ARD publishing has been explicitly turned off."""
    return ARD_ENABLED in _ARD_DISABLED_VALUES


def _ard_embed_card():
    """Resolve ARD_EMBED_CARD into True/False, or None for automatic behaviour."""
    if ARD_EMBED_CARD in _ARD_TRUE_VALUES:
        return True
    if ARD_EMBED_CARD in _ARD_DISABLED_VALUES:
        return False
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_ard_catalog(updated_at: str | None = None) -> dict:
    """Assemble the ai-catalog.json manifest from the live server config."""
    return ard.build_catalog(
        mcp,
        enabled_services=_selected_services(),
        service_config=SERVICE_CONFIG,
        public_url=ARD_PUBLIC_URL or None,
        domain=ARD_DOMAIN or None,
        host_name=ARD_HOST_NAME,
        transport=_active_transport,
        updated_at=updated_at,
        embed_card=_ard_embed_card(),
        did_web=ARD_DID_WEB or None,
    )


def _build_ard_server_card() -> dict:
    """Assemble the MCP server card from the live server config."""
    return ard.build_server_card(
        mcp,
        enabled_services=_selected_services(),
        service_config=SERVICE_CONFIG,
        public_url=ARD_PUBLIC_URL or None,
        transport=_active_transport,
    )


def _emit_ard_document(server_card: bool = False) -> None:
    """Print an ARD document to stdout for static hosting; exit non-zero if invalid."""
    if server_card:
        print(json.dumps(_build_ard_server_card(), indent=2))
        return
    catalog = _build_ard_catalog(updated_at=_now_iso())
    problems = ard.validate_catalog(catalog)
    if problems:
        for problem in problems:
            print(f"⚠️  ARD catalog invalid: {problem}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(catalog, indent=2))


@mcp.custom_route(ard.WELL_KNOWN_CATALOG_PATH, methods=["GET"])
async def _serve_ai_catalog(request):
    """Serve the ARD capability manifest (ai-catalog.json) for discovery."""
    if _ard_disabled():
        return Response(status_code=404)
    return JSONResponse(_build_ard_catalog(updated_at=_now_iso()), headers=_ard_response_headers())


@mcp.custom_route(ard.WELL_KNOWN_SERVER_CARD_PATH, methods=["GET"])
async def _serve_mcp_server_card(request):
    """Serve the MCP server card referenced by the catalog entry."""
    if _ard_disabled():
        return Response(status_code=404)
    return JSONResponse(_build_ard_server_card(), headers=_ard_response_headers())


# ── HTTP helpers ──


def _http_error(service: str, error: Exception) -> str:
    """Format an HTTP error without exposing credentials or request headers."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        body = error.response.text[:200] if error.response is not None else ""
        body = _redact(body)
        logger.error("%s HTTP %s: %s", service, status, body)
        return f"{service} request failed: HTTP {status} — {body}"
    logger.error("%s request error: %s", service, error)
    return f"{service} request error: {_redact(str(error))}"


def _decode_json(service: str, r):
    """Decode a JSON response body.

    An empty body (common for DELETE and some command endpoints) decodes to an
    empty dict so callers treat it as success. A non-empty, non-JSON body (an
    HTML login page, a proxy error, ...) becomes a readable error string.
    """
    try:
        return r.json()
    except ValueError:  # json.JSONDecodeError is a ValueError subclass
        if not (r.text or "").strip():
            return {}
        logger.error("%s returned non-JSON response (HTTP %s)", service, r.status_code)
        return f"{service} returned a non-JSON response (HTTP {r.status_code})."


def _arr_request(service, base_url, api_path, api_key, path, method="GET", json=None, params=None):
    """Shared request helper for the *arr family (X-Api-Key auth, JSON API)."""
    logger.info("%s %s %s", service, method, path)
    headers = {"X-Api-Key": api_key} if api_key else {}
    try:
        r = httpx.request(
            method,
            f"{base_url}{api_path}{path}",
            headers=headers,
            json=json,
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return _decode_json(service, r)
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        return _http_error(service, error)


def _sonarr(path: str, method: str = "GET", json=None, params=None):
    if not SONARR_URL:
        return "Sonarr is not configured. Set SONARR_URL and SONARR_API_KEY."
    return _arr_request("sonarr", SONARR_URL, "/api/v3", SONARR_API_KEY, path, method, json, params)


def _radarr(path: str, method: str = "GET", json=None, params=None):
    if not RADARR_URL:
        return "Radarr is not configured. Set RADARR_URL and RADARR_API_KEY."
    return _arr_request("radarr", RADARR_URL, "/api/v3", RADARR_API_KEY, path, method, json, params)


def _lidarr(path: str, method: str = "GET", json=None, params=None):
    if not LIDARR_URL:
        return "Lidarr is not configured. Set LIDARR_URL and LIDARR_API_KEY."
    return _arr_request("lidarr", LIDARR_URL, "/api/v1", LIDARR_API_KEY, path, method, json, params)


_qbt_cookies = None  # None = not logged in; {} = logged in without a cookie (auth bypass)


def _qbt_session_cookies(login) -> dict | None:
    """Pick the WebUI session cookie out of a /auth/login response.

    qBittorrent < 5 names it "SID"; 5.x names it "QBT_SID_<port>" so several
    instances can share a reverse-proxy domain. When the client is whitelisted
    ("Bypass authentication for clients on localhost / in whitelisted subnets"),
    login answers "Ok." without setting any cookie and requests work without one.
    Returns None when the login was rejected.
    """
    for name, value in login.cookies.items():
        if name == "SID" or name.startswith("QBT_SID_"):
            return {name: value}
    if (login.text or "").strip().lower() == "ok.":
        return {}
    return None


def _qbt(path: str, method: str = "GET", data=None, params=None, files=None, _retry: bool = False):
    global _qbt_cookies
    if not QBT_URL:
        return "qBittorrent is not configured. Set QBT_URL and QBT_PASS."
    logger.info("qbt %s %s", method, path)
    try:
        if _qbt_cookies is None:
            login = httpx.post(
                f"{QBT_URL}/api/v2/auth/login",
                data={"username": QBT_USER, "password": QBT_PASS},
                timeout=10,
            )
            login.raise_for_status()
            _qbt_cookies = _qbt_session_cookies(login)
            if _qbt_cookies is None:
                return "qBittorrent login failed: check QBT_USER and QBT_PASS."
        r = httpx.request(
            method,
            f"{QBT_URL}/api/v2{path}",
            cookies=_qbt_cookies or None,
            data=data,
            params=params,
            files=files,
            timeout=30,
        )
        if r.status_code == 403:
            _qbt_cookies = None
            if _retry:
                return "qBittorrent auth failure: 403 after retry."
            return _qbt(path, method, data=data, params=params, files=files, _retry=True)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return r.text
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        return _http_error("qBittorrent", error)


_rdt_sid = None


def _rdt(path: str, method: str = "GET", data=None, params=None, _retry: bool = False):
    """Talk to RDTClient's qBittorrent-compatible API at /api/v2.

    RDTClient is a .NET app that mimics qBittorrent's WebUI API, so the same
    cookie-based session flow applies. Login may be required (depends on user
    config). Mirrors `_qbt` discipline: single retry on 403, no recursion
    beyond that, no logging of credentials.
    """
    global _rdt_sid
    if not RDT_URL:
        return "RDTClient is not configured. Set RDT_URL (and RDT_USER/RDT_PASS if login is enabled)."
    logger.info("rdt %s %s", method, path)
    try:
        if not _rdt_sid:
            login = httpx.post(
                f"{RDT_URL}/api/v2/auth/login",
                data={"username": RDT_USER, "password": RDT_PASS},
                timeout=10,
            )
            login.raise_for_status()
            # qBittorrent-compatible login returns "Ok." on success and sets a SID cookie.
            # If login is disabled in RDTClient, the API may not require a cookie at all —
            # treat absent SID + non-"Ok." body as a likely auth failure.
            _rdt_sid = login.cookies.get("SID")
            if not _rdt_sid and login.text.strip() != "Ok.":
                logger.error("rdt login failed: %s", login.text[:80])
                return "RDTClient login failed (check RDT_USER/RDT_PASS, or confirm login is required)."
        r = httpx.request(
            method,
            f"{RDT_URL}/api/v2{path}",
            cookies={"SID": _rdt_sid} if _rdt_sid else None,
            data=data,
            params=params,
            timeout=30,
        )
        if r.status_code == 403:
            _rdt_sid = None
            if _retry:
                logger.error("rdt 403 after retry; auth failure")
                return "RDTClient auth failure: 403 after retry (check RDT_USER/RDT_PASS)."
            return _rdt(path, method, data=data, params=params, _retry=True)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return r.text
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        return _http_error("rdt", e)


def _rdt_native(path: str, method: str = "GET", json=None, params=None):
    """Call RDTClient's native /api surface (e.g. /api/Settings, /api/Authentication).

    Reuses the SID cookie established by `_rdt` if available. The native API may
    require a different auth scheme on some installs; if you hit 401/403 for
    everything here, extend this helper to do a fresh login.
    """
    global _rdt_sid
    if not RDT_URL:
        return "RDTClient is not configured. Set RDT_URL."
    logger.info("rdt-native %s %s", method, path)
    try:
        cookies = {"SID": _rdt_sid} if _rdt_sid else None
        r = httpx.request(
            method,
            f"{RDT_URL}{path}",
            cookies=cookies,
            json=json,
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return r.text
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        return _http_error("rdt-native", e)


def _jellyfin(path: str, params=None):
    if not JELLYFIN_URL:
        return "Jellyfin is not configured. Set JELLYFIN_URL."
    logger.info("jellyfin GET %s", path)
    headers = {}
    if JELLYFIN_API_KEY:
        headers["Authorization"] = f'MediaBrowser Token="{JELLYFIN_API_KEY}"'
    try:
        r = httpx.get(f"{JELLYFIN_URL}{path}", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return _decode_json("jellyfin", r)
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        return _http_error("jellyfin", error)


def _prowlarr(path: str, method: str = "GET", json=None, params=None):
    if not PROWLARR_URL:
        return "Prowlarr is not configured. Set PROWLARR_URL and PROWLARR_API_KEY."
    return _arr_request("prowlarr", PROWLARR_URL, "/api/v1", PROWLARR_API_KEY, path, method, json, params)


def _romm(path: str, method: str = "GET", params=None, json=None, public: bool = False):
    if not ROMM_URL:
        return "RomM is not configured. Set ROMM_URL."
    if not public and not ROMM_API_TOKEN and not (ROMM_USER and ROMM_PASS):
        return (
            "RomM authentication is not configured. Set ROMM_API_TOKEN or "
            "ROMM_USER and ROMM_PASS."
        )

    headers = {}
    auth = None
    if ROMM_API_TOKEN:
        headers["Authorization"] = f"Bearer {ROMM_API_TOKEN}"
    elif ROMM_USER and ROMM_PASS:
        auth = httpx.BasicAuth(ROMM_USER, ROMM_PASS)

    logger.info("romm %s %s", method, path)
    try:
        r = httpx.request(
            method,
            f"{ROMM_URL}{path}",
            headers=headers,
            auth=auth,
            params=params,
            json=json,
            timeout=30,
        )
        r.raise_for_status()
        return _decode_json("RomM", r)
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        return _http_error("RomM", error)


def _gamevault(path: str, method: str = "GET", params=None):
    if not GAMEVAULT_URL:
        return "GameVault is not configured. Set GAMEVAULT_URL."
    if not GAMEVAULT_API_KEY:
        return "GameVault authentication is not configured. Set GAMEVAULT_API_KEY."

    logger.info("gamevault %s %s", method, path)
    try:
        r = httpx.request(
            method,
            f"{GAMEVAULT_URL}{path}",
            headers={"X-Api-Key": GAMEVAULT_API_KEY},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return _decode_json("GameVault", r)
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        return _http_error("GameVault", error)


def _format_size(size_bytes) -> str:
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024


def _apply_quality_allowlist(items, allow_lower: set, disallow_lower: set):
    """Toggle 'allowed' on quality profile items matching the given
    (lowercased) names. Radarr/Sonarr quality profiles nest grouped
    qualities (e.g. "WEB 1080p" containing WEBDL-1080p + WEBRip-1080p) —
    the group's own 'allowed' flag is what governs selection, so a match
    on a member's name toggles the enclosing group instead of the leaf.
    Mutates `items` in place. Returns (changed_descriptions, names_not_found).
    """
    changed = []
    seen_lower = set()
    for item in items:
        quality = item.get("quality")
        name = quality.get("name") if quality else item.get("name")
        if not name:
            continue
        name_lower = name.lower()
        nested = item.get("items") or []
        member_names_lower = {
            (m.get("quality") or {}).get("name", "").lower() for m in nested
        }
        matches_allow = name_lower in allow_lower or bool(member_names_lower & allow_lower)
        matches_disallow = name_lower in disallow_lower or bool(member_names_lower & disallow_lower)
        if matches_allow:
            seen_lower.add(name_lower)
            seen_lower |= member_names_lower & allow_lower
            if not item.get("allowed"):
                item["allowed"] = True
                changed.append(f"+{name}")
        if matches_disallow:
            seen_lower.add(name_lower)
            seen_lower |= member_names_lower & disallow_lower
            if item.get("allowed"):
                item["allowed"] = False
                changed.append(f"-{name}")
    not_found = sorted((allow_lower | disallow_lower) - seen_lower)
    return changed, not_found


def _tag_labels(fetch) -> dict:
    """Map tag id -> label for an *arr service; empty if the lookup fails.

    Tags are decoration in list/get output, so a failed /tag call must not
    break the listing itself.
    """
    data = fetch("/tag")
    if not isinstance(data, list):
        return {}
    return {t["id"]: t["label"] for t in data if isinstance(t, dict) and "id" in t}


def _resolve_tag_ids(all_tags: list, tags_csv: str):
    """Resolve a comma-separated list of tag labels to their IDs against
    `all_tags` ([{id,label}, ...]). Returns (resolved_ids, unmatched_labels).
    """
    label_to_id = {t["label"].lower(): t["id"] for t in all_tags}
    ids, unmatched = [], []
    for raw in tags_csv.split(","):
        label = raw.strip()
        if not label:
            continue
        tid = label_to_id.get(label.lower())
        if tid is None:
            unmatched.append(label)
        else:
            ids.append(tid)
    return ids, unmatched


def _selected_services(value: str | None = None) -> set[str]:
    """Resolve ENABLED_SERVICES into canonical service keys."""
    raw = (value if value is not None else ENABLED_SERVICES).strip().lower()
    if not raw or raw == "auto":
        return {key for key, (_, url, _) in SERVICE_CONFIG.items() if url}
    if raw == "all":
        return set(SERVICE_CONFIG)

    selected = set()
    for item in raw.split(","):
        key = SERVICE_ALIASES.get(item.strip(), item.strip())
        if key not in SERVICE_CONFIG:
            valid = ", ".join(SERVICE_CONFIG)
            raise ValueError(f"Unknown service '{item.strip()}'. Valid services: {valid}")
        selected.add(key)
    return selected


def _configure_service_tools(value: str | None = None) -> set[str]:
    """Remove tools of unselected services, and destructive tools if disallowed."""
    selected = _selected_services(value)
    disabled_prefixes = [
        prefix
        for key, (_, _, prefix) in SERVICE_CONFIG.items()
        if key not in selected
    ]
    allow_destructive = _parse_bool("ALLOW_DESTRUCTIVE", ALLOW_DESTRUCTIVE)
    for tool in list(mcp._tool_manager.list_tools()):
        if tool.name.startswith(tuple(disabled_prefixes)) or (
            not allow_destructive and tool.name in DESTRUCTIVE_TOOLS
        ):
            mcp.remove_tool(tool.name)
    return selected


def _print_service_status(value: str | None = None) -> None:
    selected = _selected_services(value)
    print("Service catalog:")
    for key, (name, url, prefix) in SERVICE_CONFIG.items():
        state = "enabled" if key in selected else "disabled"
        configured = "configured" if url else "not configured"
        count = sum(
            tool.name.startswith(prefix) for tool in mcp._tool_manager.list_tools()
        )
        print(f"  {key:12} {state:8} {configured:14} {count:2} tools  ({name})")


def _run_setup(output: str | None, fmt: str, use_discovery: bool, arr_config_dirs, timeout: float) -> int:
    """--setup: interactive wizard that tests each service and writes a config file."""
    if CONFIG_PROBLEMS:
        print(_format_config_problems() + "\n(continuing; the wizard will ask for these values)\n")
    found = {}
    if use_discovery or arr_config_dirs:
        print("Looking for services (known container names and localhost only)...")
        found = discover.discover(arr_config_dirs, network=use_discovery)
    # Only a TOML run defaults to the --config file; never write env text into it.
    default = (_CONFIG.path or "arrstack.toml") if fmt == "toml" else ".env"
    path = pathlib.Path(output or default)
    try:
        result = wizard.run_setup(_CONFIG, path, fmt, discovered=found, timeout=timeout)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled; nothing written.")
        return 1
    return 0 if result else 1


def _run_discover(arr_config_dirs, timeout: float) -> int:
    """--discover: list services found on the network / in *arr config files."""
    found = discover.discover(arr_config_dirs, network=True, timeout=timeout)
    if not found:
        print("No services found at the usual container names or on localhost.")
        return 1
    for item in found.values():
        print(f"  • {item.describe()}")
    print("\nRun `python server.py --setup --discover` to configure them interactively.")
    return 0


def _sab(mode: str, **params):
    if not SAB_URL or not SAB_API_KEY:
        return "SABnzbd is not configured. Set SAB_URL and SAB_API_KEY."
    # Log only the mode, never the apikey or values that may include secrets.
    logger.info("sab GET mode=%s", mode)
    try:
        query = {"mode": mode, "apikey": SAB_API_KEY, "output": "json"}
        # Drop None values; pass everything else as-is so httpx URL-encodes.
        for k, v in params.items():
            if v is not None:
                query[k] = v
        r = httpx.get(f"{SAB_URL}/sabnzbd/api", params=query, timeout=30)
        r.raise_for_status()
        return _decode_json("sab", r)
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        return _http_error("sab", e)


def _bookshelf(path: str, method: str = "GET", json=None, params=None):
    """HTTP helper for Bookshelf (a Hardcover-flavored Readarr fork; Readarr v1 API)."""
    if not BOOKSHELF_URL:
        return "Bookshelf is not configured. Set BOOKSHELF_URL (and BOOKSHELF_API_KEY)."
    return _arr_request("bookshelf", BOOKSHELF_URL, "/api/v1", BOOKSHELF_API_KEY, path, method, json, params)


def _seerr(path: str, method: str = "GET", json=None, params=None):
    """HTTP helper for Seerr (Overseerr/Jellyseerr media requests; /api/v1, X-Api-Key)."""
    if not SEERR_URL:
        return "Seerr is not configured. Set SEERR_URL and SEERR_API_KEY."
    return _arr_request("seerr", SEERR_URL, "/api/v1", SEERR_API_KEY, path, method, json, params)


# ════════════════════════════════════════════════════════════════
#  Sonarr Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def sonarr_list_series() -> str:
    """List all TV series in Sonarr with monitoring status, episode counts, disk usage, and tags."""
    data = _sonarr("/series")
    if isinstance(data, str):
        return data
    profiles = {}
    try:
        for p in _sonarr("/qualityprofile"):
            profiles[p["id"]] = p["name"]
    except Exception:
        pass
    tag_labels = _tag_labels(_sonarr)
    lines = []
    for s in sorted(data, key=lambda x: x["title"]):
        stats = s.get("statistics", {})
        have = stats.get("episodeFileCount", 0)
        total = stats.get("episodeCount", 0)
        size_gb = stats.get("sizeOnDisk", 0) / 1e9
        icon = "✅" if s.get("monitored") else "⏸️"
        profile_name = profiles.get(s.get("qualityProfileId"), "?")
        tags = ", ".join(tag_labels.get(tid, str(tid)) for tid in s.get("tags", []))
        tags_str = f" [tags: {tags}]" if tags else ""
        lines.append(
            f"{icon} [id:{s['id']}] {s['title']} ({s.get('year', '?')}) — "
            f"{have}/{total} episodes, {size_gb:.1f} GB [profile: {profile_name}]{tags_str}"
        )
    return "\n".join(lines) or "No series found."


@mcp.tool()
def sonarr_get_series(series_id: int) -> str:
    """Get detailed info about a specific TV series by its Sonarr ID."""
    if series_id <= 0:
        return "Invalid series_id."
    s = _sonarr(f"/series/{series_id}")
    if isinstance(s, str):
        return s
    stats = s.get("statistics", {})
    profile_name = "?"
    try:
        for p in _sonarr("/qualityprofile"):
            if p["id"] == s.get("qualityProfileId"):
                profile_name = p["name"]
                break
    except Exception:
        pass
    tag_labels = _tag_labels(_sonarr)
    tag_names = [tag_labels.get(tid, str(tid)) for tid in s.get("tags", [])]
    lines = [
        f"Title: {s['title']} ({s.get('year', '?')})",
        f"Sonarr ID: {s['id']}",
        f"Status: {s.get('status', '?')}",
        f"Network: {s.get('network', '?')}",
        f"Quality Profile: [{s.get('qualityProfileId', '?')}] {profile_name}",
        f"Monitored: {s.get('monitored', False)}",
        f"Tags: {', '.join(tag_names) or 'none'}",
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
    data = _sonarr("/series/lookup", params={"term": term})
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
    if tvdb_id <= 0:
        return "Invalid tvdb_id."
    lookup = _sonarr("/series/lookup", params={"term": f"tvdb:{tvdb_id}"})
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
    data = _sonarr("/calendar", params={"start": start, "end": end, "includeSeries": "true"})
    if isinstance(data, str):
        return data
    lines = []
    for ep in data:
        series = ep.get("series", {}).get("title", "?")
        s_num = ep.get("seasonNumber", 0)
        e_num = ep.get("episodeNumber", 0)
        air = (ep.get("airDateUtc") or "?")[:10]
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
def sonarr_set_quality_profile_qualities(profile_id: int, allow: str = "", disallow: str = "") -> str:
    """Enable or disable specific qualities (or quality groups, e.g. "WEB 1080p")
    within a Sonarr quality profile. Use sonarr_list_quality_profiles first to
    see profile IDs and current quality/group names.

    Args:
        profile_id: Sonarr quality profile ID.
        allow: Comma-separated quality/group names to enable (e.g. "Bluray-1080p,WEB 1080p").
        disallow: Comma-separated quality/group names to disable.
    """
    if profile_id <= 0:
        return "Invalid profile_id."
    allow_lower = {n.strip().lower() for n in allow.split(",") if n.strip()}
    disallow_lower = {n.strip().lower() for n in disallow.split(",") if n.strip()}
    if not allow_lower and not disallow_lower:
        return "Provide at least one quality name in allow or disallow."
    overlap = allow_lower & disallow_lower
    if overlap:
        return f"Quality names can't be in both allow and disallow: {', '.join(sorted(overlap))}"

    profile = _sonarr(f"/qualityprofile/{profile_id}")
    if isinstance(profile, str):
        return profile

    changed, not_found = _apply_quality_allowlist(profile.get("items", []), allow_lower, disallow_lower)
    if changed:
        result = _sonarr(f"/qualityprofile/{profile_id}", method="PUT", json=profile)
        if isinstance(result, str):
            return result
        msg = f"✅ Updated '{profile.get('name', profile_id)}': {', '.join(changed)}"
    else:
        msg = "No changes made."
    if not_found:
        msg += f"\n⚠️ Not found in this profile: {', '.join(not_found)}"
    return msg


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
    if queue_id <= 0:
        return "Invalid queue_id."
    result = _sonarr(
        f"/queue/{queue_id}",
        method="DELETE",
        params={"removeFromClient": "true", "blocklist": str(blocklist).lower()},
    )
    if isinstance(result, str):
        return f"❌ Failed to remove queue item: {result}"
    return "✅ Removed from queue." + (" (blocklisted)" if blocklist else "")


@mcp.tool()
def sonarr_list_episode_files(series_id: int, season: int = -1) -> str:
    """List downloaded episode files for a series, with the file IDs used by sonarr_delete_episode_file.

    Args:
        series_id: Sonarr series ID (use sonarr_list_series to find it).
        season: Only show this season number (-1 for all seasons).
    """
    if series_id <= 0:
        return "Invalid series_id."
    data = _sonarr("/episodefile", params={"seriesId": series_id})
    if isinstance(data, str):
        return data
    files = [f for f in data if season < 0 or f.get("seasonNumber") == season]
    lines = []
    for f in sorted(files, key=lambda x: (x.get("seasonNumber", 0), x.get("relativePath") or "")):
        quality = ((f.get("quality") or {}).get("quality") or {}).get("name", "?")
        lines.append(
            f"• [fileId: {f.get('id', '?')}] S{f.get('seasonNumber', 0):02d} "
            f"{f.get('relativePath', '?')} — {quality}, {_format_size(f.get('size'))}"
        )
    return "\n".join(lines) or "No episode files found."


@mcp.tool()
def sonarr_delete_episode_file(episode_file_id: int) -> str:
    """Delete a downloaded episode file, marking it as missing in Sonarr.
    This allows Sonarr to re-search and download a new version.

    Args:
        episode_file_id: Episode file ID (use sonarr_list_episode_files to find it).
    """
    if episode_file_id <= 0:
        return "Invalid episode_file_id."
    result = _sonarr(f"/episodefile/{episode_file_id}", method="DELETE")
    if isinstance(result, str):
        return f"❌ Failed to delete episode file: {result}"
    return f"✅ Deleted episode file (id: {episode_file_id}). Episode is now marked as missing."


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


@mcp.tool()
def sonarr_update_series(series_id: int, quality_profile_id: int = -1, monitored: int = -1) -> str:
    """Update settings for a series in Sonarr (quality profile, monitored status, etc.).

    Args:
        series_id: Sonarr series ID (use sonarr_list_series or sonarr_get_series to find it).
        quality_profile_id: New quality profile ID (-1 to keep current).
        monitored: Set to 1 to monitor, 0 to unmonitor (-1 to keep current).
    """
    if series_id <= 0:
        return "Invalid series_id."
    series = _sonarr(f"/series/{series_id}")
    if isinstance(series, str):
        return series
    changes = []
    if quality_profile_id >= 0:
        series["qualityProfileId"] = quality_profile_id
        changes.append(f"qualityProfile → {quality_profile_id}")
    if monitored >= 0:
        series["monitored"] = bool(monitored)
        changes.append(f"monitored → {bool(monitored)}")
    if not changes:
        return "No changes specified."
    result = _sonarr(f"/series/{series_id}", method="PUT", json=series)
    if isinstance(result, dict):
        return f"✅ Updated '{result.get('title', '?')}': {', '.join(changes)}"
    return f"❌ Failed: {result}"


@mcp.tool()
def sonarr_list_tags() -> str:
    """List all tags defined in Sonarr, with how many series each is applied to.
    Sonarr/Seerr can auto-tag series by requesting user — useful for scoping
    actions to what a specific person requested."""
    data = _sonarr("/tag/detail")
    if isinstance(data, str):
        return data
    lines = [f"• [{t['id']}] {t['label']} — {len(t.get('seriesIds', []))} series" for t in data]
    return "\n".join(lines) or "No tags found."


@mcp.tool()
def sonarr_list_series_by_tag(tag: str) -> str:
    """List series in Sonarr that have a specific tag (e.g. a requester tag
    like "2 - bryan"). Use sonarr_list_tags to see available tag labels.

    Args:
        tag: Exact tag label to filter by (case-insensitive).
    """
    all_tags = _sonarr("/tag")
    if isinstance(all_tags, str):
        return all_tags
    ids, unmatched = _resolve_tag_ids(all_tags, tag)
    if unmatched:
        valid = ", ".join(t["label"] for t in all_tags)
        return f"Tag not found: {unmatched[0]}. Valid tags: {valid}"
    tag_id = ids[0]
    data = _sonarr("/series")
    if isinstance(data, str):
        return data
    matches = [s for s in data if tag_id in s.get("tags", [])]
    lines = [
        f"[id:{s['id']}] {s['title']} ({s.get('year', '?')})" for s in sorted(matches, key=lambda x: x["title"])
    ]
    return "\n".join(lines) or f"No series tagged '{tag}'."


@mcp.tool()
def sonarr_set_series_tags(series_id: int, tags: str, mode: str = "add") -> str:
    """Add, remove, or set tags on a series in Sonarr. Tags that don't exist
    yet are created automatically (matching add/set mode only).

    Args:
        series_id: Sonarr series ID (use sonarr_list_series or sonarr_get_series to find it).
        tags: Comma-separated tag labels (e.g. "2 - bryan,rewatch").
        mode: "add" to add to existing tags, "remove" to remove named tags,
            "set" to replace all tags with exactly these (default: "add").
    """
    if series_id <= 0:
        return "Invalid series_id."
    if mode not in ("add", "remove", "set"):
        return 'Invalid mode. Use "add", "remove", or "set".'
    tag_names = [n.strip() for n in tags.split(",") if n.strip()]
    if not tag_names:
        return "Provide at least one tag name."

    series = _sonarr(f"/series/{series_id}")
    if isinstance(series, str):
        return series
    all_tags = _sonarr("/tag")
    if isinstance(all_tags, str):
        return all_tags

    ids, unmatched = _resolve_tag_ids(all_tags, tags)
    if mode == "remove" and unmatched:
        return f"Tag(s) not found: {', '.join(unmatched)}"
    for label in unmatched:  # add/set: create any tag that doesn't exist yet
        created = _sonarr("/tag", method="POST", json={"label": label})
        if isinstance(created, str):
            return created
        ids.append(created["id"])

    current = set(series.get("tags", []))
    if mode == "add":
        series["tags"] = sorted(current | set(ids))
    elif mode == "remove":
        series["tags"] = sorted(current - set(ids))
    else:  # set
        series["tags"] = sorted(set(ids))

    result = _sonarr(f"/series/{series_id}", method="PUT", json=series)
    if isinstance(result, str):
        return result
    return f"✅ Tags on '{result.get('title') or series.get('title', series_id)}' ({mode}: {', '.join(tag_names)}): now {series['tags']}"


# ════════════════════════════════════════════════════════════════
#  Radarr Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def radarr_list_movies() -> str:
    """List all movies in Radarr with download status, disk usage, and tags."""
    data = _radarr("/movie")
    if isinstance(data, str):
        return data
    profiles = {}
    try:
        for p in _radarr("/qualityprofile"):
            profiles[p["id"]] = p["name"]
    except Exception:
        pass
    tag_labels = _tag_labels(_radarr)
    lines = []
    for m in sorted(data, key=lambda x: x["title"]):
        has_file = "✅" if m.get("hasFile") else "❌"
        monitored = "👁" if m.get("monitored") else "⏸️"
        size_gb = m.get("sizeOnDisk", 0) / 1e9
        profile_name = profiles.get(m.get("qualityProfileId"), "?")
        tags = ", ".join(tag_labels.get(tid, str(tid)) for tid in m.get("tags", []))
        tags_str = f" [tags: {tags}]" if tags else ""
        lines.append(
            f"{has_file}{monitored} [id:{m['id']}] {m['title']} ({m.get('year', '?')}) — "
            f"{size_gb:.1f} GB [profile: {profile_name}]{tags_str}"
        )
    return "\n".join(lines) or "No movies found."


@mcp.tool()
def radarr_get_movie(movie_id: int) -> str:
    """Get detailed info about a specific movie by its Radarr ID."""
    if movie_id <= 0:
        return "Invalid movie_id."
    m = _radarr(f"/movie/{movie_id}")
    if isinstance(m, str):
        return m
    profile_name = "?"
    try:
        for p in _radarr("/qualityprofile"):
            if p["id"] == m.get("qualityProfileId"):
                profile_name = p["name"]
                break
    except Exception:
        pass
    tag_labels = _tag_labels(_radarr)
    tag_names = [tag_labels.get(tid, str(tid)) for tid in m.get("tags", [])]
    lines = [
        f"Title: {m['title']} ({m.get('year', '?')})",
        f"Radarr ID: {m['id']}",
        f"Status: {m.get('status', '?')}",
        f"Studio: {m.get('studio', '?')}",
        f"Quality Profile: [{m.get('qualityProfileId', '?')}] {profile_name}",
        f"Has File: {m.get('hasFile', False)}",
        f"Monitored: {m.get('monitored', False)}",
        f"Tags: {', '.join(tag_names) or 'none'}",
        f"Size: {m.get('sizeOnDisk', 0) / 1e9:.1f} GB",
        f"Path: {m.get('path', '?')}",
        f"TMDB: {m.get('tmdbId', '?')} | IMDB: {m.get('imdbId', '?')}",
        f"Overview: {(m.get('overview') or 'N/A')[:300]}",
    ]
    return "\n".join(lines)


@mcp.tool()
def radarr_search(term: str) -> str:
    """Search for a movie to add to Radarr. Returns title, year, TMDB ID, and overview."""
    data = _radarr("/movie/lookup", params={"term": term})
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
    if tmdb_id <= 0:
        return "Invalid tmdb_id."
    lookup = _radarr("/movie/lookup/tmdb", params={"tmdbId": tmdb_id})
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
def radarr_set_quality_profile_qualities(profile_id: int, allow: str = "", disallow: str = "") -> str:
    """Enable or disable specific qualities (or quality groups, e.g. "WEB 1080p")
    within a Radarr quality profile. Use radarr_list_quality_profiles first to
    see profile IDs and current quality/group names.

    Args:
        profile_id: Radarr quality profile ID.
        allow: Comma-separated quality/group names to enable (e.g. "Bluray-1080p,WEB 1080p").
        disallow: Comma-separated quality/group names to disable.
    """
    if profile_id <= 0:
        return "Invalid profile_id."
    allow_lower = {n.strip().lower() for n in allow.split(",") if n.strip()}
    disallow_lower = {n.strip().lower() for n in disallow.split(",") if n.strip()}
    if not allow_lower and not disallow_lower:
        return "Provide at least one quality name in allow or disallow."
    overlap = allow_lower & disallow_lower
    if overlap:
        return f"Quality names can't be in both allow and disallow: {', '.join(sorted(overlap))}"

    profile = _radarr(f"/qualityprofile/{profile_id}")
    if isinstance(profile, str):
        return profile

    changed, not_found = _apply_quality_allowlist(profile.get("items", []), allow_lower, disallow_lower)
    if changed:
        result = _radarr(f"/qualityprofile/{profile_id}", method="PUT", json=profile)
        if isinstance(result, str):
            return result
        msg = f"✅ Updated '{profile.get('name', profile_id)}': {', '.join(changed)}"
    else:
        msg = "No changes made."
    if not_found:
        msg += f"\n⚠️ Not found in this profile: {', '.join(not_found)}"
    return msg


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
    if queue_id <= 0:
        return "Invalid queue_id."
    result = _radarr(
        f"/queue/{queue_id}",
        method="DELETE",
        params={"removeFromClient": "true", "blocklist": str(blocklist).lower()},
    )
    if isinstance(result, str):
        return f"❌ Failed to remove queue item: {result}"
    return "✅ Removed from queue." + (" (blocklisted)" if blocklist else "")


@mcp.tool()
def radarr_delete_movie_file(movie_id: int) -> str:
    """Delete the downloaded file for a movie, marking it as missing in Radarr.
    This allows Radarr to re-search and download a new version.

    Args:
        movie_id: Radarr movie ID (use radarr_list_movies or radarr_get_movie to find it).
    """
    if movie_id <= 0:
        return "Invalid movie_id."
    movie = _radarr(f"/movie/{movie_id}")
    if isinstance(movie, str):
        return movie
    movie_file = movie.get("movieFile")
    if not movie_file:
        return f"Movie '{movie.get('title', '?')}' has no file to delete."
    fid = movie_file.get("id")
    result = _radarr(f"/moviefile/{fid}", method="DELETE")
    if isinstance(result, str):
        return f"❌ Failed to delete movie file: {result}"
    return f"✅ Deleted file for '{movie.get('title', '?')}' (file id: {fid}). Movie is now marked as missing."


@mcp.tool()
def radarr_update_movie(movie_id: int, quality_profile_id: int = -1, monitored: int = -1) -> str:
    """Update settings for a movie in Radarr (quality profile, monitored status, etc.).

    Args:
        movie_id: Radarr movie ID (use radarr_list_movies or radarr_get_movie to find it).
        quality_profile_id: New quality profile ID (-1 to keep current).
        monitored: Set to 1 to monitor, 0 to unmonitor (-1 to keep current).
    """
    if movie_id <= 0:
        return "Invalid movie_id."
    movie = _radarr(f"/movie/{movie_id}")
    if isinstance(movie, str):
        return movie
    changes = []
    if quality_profile_id >= 0:
        movie["qualityProfileId"] = quality_profile_id
        changes.append(f"qualityProfile → {quality_profile_id}")
    if monitored >= 0:
        movie["monitored"] = bool(monitored)
        changes.append(f"monitored → {bool(monitored)}")
    if not changes:
        return "No changes specified."
    result = _radarr(f"/movie/{movie_id}", method="PUT", json=movie)
    if isinstance(result, dict):
        return f"✅ Updated '{result.get('title', '?')}': {', '.join(changes)}"
    return f"❌ Failed: {result}"


@mcp.tool()
def radarr_list_tags() -> str:
    """List all tags defined in Radarr, with how many movies each is applied to.
    Radarr/Seerr can auto-tag movies by requesting user — useful for scoping
    actions to what a specific person requested."""
    data = _radarr("/tag/detail")
    if isinstance(data, str):
        return data
    lines = [f"• [{t['id']}] {t['label']} — {len(t.get('movieIds', []))} movies" for t in data]
    return "\n".join(lines) or "No tags found."


@mcp.tool()
def radarr_list_movies_by_tag(tag: str) -> str:
    """List movies in Radarr that have a specific tag (e.g. a requester tag
    like "2 - bryan"). Use radarr_list_tags to see available tag labels.

    Args:
        tag: Exact tag label to filter by (case-insensitive).
    """
    all_tags = _radarr("/tag")
    if isinstance(all_tags, str):
        return all_tags
    ids, unmatched = _resolve_tag_ids(all_tags, tag)
    if unmatched:
        valid = ", ".join(t["label"] for t in all_tags)
        return f"Tag not found: {unmatched[0]}. Valid tags: {valid}"
    tag_id = ids[0]
    data = _radarr("/movie")
    if isinstance(data, str):
        return data
    matches = [m for m in data if tag_id in m.get("tags", [])]
    lines = [
        f"[id:{m['id']}] {m['title']} ({m.get('year', '?')})" for m in sorted(matches, key=lambda x: x["title"])
    ]
    return "\n".join(lines) or f"No movies tagged '{tag}'."


@mcp.tool()
def radarr_set_movie_tags(movie_id: int, tags: str, mode: str = "add") -> str:
    """Add, remove, or set tags on a movie in Radarr. Tags that don't exist
    yet are created automatically (matching add/set mode only).

    Args:
        movie_id: Radarr movie ID (use radarr_list_movies or radarr_get_movie to find it).
        tags: Comma-separated tag labels (e.g. "2 - bryan,rewatch").
        mode: "add" to add to existing tags, "remove" to remove named tags,
            "set" to replace all tags with exactly these (default: "add").
    """
    if movie_id <= 0:
        return "Invalid movie_id."
    if mode not in ("add", "remove", "set"):
        return 'Invalid mode. Use "add", "remove", or "set".'
    tag_names = [n.strip() for n in tags.split(",") if n.strip()]
    if not tag_names:
        return "Provide at least one tag name."

    movie = _radarr(f"/movie/{movie_id}")
    if isinstance(movie, str):
        return movie
    all_tags = _radarr("/tag")
    if isinstance(all_tags, str):
        return all_tags

    ids, unmatched = _resolve_tag_ids(all_tags, tags)
    if mode == "remove" and unmatched:
        return f"Tag(s) not found: {', '.join(unmatched)}"
    for label in unmatched:  # add/set: create any tag that doesn't exist yet
        created = _radarr("/tag", method="POST", json={"label": label})
        if isinstance(created, str):
            return created
        ids.append(created["id"])

    current = set(movie.get("tags", []))
    if mode == "add":
        movie["tags"] = sorted(current | set(ids))
    elif mode == "remove":
        movie["tags"] = sorted(current - set(ids))
    else:  # set
        movie["tags"] = sorted(set(ids))

    result = _radarr(f"/movie/{movie_id}", method="PUT", json=movie)
    if isinstance(result, str):
        return result
    return f"✅ Tags on '{result.get('title') or movie.get('title', movie_id)}' ({mode}: {', '.join(tag_names)}): now {movie['tags']}"


@mcp.tool()
def radarr_search_missing(movie_ids: str = "") -> str:
    """Trigger a search for missing movies in Radarr.

    Args:
        movie_ids: Comma-separated Radarr movie IDs to search. Leave empty to search all
            monitored movies that are missing (Radarr's own "Search All Missing").
    """
    if not movie_ids.strip():
        result = _radarr("/command", method="POST", json={"name": "MissingMoviesSearch"})
        if isinstance(result, dict):
            return "🔍 Search triggered for all monitored missing movies."
        return str(result)
    try:
        ids = [int(x) for x in (part.strip() for part in movie_ids.split(",")) if x]
    except ValueError:
        return "Invalid movie_ids: use comma-separated numeric Radarr IDs, e.g. \"12,34\"."
    if not ids or any(i <= 0 for i in ids):
        return "Invalid movie_ids: use comma-separated numeric Radarr IDs, e.g. \"12,34\"."
    result = _radarr("/command", method="POST", json={"name": "MoviesSearch", "movieIds": ids})
    if isinstance(result, dict):
        return f"🔍 Search triggered for {len(ids)} movie(s)."
    return str(result)


# ════════════════════════════════════════════════════════════════
#  Lidarr Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def lidarr_list_artists() -> str:
    """List all artists in Lidarr with monitoring status, album counts, and disk usage."""
    data = _lidarr("/artist")
    if isinstance(data, str):
        return data
    lines = []
    for a in sorted(data, key=lambda x: x.get("artistName", "")):
        stats = a.get("statistics", {})
        have = stats.get("trackFileCount", 0)
        total = stats.get("trackCount", 0)
        albums = stats.get("albumCount", 0)
        size_gb = stats.get("sizeOnDisk", 0) / 1e9
        icon = "✅" if a.get("monitored") else "⏸️"
        lines.append(
            f"{icon} [{a['id']}] {a.get('artistName', '?')} — "
            f"{albums} albums, {have}/{total} tracks, {size_gb:.1f} GB"
        )
    return "\n".join(lines) or "No artists found."


@mcp.tool()
def lidarr_get_artist(artist_id: int) -> str:
    """Get detailed info about a specific artist by their Lidarr ID."""
    if artist_id <= 0:
        return "Invalid artist_id."
    a = _lidarr(f"/artist/{artist_id}")
    if isinstance(a, str):
        return a
    stats = a.get("statistics", {})
    lines = [
        f"Name: {a.get('artistName', '?')}",
        f"Status: {a.get('status', '?')}",
        f"Monitored: {a.get('monitored', False)}",
        f"Albums: {stats.get('albumCount', 0)}",
        f"Tracks: {stats.get('trackFileCount', 0)}/{stats.get('trackCount', 0)}",
        f"Size: {stats.get('sizeOnDisk', 0) / 1e9:.1f} GB",
        f"Path: {a.get('path', '?')}",
        f"MusicBrainz: {a.get('foreignArtistId', '?')}",
        f"Overview: {(a.get('overview') or 'N/A')[:300]}",
    ]
    return "\n".join(lines)


@mcp.tool()
def lidarr_search(term: str) -> str:
    """Search for an artist to add to Lidarr. Returns artist name and MusicBrainz ID."""
    data = _lidarr("/artist/lookup", params={"term": term})
    if isinstance(data, str):
        return data
    lines = []
    for r in data[:10]:
        overview = (r.get("overview") or "No description.")[:150]
        lines.append(
            f"• {r.get('artistName', '?')} "
            f"[mbId: {r.get('foreignArtistId', '?')}]\n  {overview}"
        )
    return "\n".join(lines) or "No results found."


@mcp.tool()
def lidarr_search_album(term: str) -> str:
    """Search for an album in Lidarr's metadata source. Returns album title, artist, and MusicBrainz ID."""
    data = _lidarr("/album/lookup", params={"term": term})
    if isinstance(data, str):
        return data
    lines = []
    for r in data[:10]:
        title = r.get("title", "?")
        artist = r.get("artist", {}).get("artistName", "?") if isinstance(r.get("artist"), dict) else "?"
        release = (r.get("releaseDate") or "?")[:10]
        lines.append(
            f"• {title} — {artist} ({release}) "
            f"[mbId: {r.get('foreignAlbumId', '?')}]"
        )
    return "\n".join(lines) or "No results found."


@mcp.tool()
def lidarr_add_artist(
    artist_name: str,
    quality_profile_id: int,
    metadata_profile_id: int,
    root_folder_path: str,
    monitored: bool = True,
) -> str:
    """Add an artist to Lidarr by name. Use lidarr_search to find the artist first.

    Args:
        artist_name: Artist name to look up and add.
        quality_profile_id: Quality profile (use lidarr_list_quality_profiles).
        metadata_profile_id: Metadata profile (use lidarr_list_metadata_profiles).
        root_folder_path: Root folder path (use lidarr_list_root_folders).
        monitored: Whether to monitor the artist (default: True).
    """
    lookup = _lidarr("/artist/lookup", params={"term": artist_name})
    if isinstance(lookup, str):
        return lookup
    if not lookup:
        return "Artist not found."
    artist_data = lookup[0]
    artist_data.update(
        {
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"monitor": "all", "searchForMissingAlbums": True},
        }
    )
    result = _lidarr("/artist", method="POST", json=artist_data)
    if isinstance(result, dict):
        return f"✅ Added: {result.get('artistName', '?')}"
    return str(result)


@mcp.tool()
def lidarr_list_quality_profiles() -> str:
    """List all quality profiles in Lidarr with their allowed qualities."""
    data = _lidarr("/qualityprofile")
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
            f"• [{p['id']}] {p['name']} — Allowed: {', '.join(qualities) or 'none'}"
        )
    return "\n".join(lines) or "No quality profiles found."


@mcp.tool()
def lidarr_list_metadata_profiles() -> str:
    """List all metadata profiles in Lidarr (controls which release types/secondary types are tracked)."""
    data = _lidarr("/metadataprofile")
    if isinstance(data, str):
        return data
    lines = []
    for p in data:
        lines.append(f"• [{p['id']}] {p.get('name', '?')}")
    return "\n".join(lines) or "No metadata profiles found."


@mcp.tool()
def lidarr_list_root_folders() -> str:
    """List all configured root folders in Lidarr with free space."""
    data = _lidarr("/rootfolder")
    if isinstance(data, str):
        return data
    lines = []
    for r in data:
        free_gb = r.get("freeSpace", 0) / 1e9
        lines.append(f"• [{r.get('id', '?')}] {r.get('path', '?')} — {free_gb:.1f} GB free")
    return "\n".join(lines) or "No root folders configured."


@mcp.tool()
def lidarr_queue() -> str:
    """Show the current Lidarr download queue with status and queue IDs for each item."""
    data = _lidarr("/queue", params={"pageSize": 50, "includeUnknownArtistItems": "true"})
    if isinstance(data, str):
        return data
    records = data.get("records", []) if isinstance(data, dict) else []
    lines = []
    for r in records:
        title = r.get("title", "?")
        status = r.get("status", "?")
        sizeleft = r.get("sizeleft", 0) / 1e9
        lines.append(f"• [queueId: {r['id']}] {title} — {status} ({sizeleft:.1f} GB remaining)")
    return "\n".join(lines) or "Queue is empty."


@mcp.tool()
def lidarr_delete_queue_item(queue_id: int, blocklist: bool = True) -> str:
    """Remove an item from the Lidarr download queue.

    Args:
        queue_id: Queue item ID (use lidarr_queue to find it).
        blocklist: If True, adds the release to the blocklist so it won't be grabbed again.
    """
    if queue_id <= 0:
        return "Invalid queue_id."
    result = _lidarr(
        f"/queue/{queue_id}",
        method="DELETE",
        params={"removeFromClient": "true", "blocklist": str(blocklist).lower()},
    )
    if isinstance(result, str):
        return f"❌ Failed to remove queue item: {result}"
    return "✅ Removed from queue." + (" (blocklisted)" if blocklist else "")


@mcp.tool()
def lidarr_search_missing() -> str:
    """Trigger a search for all missing albums in Lidarr."""
    result = _lidarr("/command", method="POST", json={"name": "MissingAlbumSearch"})
    if isinstance(result, dict):
        return "🔍 Search triggered for all missing albums."
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
    if indexer_id <= 0:
        return "Invalid indexer_id."
    indexer = _prowlarr(f"/indexer/{indexer_id}")
    if isinstance(indexer, str):
        return f"❌ Indexer test failed: {indexer}"
    result = _prowlarr("/indexer/test", method="POST", json=indexer, params={"forceTest": "true"})
    if isinstance(result, str):
        return f"❌ Indexer test failed: {result}"
    return "✅ Indexer test passed."


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
        result = _prowlarr("/indexer/test", method="POST", json=idx, params={"forceTest": "true"})
        if isinstance(result, str):
            results.append(f"❌ {idx['name']} — {result}")
        else:
            results.append(f"✅ {idx['name']} — OK")
    return "\n".join(results) or "No enabled indexers."


_prowlarr_search_cache: list = []


@mcp.tool()
def prowlarr_search(query: str, indexer_ids: str = "") -> str:
    """Search across Prowlarr indexers for releases.

    Returns numbered results with guid for use with prowlarr_grab.

    Args:
        query: Search term.
        indexer_ids: Comma-separated indexer IDs to search (empty = all).
    """
    global _prowlarr_search_cache
    params = {"query": query, "type": "search"}
    if indexer_ids:
        params["indexerIds"] = indexer_ids
    data = _prowlarr("/search", params=params)
    if isinstance(data, str):
        return data
    _prowlarr_search_cache = data[:25]
    lines = []
    for i, r in enumerate(data[:25]):
        title = r.get("title", "?")
        size_gb = r.get("size", 0) / 1e9
        seeders = r.get("seeders", "?")
        indexer = r.get("indexer", "?")
        lines.append(
            f"[{i}] {title} — {size_gb:.1f} GB, {seeders} seeds [{indexer}]"
        )
    return "\n".join(lines) or "No results."


@mcp.tool()
def prowlarr_grab(index: int) -> str:
    """Grab a release from the most recent prowlarr_search results and send it to the download client (qBittorrent).

    Run prowlarr_search first, then use the [index] number from those results here.

    Args:
        index: The result number from prowlarr_search (e.g. 0 for the first result).
    """
    global _prowlarr_search_cache
    if not _prowlarr_search_cache:
        return "❌ No cached search results. Run prowlarr_search first."
    if index < 0 or index >= len(_prowlarr_search_cache):
        return f"❌ Invalid index {index}. Valid range: 0–{len(_prowlarr_search_cache) - 1}."

    release = _prowlarr_search_cache[index]
    title = release.get("title", "?")
    indexer_id = release.get("indexerId")
    guid = release.get("guid", "")

    # Use Prowlarr's download proxy URL if available, fall back to magnetUrl then guid
    download_url = release.get("downloadUrl") or release.get("magnetUrl") or guid
    if not download_url:
        return f"❌ No download URL found for [{index}] {title}."

    # If it's a magnet link, send straight to qBittorrent
    if download_url.startswith("magnet:"):
        ok, detail = _qbt_add_url(download_url)
        if ok:
            return f"✅ Sent magnet to qBittorrent: {title}"
        return f"❌ Failed to add magnet to qBittorrent: {detail}"

    # For .torrent download URLs (nCore, etc.), download via Prowlarr proxy then send to qBittorrent
    kind, value = _qbt_fetch_torrent(download_url)
    if kind == "error":
        return f"❌ Failed to grab release: {value}"
    if kind == "magnet":
        ok, detail = _qbt_add_url(value)
        if ok:
            return f"✅ Sent magnet to qBittorrent: {title}"
        return f"❌ Failed to add magnet to qBittorrent: {detail}"
    _, content = value
    ok, detail = _qbt_add_file(f"{title}.torrent", content)
    if ok:
        return f"✅ Downloaded and sent to qBittorrent: {title}"
    return f"❌ Failed to send torrent to qBittorrent: {detail}"


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
#
# Helpers below resolve "anything the user hands us" — a magnet link, a remote
# .torrent URL, a local .torrent path, or base64 torrent data — into a single
# qBittorrent /torrents/add call. The public tools are thin wrappers over them.

# Sanity ceiling for a remote .torrent download. Real torrent files are at most
# a few MB even for very large multi-file releases; anything bigger is a wrong
# URL (an HTML page, a media file, …), so we refuse it with a clear message.
MAX_TORRENT_BYTES = 25 * 1024 * 1024
# Bound on redirect hops when fetching a remote .torrent (we follow them by hand
# so a redirect to a magnet: URI is captured rather than crashing httpx).
MAX_TORRENT_REDIRECTS = 5


def _qbt_add_options(category="", save_path="", paused=False):
    """Build the optional form fields shared by every /torrents/add call."""
    opts = {}
    if category:
        opts["category"] = category
    if save_path:
        opts["savepath"] = save_path
        # Turn off Automatic Torrent Management so the explicit path is honored.
        opts["autoTMM"] = "false"
    if paused:
        # qBittorrent <5 uses "paused"; 5.x renamed it to "stopped". Send both so
        # the torrent is added stopped regardless of the server version.
        opts["paused"] = "true"
        opts["stopped"] = "true"
    return opts


def _qbt_add_result(result):
    """Normalize a /torrents/add response into (ok, detail)."""
    if isinstance(result, str):
        text = result.strip()
        if text.lower() == "ok.":
            return True, ""
        if text.lower() == "fails.":
            return False, "qBittorrent rejected it (duplicate or invalid torrent)."
        if not text:
            return False, "qBittorrent returned an empty response."
        # _qbt returns plain-text error messages (not configured, HTTP errors, …).
        return False, text
    if isinstance(result, dict):
        # Newer WebUI API versions answer /torrents/add with JSON counts.
        if result.get("success_count", 0) > 0 or result.get("added_torrent_ids"):
            return True, ""
        if result.get("failure_count", 0) > 0:
            return False, "qBittorrent rejected it (duplicate or invalid torrent)."
    return False, str(result)


def _qbt_add_url(url, category="", save_path="", paused=False):
    """Add a magnet link or remote .torrent URL via the `urls` field."""
    data = {"urls": url}
    data.update(_qbt_add_options(category, save_path, paused))
    return _qbt_add_result(_qbt("/torrents/add", method="POST", data=data))


def _qbt_add_file(filename, content, category="", save_path="", paused=False):
    """Upload raw .torrent bytes via the multipart `torrents` field."""
    files = {"torrents": (filename or "upload.torrent", content, "application/x-bittorrent")}
    return _qbt_add_result(
        _qbt(
            "/torrents/add",
            method="POST",
            data=_qbt_add_options(category, save_path, paused),
            files=files,
        )
    )


def _looks_like_torrent(content):
    """Cheap sanity check that bytes are a bencoded .torrent file.

    A valid torrent is a bencoded dict (starts with ``d``) with a mandatory
    top-level ``info`` key (bencoded as ``4:info``). ``announce`` is optional
    (trackerless/DHT torrents omit it). Scan a generous prefix so the ``info``
    key is found even after a large ``announce-list``.
    """
    if not content or content[:1] != b"d":
        return False
    return b"4:info" in content


def _decode_b64_torrent(text):
    """Decode base64 (optionally a data: URI) into .torrent bytes, or None."""
    raw = text.strip()
    if raw.lower().startswith("data:"):
        _, _, raw = raw.partition(",")
    raw = "".join(raw.split())
    if len(raw) < 16:
        return None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return decoded if _looks_like_torrent(decoded) else None


def _filename_from_url(url):
    """Best-effort .torrent filename derived from a URL path."""
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1] or "")
    if name and not name.lower().endswith(".torrent"):
        name = f"{name}.torrent"
    return name or "download.torrent"


def _magnet_display_name(magnet):
    """Human-readable name for a magnet link (dn= param, else btih hash)."""
    qs = parse_qs(urlparse(magnet).query)
    if qs.get("dn"):
        return qs["dn"][0]
    for xt in qs.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            return f"magnet ({xt.split(':')[-1][:16]}…)"
    return "magnet link"


def _redact_url(url):
    """Return just the host of a URL, dropping path, query and userinfo.

    Tracker / Prowlarr download links carry apikey/passkey/token secrets in the
    query string *and sometimes in the path itself* (e.g. private trackers like
    nCore use ``/download/<passkey>/name.torrent``), so only the hostname is
    safe to echo back through tool output or logs. A trailing ``/…`` signals a
    path/query was present without revealing it.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "the provided URL"
    host = parsed.hostname or ""
    if not host:
        return "the provided URL"
    if (parsed.path and parsed.path != "/") or parsed.query:
        return f"{host}/…"
    return host


def _filename_from_disposition(disposition):
    """Best-effort filename from a Content-Disposition header (RFC 5987 aware)."""
    params = {}
    for part in disposition.split(";")[1:]:
        key, sep, value = part.partition("=")
        if sep:
            params[key.strip().lower()] = value.strip()
    if params.get("filename*"):
        # e.g. filename*=UTF-8''My%20Release.torrent
        value = params["filename*"].split("''", 1)[-1]
        return unquote(value).strip('"').strip("'")
    return params.get("filename", "").strip('"').strip("'")


def _qbt_fetch_torrent(url):
    """Download a remote .torrent, following redirects manually.

    Redirects are followed by hand (rather than via httpx ``follow_redirects``)
    so a tracker that 302-redirects to a ``magnet:`` URI is captured instead of
    crashing httpx with an unsupported-scheme error. Returns
    ("file", (name, bytes)), ("magnet", uri), or ("error", message). URLs are
    redacted in error messages because tracker/Prowlarr download links often
    embed apikey/passkey tokens.
    """
    current = url
    try:
        for _ in range(MAX_TORRENT_REDIRECTS + 1):
            r = httpx.get(current, timeout=30, follow_redirects=False)
            if r.is_redirect:
                location = r.headers.get("location", "")
                if location.lower().startswith("magnet:"):
                    return "magnet", location.strip()
                if not location:
                    return "error", f"{_redact_url(current)} returned a redirect with no location."
                current = urljoin(current, location)
                continue
            r.raise_for_status()
            break
        else:
            return "error", f"Too many redirects fetching torrent from {_redact_url(url)}."
    except httpx.HTTPStatusError as error:
        return "error", f"Could not download torrent (HTTP {error.response.status_code}) from {_redact_url(current)}."
    except httpx.RequestError as error:
        return "error", f"Could not download torrent from {_redact_url(current)} ({type(error).__name__})."
    content = r.content or b""
    if len(content) > MAX_TORRENT_BYTES:
        size_mb = len(content) // (1024 * 1024)
        return "error", f"{_redact_url(current)} returned {size_mb} MB — too large to be a .torrent file."
    if content[:7].lower() == b"magnet:":
        return "magnet", content.strip().decode("utf-8", "ignore")
    if not _looks_like_torrent(content):
        return "error", f"{_redact_url(current)} did not return a valid .torrent file."
    name = _filename_from_url(current)
    fn = _filename_from_disposition(r.headers.get("content-disposition", ""))
    if fn:
        name = fn if fn.lower().endswith(".torrent") else f"{fn}.torrent"
    return "file", (name, content)


def _qbt_resolve_source(source):
    """Classify an arbitrary `source` string for adding to qBittorrent.

    Returns one of:
      ("magnet", magnet_uri)
      ("file", (filename, content_bytes))
      ("error", message)
    """
    src = (source or "").strip()
    if not src:
        return "error", "No source provided."
    low = src.lower()
    if low.startswith("magnet:"):
        return "magnet", src
    if low.startswith(("http://", "https://")):
        return _qbt_fetch_torrent(src)
    # Only files named *.torrent are read from the server's disk. Anything else
    # falls through to the generic "unrecognized" error, so this tool can't be
    # used to read arbitrary files or to probe which paths exist on the host.
    if low.endswith(".torrent") and os.path.isfile(src):
        try:
            with open(src, "rb") as fh:
                content = fh.read()
        except OSError as error:
            return "error", f"Could not read file: {error}"
        if not _looks_like_torrent(content):
            return "error", f"{os.path.basename(src)} is not a valid .torrent file."
        return "file", (os.path.basename(src), content)
    decoded = _decode_b64_torrent(src)
    if decoded is not None:
        return "file", ("upload.torrent", decoded)
    return (
        "error",
        "Unrecognized source. Provide a magnet link, an http(s) .torrent URL, "
        "a local .torrent file path, or base64-encoded torrent data.",
    )


def _qbt_add_source(source, category="", save_path="", paused=False):
    """Resolve `source` then add it. Returns a user-facing status string."""
    kind, value = _qbt_resolve_source(source)
    if kind == "error":
        return f"❌ {value}"
    if kind == "magnet":
        ok, detail = _qbt_add_url(value, category, save_path, paused)
        name = _magnet_display_name(value)
    else:
        name, content = value
        ok, detail = _qbt_add_file(name, content, category, save_path, paused)
    if ok:
        cat = f" → {category}" if category else ""
        state = " (added stopped)" if paused else ""
        return f"✅ Added to qBittorrent{cat}{state}: {name}"
    return f"❌ Failed to add to qBittorrent: {detail}"


@mcp.tool()
def qbt_list_torrents(filter: str = "all") -> str:
    """List torrents in qBittorrent.

    Args:
        filter: Filter torrents — "all", "downloading", "seeding", "completed", "paused", "active", "stalled".
    """
    data = _qbt("/torrents/info", params={"filter": filter})
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
    props = _qbt("/torrents/properties", params={"hash": torrent_hash})
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
def qbt_add(source: str, category: str = "", save_path: str = "", paused: bool = False) -> str:
    """Add anything to qBittorrent and start downloading immediately.

    This is the one-shot "just download it" tool. Hand it a `source` and it
    auto-detects what it is:
      • a magnet link (magnet:?xt=urn:btih:...)
      • an http(s):// URL pointing to a .torrent file
      • a path to a local .torrent file on the server
      • base64-encoded .torrent file contents (optionally a data: URI)

    Args:
        source: A magnet link, .torrent URL, local .torrent path, or base64 torrent data.
        category: Optional qBittorrent category to assign (e.g. "tv", "movies").
        save_path: Optional download directory; defaults to the category/global path.
        paused: If True, add the torrent without starting it.
    """
    return _qbt_add_source(source, category=category, save_path=save_path, paused=paused)


@mcp.tool()
def qbt_add_magnet(magnet_url: str, category: str = "", save_path: str = "", paused: bool = False) -> str:
    """Add a magnet link to qBittorrent.

    For .torrent files or URLs, use qbt_add_torrent_file (or qbt_add, which
    accepts any source).

    Args:
        magnet_url: The magnet URI to add (must start with "magnet:").
        category: Optional category to assign (e.g. "tv", "movies").
        save_path: Optional download directory; defaults to the category/global path.
        paused: If True, add the torrent without starting it.
    """
    if not magnet_url.strip().lower().startswith("magnet:"):
        return "❌ That doesn't look like a magnet link (it must start with 'magnet:'). Use qbt_add for files or URLs."
    magnet = magnet_url.strip()
    ok, detail = _qbt_add_url(magnet, category, save_path, paused)
    if ok:
        cat = f" → {category}" if category else ""
        state = " (added stopped)" if paused else ""
        return f"✅ Magnet added to qBittorrent{cat}{state}: {_magnet_display_name(magnet)}"
    return f"❌ Failed to add magnet: {detail}"


@mcp.tool()
def qbt_add_torrent_file(source: str, category: str = "", save_path: str = "", paused: bool = False) -> str:
    """Add a .torrent file to qBittorrent.

    `source` may be a local file path on the server, an http(s):// URL to a
    .torrent file (downloaded and uploaded for you), or base64-encoded .torrent
    contents. For magnet links use qbt_add_magnet (or qbt_add for anything).

    Args:
        source: Local .torrent path, .torrent URL, or base64 torrent data.
        category: Optional category to assign (e.g. "tv", "movies").
        save_path: Optional download directory; defaults to the category/global path.
        paused: If True, add the torrent without starting it.
    """
    if source.strip().lower().startswith("magnet:"):
        return "❌ That's a magnet link — use qbt_add_magnet (or qbt_add) instead."
    return _qbt_add_source(source, category=category, save_path=save_path, paused=paused)


def _action_ok(result) -> bool:
    """True when a qBittorrent-style action call succeeded.

    Action endpoints answer with an empty body or "Ok." on success; the
    request helpers return a non-empty message string on any failure
    (not configured, login/auth failure, HTTP error, ...).
    """
    if isinstance(result, str):
        text = result.strip()
        return text == "" or text.lower() == "ok."
    return True


def _qbt_action(path: str, legacy_path: str, data: dict):
    """POST a qBittorrent action, falling back to the pre-5.0 endpoint name.

    qBittorrent 5.x renamed /torrents/pause|resume to /torrents/stop|start;
    4.x answers the new names with 404.
    """
    result = _qbt(path, method="POST", data=data)
    if not _action_ok(result) and isinstance(result, str) and "HTTP 404" in result:
        result = _qbt(legacy_path, method="POST", data=data)
    return result


@mcp.tool()
def qbt_pause(torrent_hash: str) -> str:
    """Pause a torrent. Use 'all' to pause everything.

    Args:
        torrent_hash: Hash of the torrent, "hash1|hash2", or "all" to pause all.
    """
    if not torrent_hash.strip():
        return "❌ No torrent hash given."
    result = _qbt_action("/torrents/stop", "/torrents/pause", {"hashes": torrent_hash.strip()})
    if _action_ok(result):
        return "⏸️ Paused."
    return f"❌ Pause failed: {result}"


@mcp.tool()
def qbt_resume(torrent_hash: str) -> str:
    """Resume a torrent. Use 'all' to resume everything.

    Args:
        torrent_hash: Hash of the torrent, "hash1|hash2", or "all" to resume all.
    """
    if not torrent_hash.strip():
        return "❌ No torrent hash given."
    result = _qbt_action("/torrents/start", "/torrents/resume", {"hashes": torrent_hash.strip()})
    if _action_ok(result):
        return "▶️ Resumed."
    return f"❌ Resume failed: {result}"


@mcp.tool()
def qbt_delete(torrent_hash: str, delete_files: bool = False) -> str:
    """Delete a torrent from qBittorrent.

    Args:
        torrent_hash: Hash of the torrent to delete (or "hash1|hash2").
        delete_files: If True, also delete downloaded files from disk.
    """
    if not torrent_hash.strip():
        return "❌ No torrent hash given."
    result = _qbt(
        "/torrents/delete",
        method="POST",
        data={"hashes": torrent_hash.strip(), "deleteFiles": str(delete_files).lower()},
    )
    if not _action_ok(result):
        return f"❌ Delete failed: {result}"
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
#  RDTClient Tools
# ════════════════════════════════════════════════════════════════
#
# RDTClient (https://github.com/rogerfar/rdt-client) is a Real-Debrid /
# AllDebrid / Premiumize download manager that exposes a qBittorrent-compatible
# API at /api/v2 plus a native /api surface. We use the qBT-compat surface for
# everything the *arr stack already understands, and reach into the native API
# for things qBT doesn't model (e.g. provider settings).


@mcp.tool()
def rdt_list_torrents(filter: str = "all") -> str:
    """List torrents in RDTClient.

    Args:
        filter: Filter — "all", "downloading", "seeding", "completed", "paused", "active", "stalled".
    """
    data = _rdt("/torrents/info", params={"filter": filter})
    if isinstance(data, str):
        return data
    if not data:
        return "No torrents."
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
    return "\n".join(lines)


@mcp.tool()
def rdt_torrent_details(torrent_hash: str) -> str:
    """Get detailed info about a specific RDTClient torrent by its hash.

    Args:
        torrent_hash: The info hash of the torrent.
    """
    props = _rdt("/torrents/properties", params={"hash": torrent_hash})
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
def rdt_add_magnet(magnet: str, category: str = "") -> str:
    """Add a magnet link to RDTClient (which sends it to your debrid provider).

    Args:
        magnet: The magnet URI to add.
        category: Optional category to assign (e.g. "tv", "movies").
    """
    magnet = magnet.strip()
    if not magnet.lower().startswith("magnet:"):
        return "❌ That doesn't look like a magnet link (it must start with 'magnet:')."
    data = {"urls": magnet}
    if category:
        data["category"] = category
    result = _rdt("/torrents/add", method="POST", data=data)
    if _action_ok(result):
        return "✅ Torrent added to RDTClient."
    return f"❌ Failed to add to RDTClient: {result}"


def _rdt_simple_action(path: str, torrent_hashes: str, success: str, verb: str, extra=None) -> str:
    if not torrent_hashes.strip():
        return "❌ No torrent hash given."
    data = {"hashes": torrent_hashes.strip()}
    data.update(extra or {})
    result = _rdt(path, method="POST", data=data)
    if _action_ok(result):
        return success
    return f"❌ {verb} failed: {result}"


@mcp.tool()
def rdt_pause(torrent_hashes: str) -> str:
    """Pause one or more RDTClient torrents. Pipe-separate hashes, or use 'all'.

    Args:
        torrent_hashes: Hash, "hash1|hash2", or "all".
    """
    return _rdt_simple_action("/torrents/pause", torrent_hashes, "⏸️ Paused.", "Pause")


@mcp.tool()
def rdt_resume(torrent_hashes: str) -> str:
    """Resume one or more RDTClient torrents. Pipe-separate hashes, or use 'all'.

    Args:
        torrent_hashes: Hash, "hash1|hash2", or "all".
    """
    return _rdt_simple_action("/torrents/resume", torrent_hashes, "▶️ Resumed.", "Resume")


@mcp.tool()
def rdt_delete(torrent_hashes: str, delete_files: bool = False) -> str:
    """Delete one or more RDTClient torrents. Pipe-separate hashes, or use 'all'.

    Args:
        torrent_hashes: Hash, "hash1|hash2", or "all".
        delete_files: If True, also delete downloaded files from disk.
    """
    success = "🗑️ Deleted." + (" (files removed)" if delete_files else " (files kept)")
    return _rdt_simple_action(
        "/torrents/delete",
        torrent_hashes,
        success,
        "Delete",
        {"deleteFiles": str(delete_files).lower()},
    )


@mcp.tool()
def rdt_provider_status() -> str:
    """Show Real-Debrid / AllDebrid / Premiumize provider status from RDTClient.

    Reads from RDTClient's native /api/Settings endpoint to surface the configured
    debrid provider. Requires the same auth as the qBT-compat API; if your
    RDTClient install requires login, set RDT_USER/RDT_PASS.
    """
    data = _rdt_native("/api/Settings")
    if isinstance(data, str):
        return data
    provider = "?"
    keys_of_interest = {}
    if isinstance(data, dict):
        groups = data.get("settings") if isinstance(data.get("settings"), list) else None
        if groups:
            for g in groups:
                if str(g.get("key", "")).lower() == "provider":
                    for child in g.get("children", []) or []:
                        k = child.get("key")
                        v = child.get("value")
                        if k:
                            keys_of_interest[k] = v
                            if k.lower() == "provider":
                                provider = v
        else:
            provider = data.get("Provider") or data.get("provider") or "?"
    lines = [f"Provider: {provider}"]
    for k, v in keys_of_interest.items():
        if k.lower() == "provider":
            continue
        if any(s in k.lower() for s in ("token", "key", "password", "apikey")):
            v = "***" if v else "(unset)"
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


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
    data = _jellyfin("/Items/Latest", params={"Limit": limit, "EnableImages": "false"})
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
            headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'},
            timeout=30,
        )
        r.raise_for_status()
        return "✅ Library scan triggered."
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        return _http_error("jellyfin", error)


# ════════════════════════════════════════════════════════════════
#  Seerr Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def seerr_search(term: str) -> str:
    """Search Seerr for movies and TV shows. Returns title, year, media type,
    TMDB ID, and overview — use the TMDB ID with seerr_request_movie /
    seerr_request_tv to request it."""
    data = _seerr("/search", params={"query": term})
    if isinstance(data, str):
        return data
    lines = []
    for r in data.get("results", [])[:10]:
        media_type = r.get("mediaType")
        if media_type not in ("movie", "tv"):
            continue
        title = r.get("title") or r.get("name") or "?"
        date = r.get("releaseDate") or r.get("firstAirDate") or ""
        year = date[:4] if date else "?"
        overview = (r.get("overview") or "No description.")[:150]
        lines.append(
            f"• {title} ({year}) [tmdbId: {r.get('id', '?')}, type: {media_type}]\n  {overview}"
        )
    return "\n".join(lines) or "No results found."


@mcp.tool()
def seerr_request_movie(tmdb_id: int) -> str:
    """Request a movie in Seerr by its TMDB ID. Use seerr_search to find the TMDB ID first.

    Args:
        tmdb_id: The TMDB ID of the movie.
    """
    if tmdb_id <= 0:
        return "Invalid tmdb_id."
    data = _seerr("/request", method="POST", json={"mediaType": "movie", "mediaId": tmdb_id})
    if isinstance(data, str):
        return data
    status = data.get("status", "?")
    return f"✅ Requested movie (tmdbId: {tmdb_id}). Request id: {data.get('id', '?')}, status: {status}."


@mcp.tool()
def seerr_request_tv(tmdb_id: int, seasons: str = "all") -> str:
    """Request a TV show in Seerr by its TMDB ID. Use seerr_search to find the TMDB ID first.

    Args:
        tmdb_id: The TMDB ID of the show.
        seasons: "all" for every season, or a comma-separated list of season
            numbers (e.g. "1,2,3").
    """
    if tmdb_id <= 0:
        return "Invalid tmdb_id."
    if seasons.strip().lower() == "all":
        season_payload = "all"
    else:
        try:
            season_payload = [int(s.strip()) for s in seasons.split(",") if s.strip()]
        except ValueError:
            return "Invalid seasons. Use \"all\" or a comma-separated list like \"1,2,3\"."
        if not season_payload:
            return "Invalid seasons. Use \"all\" or a comma-separated list like \"1,2,3\"."
    data = _seerr(
        "/request",
        method="POST",
        json={"mediaType": "tv", "mediaId": tmdb_id, "seasons": season_payload},
    )
    if isinstance(data, str):
        return data
    status = data.get("status", "?")
    return f"✅ Requested TV show (tmdbId: {tmdb_id}, seasons: {seasons}). Request id: {data.get('id', '?')}, status: {status}."


@mcp.tool()
def seerr_list_requests(filter: str = "pending") -> str:
    """List media requests in Seerr.

    Args:
        filter: One of "all", "approved", "available", "pending", "processing",
            "unavailable", "failed" (default: "pending").
    """
    data = _seerr("/request", params={"filter": filter, "take": 20})
    if isinstance(data, str):
        return data
    results = data.get("results", [])
    lines = []
    for r in results:
        media = r.get("media", {})
        lines.append(
            f"• request id: {r.get('id', '?')} | type: {media.get('mediaType', '?')} "
            f"| tmdbId: {media.get('tmdbId', '?')} | status: {r.get('status', '?')} "
            f"| requested by: {r.get('requestedBy', {}).get('displayName', '?')}"
        )
    return "\n".join(lines) or f"No {filter} requests."


@mcp.tool()
def seerr_request_status(request_id: int) -> str:
    """Get the status of a specific Seerr request.

    Args:
        request_id: Seerr request ID (use seerr_list_requests to find it).
    """
    if request_id <= 0:
        return "Invalid request_id."
    data = _seerr(f"/request/{request_id}")
    if isinstance(data, str):
        return data
    media = data.get("media", {})
    return (
        f"Request id: {data.get('id', '?')}\n"
        f"Type: {media.get('mediaType', '?')}\n"
        f"TMDB ID: {media.get('tmdbId', '?')}\n"
        f"Status: {data.get('status', '?')}\n"
        f"Media status: {media.get('status', '?')}\n"
        f"Requested by: {data.get('requestedBy', {}).get('displayName', '?')}\n"
        f"Created: {data.get('createdAt', '?')}"
    )



# ════════════════════════════════════════════════════════════════
#  RomM Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def romm_system_info() -> str:
    """Show the RomM version, detected library platforms, and enabled metadata sources."""
    data = _romm("/api/heartbeat", public=True)
    if isinstance(data, str):
        return data
    sources = data.get("METADATA_SOURCES", {})
    enabled_sources = [
        name.removesuffix("_API_ENABLED")
        for name, enabled in sources.items()
        if name.endswith("_API_ENABLED") and enabled
    ]
    platforms = data.get("FILESYSTEM", {}).get("FS_PLATFORMS", [])
    return "\n".join(
        [
            f"Version: {data.get('SYSTEM', {}).get('VERSION', '?')}",
            f"Library platforms: {', '.join(platforms) or 'none'}",
            f"Metadata sources: {', '.join(enabled_sources) or 'none'}",
        ]
    )


@mcp.tool()
def romm_list_platforms() -> str:
    """List RomM platforms with their IDs, ROM counts, and library sizes."""
    data = _romm("/api/platforms")
    if isinstance(data, str):
        return data
    lines = [
        (
            f"• [{platform.get('id', '?')}] {platform.get('display_name') or platform.get('name', '?')} "
            f"({platform.get('fs_slug', '?')}) — {platform.get('rom_count', 0)} ROMs, "
            f"{_format_size(platform.get('fs_size_bytes'))}"
        )
        for platform in data
    ]
    return "\n".join(lines) or "No RomM platforms found."


@mcp.tool()
def romm_list_games(search: str = "", platform_id: int = 0, limit: int = 25) -> str:
    """List or search games already indexed in RomM.

    Args:
        search: Optional game-title search term.
        platform_id: Optional RomM platform ID from romm_list_platforms.
        limit: Number of games to return (default: 25, max: 100).
    """
    params = {
        "limit": max(1, min(limit, 100)),
        "with_char_index": False,
        "with_filter_values": False,
    }
    if search:
        params["search_term"] = search
    if platform_id > 0:
        params["platform_ids"] = platform_id
    data = _romm("/api/roms", params=params)
    if isinstance(data, str):
        return data
    games = data.get("items", [])
    lines = [
        (
            f"• [{game.get('id', '?')}] {game.get('name') or game.get('fs_name_no_ext', '?')} "
            f"[{game.get('platform_display_name') or game.get('platform_fs_slug', '?')}] — "
            f"{_format_size(game.get('fs_size_bytes'))}"
        )
        for game in games
    ]
    summary = f"Showing {len(games)} of {data.get('total', len(games))} RomM games."
    return "\n".join([summary, *lines])


@mcp.tool()
def romm_get_game(game_id: int) -> str:
    """Get details for a RomM game by its internal ID."""
    if game_id < 1:
        return "RomM game ID must be at least 1."
    game = _romm(f"/api/roms/{game_id}")
    if isinstance(game, str):
        return game
    summary = (game.get("summary") or "No summary.").replace("\n", " ")
    if len(summary) > 500:
        summary = f"{summary[:497]}..."
    return "\n".join(
        [
            f"Title: {game.get('name') or game.get('fs_name_no_ext', '?')}",
            f"ID: {game.get('id', '?')}",
            f"Platform: {game.get('platform_display_name') or game.get('platform_fs_slug', '?')}",
            f"File: {game.get('fs_name', '?')} ({_format_size(game.get('fs_size_bytes'))})",
            f"Regions: {', '.join(game.get('regions', [])) or 'unknown'}",
            f"Languages: {', '.join(game.get('languages', [])) or 'unknown'}",
            f"Summary: {summary}",
        ]
    )


# ════════════════════════════════════════════════════════════════
#  GameVault Tools
# ════════════════════════════════════════════════════════════════


def _format_gamevault_game(game: dict) -> str:
    metadata = game.get("metadata") or {}
    title = metadata.get("title") or game.get("title", "?")
    game_type = game.get("type", "?").replace("_", " ").title()
    return (
        f"• [{game.get('id', '?')}] {title} [{game_type}] — "
        f"{_format_size(game.get('size'))}"
    )


@mcp.tool()
def gamevault_list_games(search: str = "", page: int = 1, limit: int = 25) -> str:
    """List or search PC games and installers indexed by GameVault.

    Args:
        search: Optional game-title search term.
        page: Results page (default: 1).
        limit: Number of games to return (default: 25, max: 100).
    """
    params = {"page": max(1, page), "limit": max(1, min(limit, 100))}
    if search:
        params["search"] = search
    data = _gamevault("/api/games", params=params)
    if isinstance(data, str):
        return data
    games = data.get("data", [])
    meta = data.get("meta", {})
    summary = f"Showing {len(games)} of {meta.get('totalItems', len(games))} GameVault games."
    return "\n".join([summary, *[_format_gamevault_game(game) for game in games]])


@mcp.tool()
def gamevault_get_game(game_id: int) -> str:
    """Get details for a GameVault game by its internal ID."""
    if game_id < 1:
        return "GameVault game ID must be at least 1."
    game = _gamevault(f"/api/games/{game_id}")
    if isinstance(game, str):
        return game
    metadata = game.get("metadata") or {}
    description = (metadata.get("description") or "No description.").replace("\n", " ")
    if len(description) > 500:
        description = f"{description[:497]}..."
    return "\n".join(
        [
            f"Title: {metadata.get('title') or game.get('title', '?')}",
            f"ID: {game.get('id', '?')}",
            f"Type: {game.get('type', '?').replace('_', ' ').title()}",
            f"Version: {game.get('version') or 'unknown'}",
            f"File: {game.get('file_path', '?')} ({_format_size(game.get('size'))})",
            f"Downloads: {game.get('download_count', 0)}",
            f"Description: {description}",
        ]
    )


@mcp.tool()
def gamevault_random_game() -> str:
    """Pick a random game from GameVault."""
    game = _gamevault("/api/games/random")
    if isinstance(game, str):
        return game
    return _format_gamevault_game(game)


@mcp.tool()
def gamevault_reindex() -> str:
    """Tell GameVault to scan its game-files directory for new or changed installers."""
    data = _gamevault("/api/games/reindex", method="PUT")
    if isinstance(data, str):
        return data
    return f"GameVault reindex complete. Indexed {len(data)} games."


# ════════════════════════════════════════════════════════════════
#  SABnzbd Tools
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def sab_queue() -> str:
    """Show the current SABnzbd download queue."""
    return str(_sab("queue"))


@mcp.tool()
def sab_history() -> str:
    """Show SABnzbd download history."""
    return str(_sab("history"))


@mcp.tool()
def sab_status() -> str:
    """Show SABnzbd full status (server, disk space, speed, etc.)."""
    return str(_sab("fullstatus"))


@mcp.tool()
def sab_pause() -> str:
    """Pause the entire SABnzbd queue."""
    return str(_sab("pause"))


@mcp.tool()
def sab_resume() -> str:
    """Resume the entire SABnzbd queue."""
    return str(_sab("resume"))


@mcp.tool()
def sab_pause_job(nzo_id: str) -> str:
    """Pause a specific SABnzbd queue item.

    Args:
        nzo_id: The NZO id of the queue item (use sab_queue to find it).
    """
    if not nzo_id:
        return "Invalid nzo_id."
    return str(_sab("queue", name="pause", value=nzo_id))


@mcp.tool()
def sab_resume_job(nzo_id: str) -> str:
    """Resume a specific SABnzbd queue item.

    Args:
        nzo_id: The NZO id of the queue item.
    """
    if not nzo_id:
        return "Invalid nzo_id."
    return str(_sab("queue", name="resume", value=nzo_id))


@mcp.tool()
def sab_delete_job(nzo_id: str, delete_files: bool = False) -> str:
    """Delete a job from the SABnzbd queue.

    Args:
        nzo_id: The NZO id of the queue item.
        delete_files: If True, also delete any files already downloaded.
    """
    if not nzo_id:
        return "Invalid nzo_id."
    params = {"name": "delete", "value": nzo_id}
    if delete_files:
        params["del_files"] = 1
    return str(_sab("queue", **params))


@mcp.tool()
def sab_add_url(nzb_url: str, category: str = "", priority: int = -100) -> str:
    """Add an NZB by URL to SABnzbd.

    Args:
        nzb_url: URL pointing at an NZB file.
        category: Optional SAB category (default: empty = default category).
        priority: SABnzbd priority (-100 = default, -2..2 supported).
    """
    if not nzb_url:
        return "Invalid nzb_url."
    params = {"name": nzb_url, "priority": priority}
    if category:
        params["cat"] = category
    return str(_sab("addurl", **params))


@mcp.tool()
def sab_speed_limit(percent: int) -> str:
    """Set the SABnzbd global speed limit as a percentage of the configured max.

    Args:
        percent: Speed-limit percentage, 0..100 (0 = pause-by-throttle, 100 = full speed).
    """
    if not isinstance(percent, int) or percent < 0 or percent > 100:
        return "Invalid percent (must be an integer 0..100)."
    return str(_sab("config", name="speedlimit", value=percent))


# ════════════════════════════════════════════════════════════════
#  Bookshelf Tools (Hardcover-flavored Readarr fork; Readarr v1 API)
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def bookshelf_health() -> str:
    """Check Bookshelf health: returns app version, build, and any active health issues."""
    status = _bookshelf("/system/status")
    if isinstance(status, str):
        return status
    health = _bookshelf("/health")
    lines = [
        f"App: {status.get('appName', '?')} {status.get('version', '?')}",
        f"Branch: {status.get('branch', '?')}",
        f"Build: {status.get('buildTime', '?')}",
        f"Runtime: {status.get('runtimeName', '?')} {status.get('runtimeVersion', '?')}",
    ]
    if isinstance(health, list):
        if not health:
            lines.append("Health: ✅ no issues")
        else:
            lines.append(f"Health: ⚠️  {len(health)} issue(s)")
            for h in health:
                lines.append(f"  • [{h.get('type', '?')}] {h.get('source', '?')}: {h.get('message', '?')}")
    return "\n".join(lines)


@mcp.tool()
def bookshelf_list_authors() -> str:
    """List all authors in Bookshelf with monitoring status, book counts, and disk usage."""
    data = _bookshelf("/author")
    if isinstance(data, str):
        return data
    lines = []
    for a in sorted(data, key=lambda x: x.get("authorName", "")):
        stats = a.get("statistics", {}) or {}
        have = stats.get("bookFileCount", 0)
        total = stats.get("bookCount", 0)
        size_gb = stats.get("sizeOnDisk", 0) / 1e9
        icon = "✅" if a.get("monitored") else "⏸️"
        lines.append(
            f"{icon} [{a.get('id', '?')}] {a.get('authorName', '?')} — "
            f"{have}/{total} books, {size_gb:.2f} GB"
        )
    return "\n".join(lines) or "No authors found."


@mcp.tool()
def bookshelf_get_author(author_id: int) -> str:
    """Get detailed info about a specific author by their Bookshelf ID."""
    if author_id <= 0:
        return "Invalid author_id."
    a = _bookshelf(f"/author/{author_id}")
    if isinstance(a, str):
        return a
    stats = a.get("statistics", {}) or {}
    lines = [
        f"Name: {a.get('authorName', '?')}",
        f"Status: {a.get('status', '?')}",
        f"Monitored: {a.get('monitored', False)}",
        f"Books: {stats.get('bookFileCount', 0)}/{stats.get('bookCount', 0)}",
        f"Size: {stats.get('sizeOnDisk', 0) / 1e9:.2f} GB",
        f"Path: {a.get('path', '?')}",
        f"Hardcover ID: {a.get('foreignAuthorId', '?')}",
        f"Overview: {(a.get('overview') or 'N/A')[:300]}",
    ]
    return "\n".join(lines)


@mcp.tool()
def bookshelf_search_author(term: str) -> str:
    """Search Bookshelf's metadata source (Hardcover) for an author. Returns name and Hardcover ID."""
    data = _bookshelf("/author/lookup", params={"term": term})
    if isinstance(data, str):
        return data
    lines = []
    for r in data[:10]:
        overview = (r.get("overview") or "No description.")[:150].replace("\n", " ")
        lines.append(
            f"• {r.get('authorName', '?')} "
            f"[hardcoverId: {r.get('foreignAuthorId', '?')}]\n  {overview}"
        )
    return "\n".join(lines) or "No results found."


@mcp.tool()
def bookshelf_search_book(term: str) -> str:
    """Search Bookshelf's metadata source (Hardcover) for a book. Returns title, author, and IDs."""
    data = _bookshelf("/book/lookup", params={"term": term})
    if isinstance(data, str):
        return data
    lines = []
    for r in data[:10]:
        title = r.get("title", "?")
        author = "?"
        a = r.get("author")
        if isinstance(a, dict):
            author = a.get("authorName", "?")
        release = (r.get("releaseDate") or "?")[:10]
        lines.append(
            f"• {title} — {author} ({release}) "
            f"[bookId: {r.get('foreignBookId', '?')}]"
        )
    return "\n".join(lines) or "No results found."


@mcp.tool()
def bookshelf_list_books() -> str:
    """List all books currently tracked in Bookshelf (title, author, monitored, page count)."""
    data = _bookshelf("/book")
    if isinstance(data, str):
        return data
    lines = []
    for b in data:
        title = b.get("title", "?")
        author = "?"
        a = b.get("author")
        if isinstance(a, dict):
            author = a.get("authorName", "?")
        pages = b.get("pageCount", 0) or 0
        icon = "✅" if b.get("monitored") else "⏸️"
        release = (b.get("releaseDate") or "?")[:10]
        lines.append(
            f"{icon} [{b.get('id', '?')}] {title} — {author} ({release}, {pages}p)"
        )
    return "\n".join(lines) or "No books found."


@mcp.tool()
def bookshelf_queue() -> str:
    """Show the current Bookshelf download queue with status and queue IDs."""
    data = _bookshelf("/queue", params={"pageSize": 50, "includeUnknownAuthorItems": "true"})
    if isinstance(data, str):
        return data
    records = data.get("records", []) if isinstance(data, dict) else []
    lines = []
    for r in records:
        title = r.get("title", "?")
        status = r.get("status", "?")
        sizeleft = (r.get("sizeleft", 0) or 0) / 1e9
        lines.append(f"• [queueId: {r.get('id', '?')}] {title} — {status} ({sizeleft:.2f} GB remaining)")
    return "\n".join(lines) or "Queue is empty."


@mcp.tool()
def bookshelf_wanted_missing(page_size: int = 20) -> str:
    """List books Bookshelf has flagged as missing (monitored but no file). Paged; default 20."""
    if page_size <= 0 or page_size > 200:
        page_size = 20
    data = _bookshelf("/wanted/missing", params={"pageSize": page_size})
    if isinstance(data, str):
        return data
    records = data.get("records", []) if isinstance(data, dict) else []
    total = data.get("totalRecords", len(records)) if isinstance(data, dict) else len(records)
    lines = [f"Total missing: {total} (showing {len(records)})"]
    for r in records:
        title = r.get("title", "?")
        release = (r.get("releaseDate") or "?")[:10]
        lines.append(f"• [bookId: {r.get('id', '?')}] {title} ({release})")
    return "\n".join(lines)


@mcp.tool()
def bookshelf_list_quality_profiles() -> str:
    """List all quality profiles configured in Bookshelf with their allowed qualities."""
    data = _bookshelf("/qualityprofile")
    if isinstance(data, str):
        return data
    lines = []
    for p in data:
        qualities = [
            (q.get("quality") or {}).get("name", "?")
            for q in p.get("items", [])
            if q.get("allowed") and isinstance(q.get("quality"), dict)
        ]
        lines.append(
            f"• [{p.get('id', '?')}] {p.get('name', '?')} — Allowed: {', '.join(qualities) or 'none'}"
        )
    return "\n".join(lines) or "No quality profiles found."


@mcp.tool()
def bookshelf_list_metadata_profiles() -> str:
    """List all metadata profiles in Bookshelf (controls which release types/secondary types are tracked)."""
    data = _bookshelf("/metadataprofile")
    if isinstance(data, str):
        return data
    lines = [f"• [{p.get('id', '?')}] {p.get('name', '?')}" for p in data]
    return "\n".join(lines) or "No metadata profiles found."


@mcp.tool()
def bookshelf_list_root_folders() -> str:
    """List all configured root folders in Bookshelf with free space."""
    data = _bookshelf("/rootfolder")
    if isinstance(data, str):
        return data
    lines = []
    for r in data:
        free_gb = (r.get("freeSpace", 0) or 0) / 1e9
        lines.append(f"• [{r.get('id', '?')}] {r.get('path', '?')} — {free_gb:.1f} GB free")
    return "\n".join(lines) or "No root folders configured."


@mcp.tool()
def bookshelf_search_missing() -> str:
    """Trigger Bookshelf to search for all missing monitored books."""
    result = _bookshelf("/command", method="POST", json={"name": "MissingBookSearch"})
    if isinstance(result, dict):
        return "🔍 Search triggered for all missing books."
    return str(result)


# ── Entrypoint ──

def _setup_output_path() -> pathlib.Path:
    """Where the setup page writes: the active config file, else ./arrstack.toml."""
    return pathlib.Path(_CONFIG.path or "arrstack.toml").expanduser().resolve()


def _restart_with_config(path: pathlib.Path) -> None:
    """Re-exec this process so the saved config (and tool list) takes effect."""
    os.environ[config.CONFIG_ENV] = str(path)
    logger.warning("setup saved to %s; restarting", path)
    sys.stdout.flush()
    sys.stderr.flush()
    # Re-run as `python -m arrstack_mcp`, which works however we were started
    # (installed command, `python -m`, or the source checkout's server.py).
    package_parent = str(pathlib.Path(__file__).resolve().parent.parent)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        p for p in (package_parent, os.environ.get("PYTHONPATH", "")) if p
    )
    os.execv(sys.executable, [sys.executable, "-m", "arrstack_mcp", *sys.argv[1:]])


def _enable_setup_ui(transport: str, host: str, port: int, first_run: bool) -> None:
    """Register /setup and print its one-time link to the log."""
    ui = setup_ui.SetupUI(
        _CONFIG, _setup_output_path(), _restart_with_config,
        mcp_path="/sse" if transport == "sse" else "/mcp",
    )
    for path, methods, handler in ui.routes():
        mcp.custom_route(path, methods=methods)(handler)
    shown_host = "<this-server>" if host in ("0.0.0.0", "::", "") else host
    link = f"http://{shown_host}:{port}/setup?token={ui.token}"
    if first_run:
        print("\n   ⚙️  No services configured yet. Open the setup page to pick yours:", file=sys.stderr)
    else:
        print("   Setup page (changes on every restart):", file=sys.stderr)
    print(f"   {link}\n", file=sys.stderr)


def _format_config_problems() -> str:
    where = f" (config file: {_CONFIG.path})" if _CONFIG.path else ""
    lines = [f"configuration problems{where}:"] + [f"  - {problem}" for problem in CONFIG_PROBLEMS]
    return "\n".join(lines)


def _run_check(as_json: bool = False, timeout: float = doctor.DEFAULT_TIMEOUT, transport=None) -> int:
    """--check: report every service's state; returns the process exit code."""
    problems = list(CONFIG_PROBLEMS)
    try:
        _instruction_presets(INSTRUCTION_PRESETS)
    except ValueError as error:
        problems.append(str(error))
    try:
        selected = _selected_services()
    except ValueError as error:
        problems.append(f"ENABLED_SERVICES: {error}")
        selected = _selected_services("auto")
    results = doctor.run_checks(SETTINGS, selected, transport=transport, timeout=timeout)
    render = doctor.render_json if as_json else doctor.render_text
    sys.stdout.write(render(results, problems, _CONFIG.path))
    return doctor.exit_code(results, problems)


def main():
    global _active_transport
    parser = argparse.ArgumentParser(
        description="arrstack-mcp — MCP server for media and game library services"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=f"TOML config file (also ${config.CONFIG_ENV}); environment variables override it",
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default: 0.0.0.0)")
    parser.add_argument(
        "--list-services",
        action="store_true",
        help="Show configured/enabled services and their tool counts, then exit",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Interactive wizard: enter and test each service, write a private config file, "
        "print MCP client snippets",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="With --setup: file to write (default: the --config file, else arrstack.toml or .env)",
    )
    parser.add_argument(
        "--format",
        choices=["toml", "env"],
        default="toml",
        help="With --setup: write a TOML config file (default) or a Docker env file",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Look for services at their usual container names and on localhost "
        "(alone: list them; with --setup: use them as defaults)",
    )
    parser.add_argument(
        "--arr-config",
        metavar="DIR",
        action="append",
        default=[],
        help="Read URL base, port and API key from an *arr config.xml in DIR (repeatable)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check every service: configured, reachable, credentials accepted, version. "
        "Exits 1 if something enabled is broken",
    )
    parser.add_argument("--json", action="store_true", help="With --check: machine-readable output")
    parser.add_argument(
        "--timeout",
        type=float,
        default=doctor.DEFAULT_TIMEOUT,
        help=f"With --check/--setup: per-request timeout in seconds (default: {doctor.DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--print-env-example",
        action="store_true",
        help="Print a .env template covering every setting, then exit",
    )
    parser.add_argument(
        "--print-catalog",
        action="store_true",
        help="Print the ARD ai-catalog.json manifest to stdout, then exit "
        "(for static hosting at /.well-known/ai-catalog.json)",
    )
    parser.add_argument(
        "--print-server-card",
        action="store_true",
        help="Print the ARD MCP server card to stdout, then exit",
    )
    args = parser.parse_args()

    if args.transport in ("streamable-http", "sse"):
        _active_transport = args.transport

    if args.print_env_example:
        sys.stdout.write(config.render_env_example())
        return
    if args.check:
        sys.exit(_run_check(as_json=args.json, timeout=args.timeout))
    if args.setup:
        sys.exit(_run_setup(args.output, args.format, args.discover, args.arr_config, args.timeout))
    if args.discover:
        sys.exit(_run_discover(args.arr_config, min(args.timeout, discover.DEFAULT_TIMEOUT)))
    if CONFIG_PROBLEMS:
        parser.error(_format_config_problems())

    try:
        _instruction_presets(INSTRUCTION_PRESETS)
        if args.list_services:
            _print_service_status()
            return
        if args.print_catalog or args.print_server_card:
            _configure_service_tools()
            _emit_ard_document(server_card=args.print_server_card)
            return
        selected = _configure_service_tools()
    except ValueError as error:
        parser.error(str(error))

    http = args.transport in ("streamable-http", "sse")
    setup_enabled = http and SETTINGS["SETUP_UI"].strip().lower() not in ("false", "0", "no", "off")
    if not selected and not setup_enabled:
        print(
            "⚠️  No services enabled. Run `python server.py --setup`, configure at least one "
            "service URL, or set ENABLED_SERVICES to a comma-separated service list.",
            file=sys.stderr,
        )
        sys.exit(1)

    enabled_names = [SERVICE_CONFIG[key][0] for key in SERVICE_CONFIG if key in selected]
    tool_count = len(mcp._tool_manager.list_tools())
    print(f"🎬 arrstack-mcp starting ({', '.join(enabled_names) or 'setup mode'})", file=sys.stderr)
    if setup_enabled:
        _enable_setup_ui(args.transport, args.host, args.port, first_run=not selected)
    print(f"   Advertised tools: {tool_count}", file=sys.stderr)
    print(f"   Transport: {args.transport}", file=sys.stderr)
    if args.transport in ("streamable-http", "sse") and not _ard_disabled():
        discovery_base = ard.normalize_public_url(ARD_PUBLIC_URL) or f"http://{args.host}:{args.port}"
        print(
            f"   ARD discovery: {discovery_base}{ard.WELL_KNOWN_CATALOG_PATH}",
            file=sys.stderr,
        )

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

