"""
Web setup page for arrstack-mcp (served at /setup on the HTTP transports).

Pick the services you use, fill in URLs and API keys, test each one, save.
The page writes the same TOML config file as `--setup` and restarts the
server so the new tool list takes effect.

Security model (the server itself has no authentication):

* Every request needs a random one-time token that is printed to the server
  log at startup, so only someone who can read the log (e.g. `docker logs`)
  can open the page. The token arrives once in the URL; the page strips it
  from the address bar and sends it as an `X-Setup-Token` header, which other
  websites cannot set, so cross-site requests can't save anything.
* Stored secrets are never sent back to the browser: fields only report
  whether a value is set, and a blank secret field keeps the current value.
* Values that come from environment variables (e.g. a compose file) are
  shown as locked: they would override the file anyway, and they are never
  copied into it.
* Disable the page entirely with SETUP_UI=false.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

import config
import discover
import doctor
import wizard

PAGE = Path(__file__).with_name("setup.html")
_LOCKED_SOURCES = ("environment",)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}
_FORBIDDEN = """<!doctype html><meta charset="utf-8"><title>arrstack-mcp setup</title>
<body style="font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem">
<h1>Setup link required</h1>
<p>Open the setup link printed in the server log, for example with
<code>docker logs arrstack-mcp</code>. It looks like
<code>http://&lt;server&gt;:8000/setup?token=…</code> and changes on every restart.</p>"""


def selected_services(values) -> set[str]:
    raw = values.get("ENABLED_SERVICES", "auto").strip().lower()
    if raw in ("", "auto"):
        return {k for k, s in config.SERVICES.items() if values.get(s.url.env)}
    if raw == "all":
        return set(config.SERVICES)
    keys = {config.SERVICE_ALIASES.get(i.strip(), i.strip()) for i in raw.split(",")}
    return keys & set(config.SERVICES)


