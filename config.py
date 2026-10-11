"""
Configuration registry and loader for arrstack-mcp.

Every setting the server understands is declared once here. The same table
drives:

* loading values from the environment, ``<VAR>_FILE`` secret files and an
  optional TOML config file (``load``),
* validation with readable error messages,
* the generated ``.env.example`` and README configuration table
  (``render_env_example`` / ``render_markdown_table``), so docs can't drift,
* the ``--check`` doctor and the ``--setup`` wizard in ``server.py``.

Environment variable names are the public, backward-compatible interface; the
TOML file is an optional convenience. Precedence, highest first:

1. a non-empty environment variable (``SONARR_API_KEY``)
2. ``<VAR>_FILE`` in the environment (``SONARR_API_KEY_FILE=/run/secrets/...``)
3. the TOML file (``[sonarr] api_key = ...`` or ``api_key_file = ...``)
4. the built-in default

Empty environment variables count as unset, so a compose file listing
``SONARR_URL=`` doesn't mask a value from the config file.

This module is pure: no network, no globals beyond the registry, and ``load``
takes the environment as an argument, so it is trivially unit-testable.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

CONFIG_ENV = "ARRSTACK_CONFIG"


# ── Registry ──


@dataclass(frozen=True)
class Setting:
    """One configurable value: its env var, TOML key and documentation."""

    env: str
    key: str
    description: str
    default: str = ""
    example: str = ""  # value shown in .env.example (defaults to `default`)
    secret: bool = False
    url: bool = False
    required: str = "No"  # "Required" column of the README table
    need: str = "optional"  # required | recommended | optional (for the doctor)
    comment: str = ""  # extra .env.example comment, may span lines

    @property
    def example_value(self) -> str:
        return self.example or self.default


@dataclass(frozen=True)
class Service:
    """A backend service: its settings plus what --check/--setup need to know."""

    key: str
    name: str
    label: str  # short description, e.g. "TV Shows"
    prefix: str  # tool-name prefix
    settings: tuple[Setting, ...]  # settings[0] is the base URL
    default_port: int
    hostnames: tuple[str, ...]  # common Docker container names
    key_help: str  # where to find the credential in the service's UI
    aliases: tuple[str, ...] = ()
    credential_check: Callable[[Mapping[str, str]], list[tuple[str, str]]] | None = None

    @property
    def url(self) -> Setting:
        return self.settings[0]

    def credential_issues(self, values: Mapping[str, str]) -> list[tuple[str, str]]:
        """(level, message) pairs for missing credentials; level is error|warning."""
        if self.credential_check:
            return self.credential_check(values)
        issues = []
        for setting in self.settings[1:]:
            if values.get(setting.env):
                continue
            if setting.need == "required":
                issues.append(("error", f"{setting.env} is empty ({self.key_help})"))
            elif setting.need == "recommended":
                issues.append(("warning", f"{setting.env} is empty; {setting.description}"))
        return issues


def _url(env: str, name: str, port: int, path: str = "", what: str = "base URL") -> Setting:
    example = f"http://localhost:{port}{path}"
    return Setting(
        env, "url", f"{name} {what} (e.g. `{example}`)", example=example, url=True
    )


def _api_key(env: str, name: str, where: str, need: str = "required") -> Setting:
    required = f"If {name}" if need == "required" else "No"
    return Setting(env, "api_key", f"{name} API key ({where})", secret=True, required=required, need=need)


def _romm_credentials(values: Mapping[str, str]) -> list[tuple[str, str]]:
    if values.get("ROMM_API_TOKEN") or (values.get("ROMM_USER") and values.get("ROMM_PASS")):
        return []
    return [(
        "error",
        "set ROMM_API_TOKEN, or ROMM_USER and ROMM_PASS (only romm_system_info works without them)",
    )]


SERVICES: dict[str, Service] = {
    service.key: service
    for service in (
        Service(
            "sonarr", "Sonarr", "TV Shows", "sonarr_",
            (_url("SONARR_URL", "Sonarr", 8989), _api_key("SONARR_API_KEY", "Sonarr", "Settings → General")),
            8989, ("sonarr",), "Settings → General → API Key",
        ),
        Service(
            "radarr", "Radarr", "Movies", "radarr_",
            (_url("RADARR_URL", "Radarr", 7878), _api_key("RADARR_API_KEY", "Radarr", "Settings → General")),
            7878, ("radarr",), "Settings → General → API Key",
        ),
        Service(
            "lidarr", "Lidarr", "Music", "lidarr_",
            (_url("LIDARR_URL", "Lidarr", 8686), _api_key("LIDARR_API_KEY", "Lidarr", "Settings → General")),
            8686, ("lidarr",), "Settings → General → API Key",
        ),
        Service(
            "prowlarr", "Prowlarr", "Indexer Manager", "prowlarr_",
            (_url("PROWLARR_URL", "Prowlarr", 9696), _api_key("PROWLARR_API_KEY", "Prowlarr", "Settings → General")),
            9696, ("prowlarr",), "Settings → General → API Key",
        ),
        Service(
            "qbittorrent", "qBittorrent", "Downloads", "qbt_",
            (
                _url("QBT_URL", "qBittorrent", 8080, what="Web UI URL"),
                Setting("QBT_USER", "username", "qBittorrent username (default: `admin`)",
                        default="admin", required="If qBt"),
                Setting("QBT_PASS", "password", "qBittorrent password", secret=True, required="If qBt",
                        need="recommended",
                        comment="Leave empty only if qBittorrent bypasses authentication for this client."),
            ),
            8080, ("qbittorrent", "qbt"), "Settings → Web UI → Authentication",
            aliases=("qbt",),
        ),
        Service(
            "rdtclient", "RDTClient", "Real-Debrid / AllDebrid downloader", "rdt_",
            (
                _url("RDT_URL", "RDTClient", 6500),
                Setting("RDT_USER", "username", "RDTClient username (default: `admin`)",
                        default="admin", required="If RDT login",
                        comment="RDTClient exposes a qBittorrent-compatible API at /api/v2. RDT_USER/RDT_PASS\n"
                                "are only needed if your install requires login (most do)."),
                Setting("RDT_PASS", "password", "RDTClient password", secret=True, required="If RDT login",
                        need="recommended"),
            ),
            6500, ("rdtclient", "rdt-client"), "Settings → General → Authentication",
            aliases=("rdt",),
        ),
        Service(
            "sabnzbd", "SABnzbd", "Usenet Downloads", "sab_",
            (_url("SAB_URL", "SABnzbd", 8080), _api_key("SAB_API_KEY", "SABnzbd", "Config → General → API Key")),
            8080, ("sabnzbd",), "Config → General → API Key",
            aliases=("sab",),
        ),
        Service(
            "jellyfin", "Jellyfin", "Media Streaming", "jellyfin_",
            (
                _url("JELLYFIN_URL", "Jellyfin", 8096),
                Setting("JELLYFIN_API_KEY", "api_key",
                        "Jellyfin API key (needed for libraries, recent items and library scans)",
                        secret=True, need="recommended"),
            ),
            8096, ("jellyfin",), "Dashboard → API Keys → Add",
        ),
        Service(
            "romm", "RomM", "ROM Library", "romm_",
            (
                _url("ROMM_URL", "RomM", 8081),
                Setting("ROMM_API_TOKEN", "api_token",
                        "RomM bearer token; alternatively use `ROMM_USER` and `ROMM_PASS`",
                        secret=True, required="If RomM"),
                Setting("ROMM_USER", "username", "RomM username", required="If RomM basic auth",
                        comment="Alternatively, use basic authentication:"),
                Setting("ROMM_PASS", "password", "RomM password", secret=True, required="If RomM basic auth"),
            ),
            8081, ("romm",), "User profile → API Tokens",
            credential_check=_romm_credentials,
        ),
        Service(
            "gamevault", "GameVault", "PC Game Library", "gamevault_",
            (_url("GAMEVAULT_URL", "GameVault", 8082, what="server URL"),
             _api_key("GAMEVAULT_API_KEY", "GameVault", "Admin panel → API Keys")),
            8082, ("gamevault-backend", "gamevault"), "Admin panel → API Keys",
            aliases=("game-vault",),
        ),
        Service(
            "bookshelf", "Bookshelf", "Books — pennydreadful/bookshelf, Hardcover-flavored Readarr fork",
            "bookshelf_",
            (_url("BOOKSHELF_URL", "Bookshelf", 8787),
             _api_key("BOOKSHELF_API_KEY", "Bookshelf", "Settings → General")),
            8787, ("bookshelf", "readarr"), "Settings → General → API Key (same as Readarr)",
        ),
        Service(
            "seerr", "Seerr", "Media Requests: Overseerr / Jellyseerr", "seerr_",
            (Setting("SEERR_URL", "url",
                     "Seerr / Overseerr / Jellyseerr base URL (e.g. `http://localhost:5055`)",
                     example="http://localhost:5055", url=True),
             _api_key("SEERR_API_KEY", "Seerr", "Settings → General")),
            5055, ("seerr", "jellyseerr", "overseerr"), "Settings → General → API Key",
        ),
    )
}

SERVICE_ALIASES = {alias: s.key for s in SERVICES.values() for alias in s.aliases}

SERVER_SETTINGS: tuple[Setting, ...] = (
    Setting("ENABLED_SERVICES", "enabled_services",
            "`auto` (default), `all`, or a comma-separated service subset", default="auto",
            comment="Tool catalog selection: auto enables only services with a configured URL.\n"
                    "Use a comma-separated subset (e.g. sonarr,radarr,romm) or \"all\"."),
    Setting("ALLOW_DESTRUCTIVE", "allow_destructive",
            "`false` hides tools that delete downloads or library files (default `true`)", default="true",
            comment="Set to false to hide tools that delete downloads or library files."),
    Setting("INSTRUCTION_PRESETS", "instruction_presets",
            "Comma-separated opt-in instruction presets (e.g. `hun-ncore`)",
            comment="Opt-in instruction presets (e.g. hun-ncore) and extra free-text instructions."),
    Setting("EXTRA_INSTRUCTIONS", "extra_instructions", "Free text appended to the server instructions"),
    Setting("MCP_ALLOWED_HOSTS", "mcp_allowed_hosts",
            "Comma-separated accepted Host headers; supports wildcard ports such as `arrstack-mcp:*`",
            default="localhost:*,127.0.0.1:*,[::1]:*", required="For HTTP/SSE",
            comment="Comma-separated Host headers accepted by HTTP/SSE transports."),
    Setting("LOG_LEVEL", "log_level",
            "Logging level (default: `INFO`; credentials are masked)", default="INFO"),
)

ARD_SETTINGS: tuple[Setting, ...] = (
    Setting("ARD_ENABLED", "enabled",
            "Agentic Resource Discovery publishing: `auto` (default; on for HTTP transports), `true`, or `false`",
            default="auto",
            comment="Publishes this server at /.well-known/ai-catalog.json so ARD registries and\n"
                    "agents can discover it. See https://agenticresourcediscovery.org/\n"
                    "NOTE: ARD publishes discoverable metadata only — it does NOT authenticate this\n"
                    "server or make it safe to expose. Keep HTTP transports behind Tailscale or an\n"
                    "authenticated proxy (see the Security section of the README).\n"
                    "ARD_ENABLED: auto (serve on HTTP transports, default) | true | false"),
    Setting("ARD_PUBLIC_URL", "public_url",
            "Public base URL clients reach this server at (e.g. `https://arrstack.example.com`); "
            "advertises an absolute connection endpoint inside the card. Leave blank if the server is private.",
            comment="Public base URL clients reach this server at. Set this to advertise an absolute\n"
                    "MCP connection endpoint inside the card. Leave blank for a private deployment."),
    Setting("ARD_DOMAIN", "domain",
            "Publisher domain for the `urn:air` logical identifier "
            "(defaults to the host of `ARD_PUBLIC_URL`, else `localhost`)",
            comment="Optional: publisher domain for the urn:air logical identifier (a name, not a\n"
                    "resolvable address). Defaults to the host of ARD_PUBLIC_URL (or \"localhost\")."),
    Setting("ARD_HOST_NAME", "host_name", "Human-readable catalog host name (default: `arrstack-mcp`)",
            comment="Optional: human-readable catalog host name (defaults to \"arrstack-mcp\")."),
    Setting("ARD_EMBED_CARD", "embed_card",
            "`auto` (embed the server card inline only when `ARD_PUBLIC_URL` is unset), "
            "`true` (always embed — best for static hosting), or `false` (always reference it by URL)",
            default="auto",
            comment="Embed the MCP server card inline in the catalog: auto (default) | true | false.\n"
                    "Set true for static hosting (e.g. GitHub Pages) so the manifest is self-contained."),
    Setting("ARD_DID_WEB", "did_web",
            "Opt-in `did:web` host identity (e.g. `arrstack.example.com`). Only set for a domain whose root "
            "you control and where you host a `/.well-known/did.json`; otherwise left off",
            comment="Optional opt-in did:web host identity (e.g. arrstack.example.com). Only set for\n"
                    "a domain whose root you control and where you host /.well-known/did.json;\n"
                    "otherwise left off so the catalog never claims an identity that can't resolve."),
)

# TOML table name -> settings in it. Service tables use the service key.
TABLES: dict[str, tuple[Setting, ...]] = {
    "server": SERVER_SETTINGS,
    **{key: service.settings for key, service in SERVICES.items()},
    "ard": ARD_SETTINGS,
}

ALL_SETTINGS: tuple[Setting, ...] = tuple(s for settings in TABLES.values() for s in settings)
SECRET_ENVS: tuple[str, ...] = tuple(s.env for s in ALL_SETTINGS if s.secret)


# ── Loading ──


@dataclass
class LoadedConfig:
    values: dict[str, str]
    sources: dict[str, str]  # env name -> where the value came from
    problems: list[str] = field(default_factory=list)
    path: str | None = None


def config_path(environ: Mapping[str, str], argv: Sequence[str] = ()) -> str | None:
    """Config file from `--config PATH` / `--config=PATH`, else $ARRSTACK_CONFIG."""
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return environ.get(CONFIG_ENV, "").strip() or None


def _suggest(name: str, options) -> str:
    match = difflib.get_close_matches(name, list(options), n=1)
    return f" (did you mean '{match[0]}'?)" if match else ""


def _read_secret_file(path: str, label: str, problems: list[str]) -> str | None:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as error:
        problems.append(f"{label}: cannot read {path}: {error.strerror or error}")
        return None


def _toml_value(value, label: str, problems: list[str]) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ",".join(value)
    problems.append(f"{label}: expected a string, got {type(value).__name__}")
    return None


def _read_toml(path: str, problems: list[str]) -> dict:
    if tomllib is None:
        problems.append(f"{path}: reading a TOML config needs Python 3.11+ or `pip install tomli`")
        return {}
    try:
        with open(Path(path).expanduser(), "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        problems.append(f"config file not found: {path}")
    except OSError as error:
        problems.append(f"cannot read config file {path}: {error.strerror or error}")
    except tomllib.TOMLDecodeError as error:
        problems.append(f"{path}: invalid TOML: {error}")
    return {}


def _file_values(data: dict, path: str, problems: list[str]) -> dict[str, tuple[str, str]]:
    """Flatten a parsed TOML file into {env: (value, source)}, reporting unknown keys."""
    result: dict[str, tuple[str, str]] = {}
    for table, entries in data.items():
        table_key = SERVICE_ALIASES.get(table, table)
        if table_key not in TABLES:
            problems.append(f"{path}: unknown section [{table}]{_suggest(table, TABLES)}")
            continue
        if not isinstance(entries, dict):
            problems.append(f"{path}: '{table}' must be a [section], not a value")
            continue
        by_key = {s.key: s for s in TABLES[table_key]}
        for key, raw in entries.items():
            label = f"{path}: [{table}] {key}"
            setting = by_key.get(key.removesuffix("_file"))
            if setting is None:
                options = list(by_key) + [f"{k}_file" for k in by_key]
                problems.append(f"{label}: unknown setting{_suggest(key, options)}")
                continue
            value = _toml_value(raw, label, problems)
            if value is None:
                continue
            if key.endswith("_file") and key != setting.key:
                if setting.env in result:  # inline value wins over its _file
                    continue
                content = _read_secret_file(value, label, problems)
                if content is not None:
                    result[setting.env] = (content, f"config file ({key})")
            else:
                result[setting.env] = (value, "config file")
    return result


def _url_problem(setting: Setting, value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        hint = " (missing http:// ?)" if "://" not in value else ""
        shown = value if "@" not in value else "<url with credentials>"
        return f"{setting.env}: '{shown}' is not an http(s) URL{hint}"
    return None


def load(environ: Mapping[str, str], path: str | None = None) -> LoadedConfig:
    """Resolve every setting. Never raises: problems are collected for the caller."""
    problems: list[str] = []
    file_values = _file_values(_read_toml(path, problems), path, problems) if path else {}
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for setting in ALL_SETTINGS:
        env_value = environ.get(setting.env, "")
        file_env = environ.get(f"{setting.env}_FILE", "").strip()
        value, source = setting.default, "default"
        if env_value.strip():
            value, source = env_value, "environment"
        elif file_env:
            content = _read_secret_file(file_env, f"{setting.env}_FILE", problems)
            if content is not None:
                value, source = content, f"{setting.env}_FILE"
        elif setting.env in file_values:
            value, source = file_values[setting.env]
        if setting.url:
            value = value.strip().rstrip("/")
            if value:
                problem = _url_problem(setting, value)
                if problem:
                    problems.append(problem)
        values[setting.env] = value
        sources[setting.env] = source
    return LoadedConfig(values, sources, problems, path)


# ── Rendering (docs, wizard output) ──


def _comment(text: str) -> list[str]:
    return [f"# {line}" if line else "#" for line in text.splitlines()]


def render_env_example() -> str:
    """The committed .env.example, generated so it can't drift from the code."""
    lines = [
        "# arrstack-mcp env config — copy to .env and fill in your values.",
        "# Generated by `python server.py --print-env-example`; edit config.py, not this file.",
        "# Only configure the services you use, the rest can be left blank.",
        "# Any variable can instead be read from a file: set <NAME>_FILE=/path (Docker secrets).",
        "",
    ]
    for setting in SERVER_SETTINGS:
        lines += _comment(setting.comment)
        lines.append(f"{setting.env}={setting.example_value}")
    for service in SERVICES.values():
        lines += ["", f"# ── {service.name} ({service.label}) ──"]
        for setting in service.settings:
            lines += _comment(setting.comment)
            lines.append(f"{setting.env}={setting.example_value}")
    lines += ["", "# ── Agentic Resource Discovery (ARD) ──"]
    for setting in ARD_SETTINGS:
        lines += _comment(setting.comment)
        lines.append(f"{setting.env}={setting.example_value}")
    return "\n".join(lines) + "\n"


