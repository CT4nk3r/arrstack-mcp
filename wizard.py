"""
Interactive setup for arrstack-mcp (`python server.py --setup`).

Walks through each service, asks for its URL and credentials (secrets via
getpass, never echoed), tests them immediately with the same probes as
`--check`, then writes a private config file and prints ready-to-paste
snippets for MCP clients.

All I/O goes through the ``Prompter`` and an injectable HTTP transport, so the
whole flow is unit-tested with scripted answers and ``httpx.MockTransport``.
"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

import httpx

import config
import doctor
from discover import Found


@dataclass
class Prompter:
    ask_fn: Callable[[str], str] = input
    secret_fn: Callable[[str], str] = getpass.getpass
    out: Callable[[str], None] = print

    def ask(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self.ask_fn(f"{question}{suffix}: ").strip()
        return answer or default

    def secret(self, question: str, has_current: bool) -> str | None:
        """Returns None to keep the current value."""
        suffix = " [Enter keeps the current value]" if has_current else ""
        answer = self.secret_fn(f"{question}{suffix}: ").strip()
        return answer or (None if has_current else "")

    def confirm(self, question: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.ask_fn(f"{question} [{hint}] ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")
            self.out("Please answer y or n.")

    def choose(self, question: str, options: str, default: str) -> str:
        while True:
            answer = (self.ask_fn(f"{question} ").strip().lower() or default)[:1]
            if answer in options:
                return answer
            self.out(f"Please answer one of: {', '.join(options)}.")


@dataclass
class SetupResult:
    path: Path
    values: dict[str, str]
    configured: list[str] = field(default_factory=list)
    backup: Path | None = None


def _default_url(service: config.Service, in_docker: bool, found: Found | None) -> str:
    if found and found.url:
        return found.url
    host = service.hostnames[0] if in_docker else "localhost"
    return f"http://{host}:{service.default_port}"


def _configure_service(service: config.Service, values: dict[str, str], prompter: Prompter,
                       in_docker: bool, found: Found | None, transport, timeout: float) -> bool:
    """Ask for one service's settings until they test OK or the user keeps/skips them."""
    original = {s.env: values.get(s.env, "") for s in service.settings}
    while True:
        url = prompter.ask(f"  {service.name} URL", values.get(service.url.env) or _default_url(service, in_docker, found))
        values[service.url.env] = url.strip().rstrip("/")
        for setting in service.settings[1:]:
            if setting.secret:
                discovered = found.api_key if (found and setting.key == "api_key") else ""
                if discovered and not values.get(setting.env):
                    use = prompter.confirm(f"  Use the {setting.env} found in {found.source}?", True)
                    if use:
                        values[setting.env] = discovered
                        continue
                answer = prompter.secret(f"  {setting.description}", bool(values.get(setting.env)))
                if answer is not None:
                    values[setting.env] = answer
            else:
                values[setting.env] = prompter.ask(f"  {setting.description}", values.get(setting.env) or setting.default)

        problem = config.url_problem(service.url, values[service.url.env]) if values[service.url.env] else "URL is empty"
        if problem:
            prompter.out(f"  ❌ {problem}")
            result = None
        else:
            result = doctor.check_service(service, values, {service.key}, transport=transport, timeout=timeout)
            prompter.out(f"  {doctor.ICONS[result.status]} {result.detail}")
        if result is not None and result.status in (doctor.OK, doctor.WARNING):
            return True
        choice = prompter.choose("  r) re-enter  k) keep anyway  s) skip this service [r]:", "rks", "r")
        if choice == "k":
            return True
        if choice == "s":
            values.update(original)
            return False


def _write_private(path: Path, text: str) -> Path | None:
    """Write with mode 600; back up an existing file first. Returns the backup path."""
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak.{datetime.now():%Y%m%d%H%M%S}")
        path.replace(backup)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return backup


