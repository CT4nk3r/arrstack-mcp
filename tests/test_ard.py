import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

import ard
import server

REPO_ROOT = Path(__file__).resolve().parent.parent

# A tiny stand-in MCP server so capability/tool assertions don't depend on the
# full 94-tool arrstack catalog.
TOY_CONFIG = {
    "radarr": ("Radarr", "http://radarr", "radarr_"),
    "romm": ("RomM", "http://romm", "romm_"),
}


def _toy_mcp():
    m = FastMCP("toytools", instructions="Toy homelab server.")

    @m.tool()
    def beta_do(name: str = "n") -> str:
        """Do beta."""
        return ""

    @m.tool()
    def alpha_do(count: int) -> str:
        """Do alpha."""
        return ""

    return m


class HelperTests(unittest.TestCase):
    def test_resolve_publisher_prefers_explicit_domain(self):
        self.assertEqual(ard.resolve_publisher(domain="Example.COM"), "example.com")

    def test_resolve_publisher_falls_back_to_url_host(self):
        publisher = ard.resolve_publisher(public_url="https://a.b.example.com:8443/x")
        self.assertEqual(publisher, "a.b.example.com")

    def test_resolve_publisher_defaults_to_localhost(self):
        self.assertEqual(ard.resolve_publisher(), ard.FALLBACK_PUBLISHER)

    def test_resolve_publisher_rejects_non_urn_safe_host(self):
        # IPv6 literals contain ':' which is invalid in the URN publisher segment.
        self.assertEqual(ard.resolve_publisher(domain="::1"), ard.FALLBACK_PUBLISHER)

    def test_normalize_public_url(self):
        self.assertEqual(ard.normalize_public_url("https://h/"), "https://h")
        self.assertIsNone(ard.normalize_public_url(""))
        self.assertIsNone(ard.normalize_public_url(None))

    def test_endpoint_url_per_transport(self):
        self.assertEqual(ard.endpoint_url("https://h", "streamable-http"), "https://h/mcp")
        self.assertEqual(ard.endpoint_url("https://h", "sse"), "https://h/sse")
        self.assertIsNone(ard.endpoint_url(None))

    def test_server_card_url(self):
        self.assertEqual(
            ard.server_card_url("https://h/"),
            "https://h/.well-known/mcp-server-card.json",
        )

    def test_representative_queries_reflect_enabled_services(self):
        queries = ard.representative_queries({"radarr"})
        self.assertEqual(queries[0], ard._SERVICE_QUERIES["radarr"])
        self.assertGreaterEqual(len(queries), ard.MIN_REPRESENTATIVE_QUERIES)

    def test_representative_queries_capped_at_max(self):
        queries = ard.representative_queries(set(server.SERVICE_CONFIG))
        self.assertEqual(len(queries), ard.MAX_REPRESENTATIVE_QUERIES)
        self.assertEqual(len(queries), len(set(queries)))

    def test_representative_queries_pads_when_no_services(self):
        self.assertEqual(ard.representative_queries(set()), ard._GENERAL_QUERIES)