def render_markdown_table() -> str:
    """The README configuration table."""
    rows = ["| Variable | Required | Description |", "|----------|----------|-------------|"]
    for setting in ALL_SETTINGS:
        rows.append(f"| `{setting.env}` | {setting.required} | {setting.description} |")
    return "\n".join(rows) + "\n"


def _env_line(name: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name}: multi-line values can't be written to a .env file")
    if value and any(ch in value for ch in " #'\"$\\`"):
        if "'" in value:
            raise ValueError(f"{name}: values containing both spaces/# and ' can't be written to .env")
        return f"{name}='{value}'"
    return f"{name}={value}"


def render_env(values: Mapping[str, str]) -> str:
    """A .env file (Docker `env_file:` format) with only the given non-empty values."""
    lines = ["# Written by `python server.py --setup`. Keep this file private (chmod 600)."]
    for setting in ALL_SETTINGS:
        value = values.get(setting.env, "")
        if value and value != setting.default:
            lines.append(_env_line(setting.env, value))
    return "\n".join(lines) + "\n"


def render_toml(values: Mapping[str, str]) -> str:
    """A TOML config file with only the given non-empty, non-default values."""
    lines = ["# Written by `python server.py --setup`. Keep this file private (chmod 600).",
             f"# Use it with {CONFIG_ENV}=/path/to/this/file or `python server.py --config <file>`."]
    for table, settings in TABLES.items():
        entries = [
            (s.key, values[s.env]) for s in settings
            if values.get(s.env) and values[s.env] != s.default
        ]
        if entries:
            lines += ["", f"[{table}]"]
            # json.dumps(ensure_ascii=False) yields a valid TOML basic string.
            lines += [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in entries]
    return "\n".join(lines) + "\n"