def run_setup(loaded: config.LoadedConfig, output: Path, fmt: str = "toml", prompter: Prompter | None = None,
              discovered: Mapping[str, Found] | None = None, transport: httpx.BaseTransport | None = None,
              timeout: float = doctor.DEFAULT_TIMEOUT) -> SetupResult | None:
    prompter = prompter or Prompter()
    discovered = discovered or {}
    # Start from what a config file already holds; values that only come from the
    # environment stay there instead of being copied into a file.
    values = {
        name: value for name, value in loaded.values.items()
        if loaded.sources.get(name, "").startswith("config file")
    }
    defaults = {s.env: s.default for s in config.ALL_SETTINGS}
    working = {**defaults, **values}

    prompter.out("arrstack-mcp setup: answer a few questions per service; each one is tested right away.")
    prompter.out("Secrets are read without echo and written only to the output file (mode 600).\n")
    if discovered:
        prompter.out("Discovered:")
        for item in discovered.values():
            prompter.out(f"  • {item.describe()}")
        prompter.out("")
    in_docker = prompter.confirm(
        "Will arrstack-mcp run in Docker on the same network as your services?", bool(discovered)
    )

    configured = []
    for service in config.SERVICES.values():
        present = bool(working.get(service.url.env)) or service.key in discovered
        label = f"{service.name} ({service.label})"
        if not prompter.confirm(f"\nSet up {label}?", present):
            continue
        if _configure_service(service, working, prompter, in_docker, discovered.get(service.key), transport, timeout):
            configured.append(service.key)

    if not configured and not any(working.get(s.url.env) for s in config.SERVICES.values()):
        prompter.out("\nNothing configured; no file written.")
        return None

    enabled = working.get("ENABLED_SERVICES", "auto").strip().lower()
    if enabled not in ("", "auto", "all"):
        listed = [item.strip() for item in enabled.split(",") if item.strip()]
        listed += [key for key in configured if key not in {config.SERVICE_ALIASES.get(i, i) for i in listed}]
        working["ENABLED_SERVICES"] = ",".join(listed)

    allow = prompter.confirm("\nAllow tools that delete downloads or library files?",
                             working.get("ALLOW_DESTRUCTIVE", "true").lower() != "false")
    working["ALLOW_DESTRUCTIVE"] = "true" if allow else "false"

    output = output.expanduser().resolve()
    if output.exists() and not prompter.confirm(f"\n{output} exists. Replace it (a backup is kept)?", True):
        prompter.out("Not written.")
        return None
    text = config.render_toml(working) if fmt == "toml" else config.render_env(working)
    backup = _write_private(output, text)
    prompter.out(f"\n✅ Wrote {output} (mode 600)" + (f"; previous version saved as {backup.name}" if backup else ""))
    prompter.out(client_snippets(output, fmt))
    return SetupResult(output, working, configured, backup)


def client_snippets(output: Path, fmt: str, server_path: Path | None = None, python: str | None = None) -> str:
    server_path = server_path or Path(__file__).resolve().parent / "server.py"
    python = python or sys.executable
    lines = ["", "── Next steps ──", ""]
    if fmt == "toml":
        args = [str(server_path), "--config", str(output)]
        stdio = {"command": python, "args": args}
        lines += [
            "Claude Desktop / Cursor (mcpServers in the client's JSON config):",
            json.dumps({"mcpServers": {"arrstack": stdio}}, indent=2),
            "",
            "VS Code (.vscode/mcp.json):",
            json.dumps({"servers": {"arrstack": {"type": "stdio", **stdio}}}, indent=2),
            "",
            "Claude Code:",
            "  claude mcp add arrstack -- " + " ".join(shlex.quote(part) for part in [python, *args]),
            "",
            "Docker: mount the file and point the server at it:",
            "  volumes:",
            f"    - {output}:/config/arrstack.toml:ro",
            "  environment:",
            "    - ARRSTACK_CONFIG=/config/arrstack.toml",
        ]
    else:
        lines += [
            "Docker Compose: load the file into the container:",
            "  env_file:",
            f"    - {output}",
            "",
            "Other clients: export these variables in the server's environment.",
        ]
    lines += [
        "",
        "HTTP clients (OpenClaw, remote agents): run with --transport streamable-http and connect to",
        "  http://<host>:8000/mcp   e.g.",
        "  openclaw mcp set arrstack '{\"url\":\"http://arrstack-mcp:8000/mcp\",\"transport\":\"streamable-http\"}'",
        "",
        "Re-test any time: python server.py --check" + (f" --config {shlex.quote(str(output))}" if fmt == "toml" else ""),
    ]
    return "\n".join(lines)
