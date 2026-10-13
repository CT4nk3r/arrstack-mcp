"""
Connection checks for arrstack-mcp (`python server.py --check`).

For every service in the config registry this answers "will the tools work?":
is it configured and enabled, can the server reach it, does it accept the
credentials, which version is it, and if not, what to change.

Checks are read-only GETs (plus the qBittorrent/RDTClient login POST) with a
short timeout. Messages are built from fixed text and parsed fields only, so
no credential or response body reaches the output. The HTTP transport is
injectable, which lets the tests run every scenario against
``httpx.MockTransport`` instead of live services.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Mapping
from urllib.parse import urlparse

import httpx

from . import config

OK, WARNING, ERROR, DISABLED, NOT_CONFIGURED = "ok", "warning", "error", "disabled", "not_configured"
ICONS = {OK: "✅", WARNING: "⚠️ ", ERROR: "❌", DISABLED: "⏸️ ", NOT_CONFIGURED: "· "}
DEFAULT_TIMEOUT = 5.0


@dataclass
class CheckResult:
    service: str
    name: str
    status: str
    detail: str
    version: str = ""
    url: str = ""


class ProbeFailed(Exception):
    """A probe step failed; the message is safe to show (no secrets, no bodies)."""


# ── HTTP helpers ──


def _where(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or url)


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.ConnectError:
        raise ProbeFailed(
            f"cannot connect to {_where(url)}: is it running, and is that address reachable from "
            "where arrstack-mcp runs? (inside Docker use the container name, not localhost)"
        ) from None
    except httpx.TimeoutException:
        raise ProbeFailed(f"no answer from {_where(url)} within {client.timeout.read:g}s") from None
    except httpx.RequestError as error:
        raise ProbeFailed(f"request to {_where(url)} failed ({type(error).__name__})") from None


def _json(response: httpx.Response, what: str):
    try:
        return response.json()
    except ValueError:
        kind = "an HTML page" if "html" in response.headers.get("content-type", "") else "a non-JSON response"
        raise ProbeFailed(
            f"{what} returned {kind} instead of the API: wrong URL or URL base, "
            "or a login/proxy page in front of it"
        ) from None


def _expect_ok(response: httpx.Response, service: config.Service, auth_hint: str) -> None:
    code = response.status_code
    if code in (401, 403):
        raise ProbeFailed(f"credentials rejected (HTTP {code}): check {auth_hint}")
    if code == 404:
        example = f"http://{service.hostnames[0]}:{service.default_port}/{service.key}"
        raise ProbeFailed(
            f"no {service.name} API at this URL (HTTP 404). If {service.name} runs under a URL base, "
            f"include it, e.g. {example}"
        )
    if 300 <= code < 400:
        location = urlparse(response.headers.get("location", "")).path or "?"
        raise ProbeFailed(
            f"redirected to {location} (HTTP {code}): likely a login page or a missing URL base"
        )
    if code >= 400:
        raise ProbeFailed(f"HTTP {code} from {service.name}")


# ── Probes: return (version, extra note) or raise ProbeFailed ──

_ARR_API = {"sonarr": "v3", "radarr": "v3", "lidarr": "v1", "prowlarr": "v1", "bookshelf": "v1"}
_ARR_APP_NAMES = {"sonarr": {"sonarr"}, "radarr": {"radarr"}, "lidarr": {"lidarr"},
                  "prowlarr": {"prowlarr"}, "bookshelf": {"readarr", "bookshelf"}}


def _probe_arr(service, values, client):
    url = values[service.url.env]
    key_env = service.settings[1].env
    response = _request(client, "GET", f"{url}/api/{_ARR_API[service.key]}/system/status",
                        headers={"X-Api-Key": values[key_env]})
    _expect_ok(response, service, f"{key_env} ({service.key_help})")
    data = _json(response, service.name)
    if not isinstance(data, dict):
        raise ProbeFailed(f"unexpected status response from {service.name}")
    app = str(data.get("appName") or "")
    if app and app.lower() not in _ARR_APP_NAMES[service.key]:
        raise ProbeFailed(f"this URL is {app}, not {service.name}: check {service.url.env}")
    return str(data.get("version") or ""), ""


def _probe_qbt_like(service, values, client):
    url = values[service.url.env]
    user_env, pass_env = service.settings[1].env, service.settings[2].env
    login = _request(client, "POST", f"{url}/api/v2/auth/login",
                     data={"username": values[user_env], "password": values[pass_env]})
    if login.status_code == 403:
        raise ProbeFailed("login refused (HTTP 403): too many failed attempts can ban the IP for a while")
    _expect_ok(login, service, f"{user_env}/{pass_env}")
    if login.text.strip().lower() == "fails.":
        raise ProbeFailed(f"login rejected: check {user_env} and {pass_env}")
    response = _request(client, "GET", f"{url}/api/v2/app/version")
    if response.status_code == 404:
        return "", "logged in (version endpoint not available)"
    _expect_ok(response, service, f"{user_env}/{pass_env}")
    return response.text.strip().lstrip("v"), ""


def _probe_sab(service, values, client):
    url = values[service.url.env]
    response = _request(client, "GET", f"{url}/sabnzbd/api", params={"mode": "version", "output": "json"})
    _expect_ok(response, service, "SAB_API_KEY")
    version = str((_json(response, service.name) or {}).get("version") or "")
    response = _request(client, "GET", f"{url}/sabnzbd/api",
                        params={"mode": "queue", "limit": 0, "output": "json", "apikey": values["SAB_API_KEY"]})
    _expect_ok(response, service, "SAB_API_KEY")
    data = _json(response, service.name)
    if isinstance(data, dict) and data.get("status") is False:
        raise ProbeFailed(f"API key rejected: check SAB_API_KEY ({service.key_help})")
    return version, ""


def _probe_jellyfin(service, values, client):
    url = values[service.url.env]
    response = _request(client, "GET", f"{url}/System/Info/Public")
    _expect_ok(response, service, "JELLYFIN_API_KEY")
    data = _json(response, service.name)
    if not isinstance(data, dict) or "Version" not in data:
        raise ProbeFailed("this URL does not look like Jellyfin: check JELLYFIN_URL")
    version = str(data.get("Version") or "")
    key = values["JELLYFIN_API_KEY"]
    if not key:
        return version, ""
    response = _request(client, "GET", f"{url}/System/Info",
                        headers={"Authorization": f'MediaBrowser Token="{key}"'})
    _expect_ok(response, service, f"JELLYFIN_API_KEY ({service.key_help})")
    return version, ""


def _probe_romm(service, values, client):
    url = values[service.url.env]
    response = _request(client, "GET", f"{url}/api/heartbeat")
    _expect_ok(response, service, "ROMM_API_TOKEN")
    data = _json(response, service.name)
    version = str(((data or {}).get("SYSTEM") or {}).get("VERSION") or "") if isinstance(data, dict) else ""
    headers, auth = {}, None
    if values["ROMM_API_TOKEN"]:
        headers["Authorization"] = f"Bearer {values['ROMM_API_TOKEN']}"
    elif values["ROMM_USER"] and values["ROMM_PASS"]:
        auth = httpx.BasicAuth(values["ROMM_USER"], values["ROMM_PASS"])
    else:
        return version, ""
    response = _request(client, "GET", f"{url}/api/platforms", headers=headers, auth=auth)
    _expect_ok(response, service, "ROMM_API_TOKEN or ROMM_USER/ROMM_PASS")
    return version, ""


def _probe_gamevault(service, values, client):
    url = values[service.url.env]
    response = _request(client, "GET", f"{url}/api/games", params={"page": 1, "limit": 1},
                        headers={"X-Api-Key": values["GAMEVAULT_API_KEY"]})
    _expect_ok(response, service, f"GAMEVAULT_API_KEY ({service.key_help})")
    _json(response, service.name)
    return "", ""


def _probe_seerr(service, values, client):
    url = values[service.url.env]
    response = _request(client, "GET", f"{url}/api/v1/status")
    _expect_ok(response, service, "SEERR_API_KEY")
    data = _json(response, service.name)
    version = str(data.get("version") or "") if isinstance(data, dict) else ""
    response = _request(client, "GET", f"{url}/api/v1/auth/me", headers={"X-Api-Key": values["SEERR_API_KEY"]})
    _expect_ok(response, service, f"SEERR_API_KEY ({service.key_help})")
    return version, ""


PROBES: dict[str, Callable] = {
    "sonarr": _probe_arr, "radarr": _probe_arr, "lidarr": _probe_arr, "prowlarr": _probe_arr,
    "bookshelf": _probe_arr, "qbittorrent": _probe_qbt_like, "rdtclient": _probe_qbt_like,
    "sabnzbd": _probe_sab, "jellyfin": _probe_jellyfin, "romm": _probe_romm,
    "gamevault": _probe_gamevault, "seerr": _probe_seerr,
}


# ── Checks ──


def make_client(transport: httpx.BaseTransport | None = None, timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)


def check_service(service: config.Service, values: Mapping[str, str], selected: Iterable[str],
                  transport: httpx.BaseTransport | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    url = values.get(service.url.env, "")
    result = CheckResult(service.key, service.name, OK, "", url=url)
    is_selected = service.key in selected
    if not url:
        result.status = NOT_CONFIGURED
        result.detail = f"not configured (set {service.url.env})"
        if is_selected and values.get("ENABLED_SERVICES", "auto").strip().lower() not in ("", "auto", "all"):
            result.status = ERROR
            result.detail = f"listed in ENABLED_SERVICES but {service.url.env} is not set"
        return result
    if not is_selected:
        result.status = DISABLED
        result.detail = "configured but excluded by ENABLED_SERVICES"
        return result

    issues = service.credential_issues(values)
    errors = [message for level, message in issues if level == "error"]
    warnings = [message for level, message in issues if level == "warning"]
    if errors:
        # Probing with missing credentials would only produce a confusing 401.
        result.status = ERROR
        result.detail = "; ".join(errors)
        return result
    try:
        with make_client(transport, timeout) as client:
            version, note = PROBES[service.key](service, values, client)
    except ProbeFailed as failure:
        result.status = ERROR
        result.detail = str(failure)
        return result
    result.version = version
    reached = f"{service.name} {version}".strip() + f" at {url}"
    notes = [reached] + ([note] if note else [])
    if warnings:
        result.status = WARNING
        notes += warnings
    result.detail = "; ".join(notes)
    return result


def run_checks(values: Mapping[str, str], selected: Iterable[str],
               transport: httpx.BaseTransport | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> list[CheckResult]:
    selected = set(selected)
    return [check_service(service, values, selected, transport, timeout) for service in config.SERVICES.values()]


def exit_code(results: Iterable[CheckResult], problems: Iterable[str]) -> int:
    return 1 if list(problems) or any(r.status == ERROR for r in results) else 0


def render_text(results: list[CheckResult], problems: list[str], config_path: str | None) -> str:
    source = f"config file {config_path} + environment" if config_path else "environment"
    lines = [f"arrstack-mcp configuration check (settings from {source})", ""]
    if problems:
        lines.append("Configuration problems:")
        lines += [f"  ❌ {problem}" for problem in problems]
        lines.append("")
    width = max(len(r.service) for r in results)
    for r in results:
        lines.append(f"  {ICONS[r.status]} {r.service:<{width}}  {r.detail}")
    counts = {status: sum(r.status == status for r in results) for status in ICONS}
    lines += ["", (
        f"{counts[OK]} ok, {counts[WARNING]} warning(s), {counts[ERROR]} error(s), "
        f"{counts[DISABLED]} disabled, {counts[NOT_CONFIGURED]} not configured"
    )]
    return "\n".join(lines) + "\n"


def render_json(results: list[CheckResult], problems: list[str], config_path: str | None) -> str:
    return json.dumps(
        {"config_file": config_path, "problems": problems, "services": [asdict(r) for r in results]},
        indent=2,
    ) + "\n"