class ServerCardTests(unittest.TestCase):
    def setUp(self):
        self.mcp = _toy_mcp()

    def test_card_shape_without_public_url(self):
        card = ard.build_server_card(
            self.mcp, enabled_services={"radarr"}, service_config=TOY_CONFIG
        )
        self.assertEqual(card["name"], "toytools")
        self.assertEqual(card["instructions"], "Toy homelab server.")
        self.assertNotIn("url", card)  # no connection hint without a public URL
        names = [t["name"] for t in card["tools"]]
        self.assertEqual(names, sorted(names))  # tools are sorted
        for tool in card["tools"]:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_card_includes_connection_hint_with_public_url(self):
        card = ard.build_server_card(
            self.mcp,
            enabled_services={"radarr"},
            service_config=TOY_CONFIG,
            public_url="https://arrstack.example.com/",
            transport="streamable-http",
        )
        self.assertEqual(card["url"], "https://arrstack.example.com/mcp")
        self.assertEqual(card["transport"], "streamable-http")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.mcp = _toy_mcp()

    def _build(self, **kwargs):
        return ard.build_catalog(
            self.mcp,
            enabled_services=set(TOY_CONFIG),
            service_config=TOY_CONFIG,
            **kwargs,
        )

    def test_embedded_catalog_is_valid_and_self_contained(self):
        catalog = self._build()
        self.assertEqual(ard.validate_catalog(catalog), [])
        entry = catalog["entries"][0]
        self.assertIn("data", entry)
        self.assertNotIn("url", entry)
        self.assertEqual(entry["identifier"], "urn:air:localhost:server:arrstack")
        self.assertRegex(entry["identifier"], ard.URN_PATTERN)
        self.assertNotIn("identifier", catalog["host"])  # no did:web for localhost

    def test_reference_catalog_uses_url_and_did_web(self):
        catalog = self._build(
            public_url="https://arrstack.example.com",
            host_name="My Homelab",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(ard.validate_catalog(catalog), [])
        entry = catalog["entries"][0]
        self.assertEqual(entry["url"], "https://arrstack.example.com/.well-known/mcp-server-card.json")
        self.assertNotIn("data", entry)
        self.assertEqual(entry["identifier"], "urn:air:arrstack.example.com:server:arrstack")
        self.assertEqual(entry["updatedAt"], "2026-01-01T00:00:00Z")
        self.assertEqual(entry["metadata"]["endpoint"], "https://arrstack.example.com/mcp")
        self.assertEqual(catalog["host"]["identifier"], "did:web:arrstack.example.com")

    def test_embed_card_override_forces_inline_data(self):
        catalog = self._build(public_url="https://arrstack.example.com", embed_card=True)
        entry = catalog["entries"][0]
        self.assertIn("data", entry)
        self.assertNotIn("url", entry)

    def test_capabilities_and_tags(self):
        catalog = self._build()
        entry = catalog["entries"][0]
        self.assertEqual(entry["capabilities"], ["alpha_do", "beta_do"])
        for slug in ("radarr", "romm", "mcp", "homelab"):
            self.assertIn(slug, entry["tags"])

    def test_validator_flags_value_or_reference_violation(self):
        catalog = self._build()
        catalog["entries"][0]["url"] = "https://x/card.json"  # now has both url and data
        problems = ard.validate_catalog(catalog)
        self.assertTrue(any("exactly one" in p for p in problems))

    def test_validator_flags_bad_urn(self):
        catalog = self._build()
        catalog["entries"][0]["identifier"] = "not-a-urn"
        self.assertTrue(any("urn:air" in p for p in ard.validate_catalog(catalog)))


class ExampleArtifactTests(unittest.TestCase):
    def test_committed_example_catalog_is_valid(self):
        path = REPO_ROOT / "examples" / "ai-catalog.json"
        catalog = json.loads(path.read_text())
        self.assertEqual(ard.validate_catalog(catalog), [])


class WellKnownRouteTests(unittest.TestCase):
    """Exercise the live /.well-known routes through the real HTTP app."""

    _GLOBALS = ("ENABLED_SERVICES", "ARD_ENABLED", "ARD_PUBLIC_URL", "ARD_DOMAIN", "_active_transport")

    def setUp(self):
        self._saved = {name: getattr(server, name) for name in self._GLOBALS}
        server.ENABLED_SERVICES = "all"
        server.ARD_ENABLED = "auto"
        server.ARD_PUBLIC_URL = "https://arrstack.example.com"
        server.ARD_DOMAIN = ""
        server._active_transport = "streamable-http"
        self.client = TestClient(server.mcp.streamable_http_app())

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(server, name, value)

    def test_catalog_route_served_with_cors_to_foreign_host(self):
        resp = self.client.get(
            ard.WELL_KNOWN_CATALOG_PATH, headers={"Host": "crawler.example.com"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/json")
        self.assertEqual(resp.headers["access-control-allow-origin"], "*")
        catalog = resp.json()
        self.assertEqual(ard.validate_catalog(catalog), [])
        self.assertEqual(
            catalog["entries"][0]["identifier"],
            "urn:air:arrstack.example.com:server:arrstack",
        )

    def test_server_card_route_served(self):
        resp = self.client.get(ard.WELL_KNOWN_SERVER_CARD_PATH)
        self.assertEqual(resp.status_code, 200)
        card = resp.json()
        self.assertEqual(card["name"], "arrstack")
        self.assertTrue(card["tools"])

    def test_routes_return_404_when_ard_disabled(self):
        server.ARD_ENABLED = "false"
        client = TestClient(server.mcp.streamable_http_app())
        self.assertEqual(client.get(ard.WELL_KNOWN_CATALOG_PATH).status_code, 404)
        self.assertEqual(client.get(ard.WELL_KNOWN_SERVER_CARD_PATH).status_code, 404)


class PrintCatalogCliTests(unittest.TestCase):
    def test_print_catalog_emits_valid_manifest(self):
        env = os.environ.copy()
        env["ENABLED_SERVICES"] = "sonarr,radarr,romm"
        env["SONARR_URL"] = "http://sonarr:8989"
        env["RADARR_URL"] = "http://radarr:7878"
        env["ROMM_URL"] = "http://romm:8081"
        env["ARD_PUBLIC_URL"] = "https://arrstack.example.com"

        result = subprocess.run(
            [sys.executable, "server.py", "--print-catalog"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
        )
        catalog = json.loads(result.stdout)
        self.assertEqual(ard.validate_catalog(catalog), [])
        # capabilities reflect only the three enabled services
        caps = catalog["entries"][0]["capabilities"]
        self.assertTrue(all(c.startswith(("sonarr_", "radarr_", "romm_")) for c in caps))


if __name__ == "__main__":
    unittest.main()