class SetupUI:
    def __init__(self, loaded: config.LoadedConfig, output: Path, restart: Callable[[Path], None],
                 transport: httpx.BaseTransport | None = None, token: str | None = None,
                 timeout: float = doctor.DEFAULT_TIMEOUT, mcp_path: str = "/mcp"):
        self.loaded = loaded
        self.output = Path(output)
        self.restart = restart
        self.transport = transport
        self.token = token or secrets.token_urlsafe(18)
        self.timeout = timeout
        self.mcp_path = mcp_path

    # ── helpers ──

    def _authorized(self, request: Request, allow_query: bool = False) -> bool:
        supplied = request.headers.get("x-setup-token", "")
        if not supplied and allow_query:
            supplied = request.query_params.get("token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def _locked(self, env: str) -> bool:
        return self.loaded.sources.get(env, "default") in _LOCKED_SOURCES or \
            self.loaded.sources.get(env, "").endswith("_FILE")

    def _merge(self, submitted: dict) -> dict[str, str]:
        """Current values with the submitted edits applied (blank secret = keep)."""
        merged = dict(self.loaded.values)
        known = {s.env: s for s in config.ALL_SETTINGS}
        for env, value in (submitted or {}).items():
            setting = known.get(env)
            if setting is None or self._locked(env) or not isinstance(value, str):
                continue
            value = value.strip()
            if setting.secret and not value:
                continue
            merged[env] = value.rstrip("/") if setting.url else value
        return merged

    @staticmethod
    async def _json(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _denied(self) -> JSONResponse:
        return JSONResponse({"error": "setup token missing or wrong; open the link from the server log"},
                            status_code=403)

    # ── views ──

    def state(self) -> dict:
        values = self.loaded.values
        enabled = selected_services(values)
        services = []
        ordered = sorted(config.SERVICES.values(), key=lambda s: config.GROUP_ORDER.index(s.group))
        for service in ordered:
            fields = []
            for setting in service.settings:
                value = values.get(setting.env, "")
                fields.append({
                    "env": setting.env,
                    "label": setting.description.replace("`", ""),  # Markdown in the README
                    "secret": setting.secret,
                    "url": setting.url,
                    "need": "required" if setting.url else setting.need,
                    "value": "" if setting.secret else value,
                    "is_set": bool(value),
                    "locked": self._locked(setting.env),
                    "placeholder": setting.example_value or ("" if setting.secret else setting.default),
                })
            services.append({
                "key": service.key, "name": service.name, "label": service.label, "group": service.group,
                "key_help": service.key_help, "enabled": service.key in enabled, "fields": fields,
            })
        return {
            "services": services,
            "config_path": str(self.output),
            "allow_destructive": values.get("ALLOW_DESTRUCTIVE", "true").lower() != "false",
            # Set by the environment: the page's choices would be overridden.
            "enabled_locked": self._locked("ENABLED_SERVICES"),
            "destructive_locked": self._locked("ALLOW_DESTRUCTIVE"),
            "problems": list(self.loaded.problems),
        }

    async def page(self, request: Request):
        if not self._authorized(request, allow_query=True):
            return HTMLResponse(_FORBIDDEN, status_code=403, headers=_SECURITY_HEADERS)
        html = PAGE.read_text(encoding="utf-8").replace("__SETUP_TOKEN__", json.dumps(self.token))
        return HTMLResponse(html, headers=_SECURITY_HEADERS)

    async def api_state(self, request: Request):
        if not self._authorized(request):
            return self._denied()
        return JSONResponse(self.state(), headers=_SECURITY_HEADERS)

    async def api_test(self, request: Request):
        if not self._authorized(request):
            return self._denied()
        body = await self._json(request)
        service = config.SERVICES.get(str(body.get("service", "")))
        if service is None:
            return JSONResponse({"error": "unknown service"}, status_code=400)
        values = self._merge(body.get("values"))
        url = values.get(service.url.env, "")
        problem = config.url_problem(service.url, url) if url else f"{service.url.env} is empty"
        if problem:
            return JSONResponse({"status": doctor.ERROR, "detail": problem})
        result = doctor.check_service(service, values, {service.key}, transport=self.transport, timeout=self.timeout)
        return JSONResponse(asdict(result), headers=_SECURITY_HEADERS)

    async def api_discover(self, request: Request):
        if not self._authorized(request):
            return self._denied()
        found = discover.discover_network(transport=self.transport)
        return JSONResponse({key: item.url for key, item in found.items()}, headers=_SECURITY_HEADERS)

    async def api_save(self, request: Request):
        if not self._authorized(request):
            return self._denied()
        body = await self._json(request)
        enabled = [k for k in body.get("enabled", []) if k in config.SERVICES]
        if not enabled:
            return JSONResponse({"error": "pick at least one service"}, status_code=400)
        merged = self._merge(body.get("values"))
        errors = []
        for key in enabled:
            setting = config.SERVICES[key].url
            url = merged.get(setting.env, "")
            problem = config.url_problem(setting, url) if url else f"{config.SERVICES[key].name}: URL is empty"
            if problem:
                errors.append(problem)
        if errors:
            return JSONResponse({"error": "; ".join(errors)}, status_code=400)

        # What goes in the file: what the file already held, plus this page's
        # edits. Environment values stay in the environment.
        file_values = {
            env: value for env, value in self.loaded.values.items()
            if self.loaded.sources.get(env, "").startswith("config file")
        }
        for env, value in merged.items():
            if value != self.loaded.values.get(env) and not self._locked(env):
                file_values[env] = value
        file_values["ENABLED_SERVICES"] = ",".join(enabled)
        file_values["ALLOW_DESTRUCTIVE"] = "false" if body.get("allow_destructive") is False else "true"
        try:
            backup = wizard.write_private(self.output, config.render_toml(file_values))
        except OSError as error:
            return JSONResponse({"error": f"cannot write {self.output}: {error.strerror or error}"},
                                status_code=500)

        base = f"{request.url.scheme}://{request.url.netloc}"
        threading.Timer(1.0, self.restart, args=(self.output,)).start()
        return JSONResponse({
            "ok": True,
            "path": str(self.output),
            "backup": str(backup) if backup else None,
            "mcp_url": base + self.mcp_path,
        }, headers=_SECURITY_HEADERS)

    def routes(self) -> list[tuple[str, list[str], Callable]]:
        return [
            ("/setup", ["GET"], self.page),
            ("/setup/api/state", ["GET"], self.api_state),
            ("/setup/api/test", ["POST"], self.api_test),
            ("/setup/api/discover", ["POST"], self.api_discover),
            ("/setup/api/save", ["POST"], self.api_save),
        ]
