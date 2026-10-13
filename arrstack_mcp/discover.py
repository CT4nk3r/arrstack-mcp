"""
Opt-in service discovery for `python server.py --setup --discover` / `--discover`.

Two sources, both explicit:

* **Network probing** of a fixed candidate list per service: its common Docker
  container names plus ``localhost``, on the service's default port, with and
  without a ``/<service>`` URL base. Each candidate is identified by an
  unauthenticated endpoint (``/ping`` for the *arr apps, Jellyfin's public
  system info, ...). There is no subnet or port scanning.
* **\\*arr config files** (``config.xml`` of Sonarr/Radarr/Lidarr/Prowlarr/
  Readarr-based apps) in directories the user names with ``--arr-config``.
  They hold the API key, port and URL base. Keys are only handed to the setup
  wizard as defaults; they are never printed.

The HTTP transport is injectable so tests run against ``httpx.MockTransport``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx

from . import config

DEFAULT_TIMEOUT = 1.5
_ARR_BASED = ("sonarr", "radarr", "lidarr", "prowlarr", "bookshelf")


@dataclass
class Found:
    service: str
    url: str = ""
    api_key: str = ""  # only from config.xml; never printed
    source: str = ""  # "network" or the config.xml path

    def describe(self) -> str:
        parts = [self.url or "(address unknown)"]
        if self.api_key:
            parts.append("API key found")
        return f"{self.service}: " + ", ".join(parts) + f"  [{self.source}]"


# ── Fingerprints: does this base URL answer like the service? ──


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _looks_arr(client, base):
    data = _json(client.get(f"{base}/ping"))
    return isinstance(data, dict) and str(data.get("status", "")).upper() == "OK"


def _looks_qbt(client, base):
    response = client.get(f"{base}/api/v2/app/version")
    # 403 = API present but needs login; 200 = auth bypass for this client.
    return response.status_code == 403 or (
        response.status_code == 200 and response.text.strip()[:1] in ("v", "1", "2", "3", "4", "5")
    )


def _looks_sab(client, base):
    data = _json(client.get(f"{base}/sabnzbd/api", params={"mode": "version", "output": "json"}))
    return isinstance(data, dict) and "version" in data


def _looks_jellyfin(client, base):
    data = _json(client.get(f"{base}/System/Info/Public"))
    return isinstance(data, dict) and "Version" in data and "jellyfin" in str(data.get("ProductName", "")).lower()


def _looks_romm(client, base):
    data = _json(client.get(f"{base}/api/heartbeat"))
    return isinstance(data, dict) and "SYSTEM" in data


def _looks_gamevault(client, base):
    response = client.get(f"{base}/api/games", params={"limit": 1})
    data = _json(response)
    # NestJS answers {"statusCode": 401, ...} without a key.
    return isinstance(data, dict) and (response.status_code == 200 or data.get("statusCode") == 401)


def _looks_seerr(client, base):
    data = _json(client.get(f"{base}/api/v1/status"))
    return isinstance(data, dict) and "version" in data and "commitTag" in data


FINGERPRINTS: dict[str, Callable[[httpx.Client, str], bool]] = {
    **{key: _looks_arr for key in _ARR_BASED},
    "qbittorrent": _looks_qbt, "rdtclient": _looks_qbt, "sabnzbd": _looks_sab,
    "jellyfin": _looks_jellyfin, "romm": _looks_romm, "gamevault": _looks_gamevault,
    "seerr": _looks_seerr,
}


def candidates(service: config.Service) -> list[str]:
    """Base URLs to try for a service, most specific first."""
    hosts = list(service.hostnames) + ["localhost"]
    bases = ["", f"/{service.key}"] if service.key in _ARR_BASED + ("jellyfin",) else [""]
    return [f"http://{host}:{service.default_port}{base}" for host in hosts for base in bases]


def _probe(client: httpx.Client, key: str, base: str) -> bool:
    try:
        return FINGERPRINTS[key](client, base)
    except httpx.HTTPError:
        return False


def discover_network(services: Iterable[str] | None = None, transport: httpx.BaseTransport | None = None,
                     timeout: float = DEFAULT_TIMEOUT) -> dict[str, Found]:
    """First matching candidate per service (in candidate order)."""
    keys = list(services) if services is not None else list(config.SERVICES)
    jobs = [(key, base) for key in keys for base in candidates(config.SERVICES[key])]
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
        with ThreadPoolExecutor(max_workers=16) as pool:
            hits = list(pool.map(lambda job: _probe(client, *job), jobs))
    found: dict[str, Found] = {}
    for (key, base), hit in zip(jobs, hits):
        if hit and key not in found:
            found[key] = Found(key, url=base, source="network")
    return found


# ── *arr config.xml ──

_INSTANCE_TO_SERVICE = {"sonarr": "sonarr", "radarr": "radarr", "lidarr": "lidarr",
                        "prowlarr": "prowlarr", "readarr": "bookshelf", "bookshelf": "bookshelf"}


def read_arr_config(path: str | Path) -> Found | None:
    """Parse an *arr config.xml (or a directory containing one)."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "config.xml"
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None

    def text(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None else ""

    hint = " ".join([text("InstanceName"), path.parent.name]).lower()
    service = next((svc for name, svc in _INSTANCE_TO_SERVICE.items() if name in hint), None)
    if service is None:
        return None
    port = text("Port") or str(config.SERVICES[service].default_port)
    base = text("UrlBase").strip("/")
    host = config.SERVICES[service].hostnames[0]
    url = f"http://{host}:{port}" + (f"/{base}" if base else "")
    return Found(service, url=url, api_key=text("ApiKey"), source=str(path))


def discover(arr_config_dirs: Iterable[str] = (), network: bool = True,
             transport: httpx.BaseTransport | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Found]:
    """Combine both sources. A network hit's URL wins (it was verified); config.xml adds the key."""
    found: dict[str, Found] = {}
    for directory in arr_config_dirs:
        item = read_arr_config(directory)
        if item:
            found[item.service] = item
    if network:
        for key, hit in discover_network(transport=transport, timeout=timeout).items():
            if key in found:
                found[key].url = hit.url
                found[key].source += " + network"
            else:
                found[key] = hit
    return found
