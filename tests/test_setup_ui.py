import os
import pathlib
import stat
import tempfile
import unittest
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from arrstack_mcp import config
from arrstack_mcp import setup_ui

TOKEN = "test-token-123"
SECRET = "ui-secret-abcdef"


def sonarr_ok(request):
    if request.url.path.endswith("/api/v3/system/status"):
        if request.headers.get("x-api-key") == SECRET:
            return httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.9"})
        return httpx.Response(401)
    if request.url.path.endswith("/ping") and request.url.host == "sonarr":
        return httpx.Response(200, json={"status": "OK"})
    raise httpx.ConnectError("unreachable")


class SetupUITestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.output = self.tmp / "arrstack.toml"
        self.restarts = []
        timer = patch("arrstack_mcp.setup_ui.threading.Timer", side_effect=self._fake_timer)
        timer.start()
        self.addCleanup(timer.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_timer(self, delay, fn, args=()):
        test = self

        class Immediate:
            def start(self):
                test.restarts.append(args)

        return Immediate()

    def client(self, loaded=None):
        self.ui = setup_ui.SetupUI(
            loaded or config.load({}), self.output, restart=lambda path: None,
            transport=httpx.MockTransport(sonarr_ok), token=TOKEN,
        )
        app = Starlette(routes=[Route(p, h, methods=m) for p, m, h in self.ui.routes()])
        return TestClient(app)

    def post(self, client, path, body):
        return client.post(path, json=body, headers={"X-Setup-Token": TOKEN})


class AccessTests(SetupUITestCase):
    def test_page_needs_the_token(self):
        client = self.client()
        self.assertEqual(client.get("/setup").status_code, 403)
        self.assertEqual(client.get("/setup?token=wrong").status_code, 403)
        page = client.get(f"/setup?token={TOKEN}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'const TOKEN = "{TOKEN}";', page.text)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])

    def test_api_needs_the_header_not_a_query_token(self):
        client = self.client()
        self.assertEqual(client.get("/setup/api/state").status_code, 403)
        self.assertEqual(client.get(f"/setup/api/state?token={TOKEN}").status_code, 403)
        self.assertEqual(client.post("/setup/api/save", json={"enabled": ["sonarr"]}).status_code, 403)
        self.assertEqual(client.get("/setup/api/state", headers={"X-Setup-Token": TOKEN}).status_code, 200)


class StateTests(SetupUITestCase):
    def test_secrets_are_never_returned(self):
        loaded = config.load({"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": SECRET})
        response = self.client(loaded).get("/setup/api/state", headers={"X-Setup-Token": TOKEN})
        self.assertNotIn(SECRET, response.text)
        sonarr = next(s for s in response.json()["services"] if s["key"] == "sonarr")
        key = next(f for f in sonarr["fields"] if f["env"] == "SONARR_API_KEY")
        self.assertEqual(key["value"], "")
        self.assertTrue(key["is_set"])
        self.assertTrue(sonarr["enabled"])
        self.assertEqual(sonarr["group"], "Movies & TV")

    def test_environment_values_are_locked(self):
        loaded = config.load({"SONARR_URL": "http://sonarr:8989", "ENABLED_SERVICES": "sonarr"})
        state = self.client(loaded).get("/setup/api/state", headers={"X-Setup-Token": TOKEN}).json()
        sonarr = next(s for s in state["services"] if s["key"] == "sonarr")
        self.assertTrue(sonarr["fields"][0]["locked"])
        self.assertTrue(state["enabled_locked"])

    def test_every_service_is_listed_with_a_group(self):
        state = self.client().get("/setup/api/state", headers={"X-Setup-Token": TOKEN}).json()
        self.assertEqual({s["key"] for s in state["services"]}, set(config.SERVICES))
        self.assertNotIn("Other", {s["group"] for s in state["services"]})


class TestEndpointTests(SetupUITestCase):
    def test_tests_submitted_values(self):
        result = self.post(self.client(), "/setup/api/test", {
            "service": "sonarr", "values": {"SONARR_URL": "http://sonarr:8989/", "SONARR_API_KEY": SECRET},
        }).json()
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("Sonarr 4.0.9", result["detail"])

    def test_blank_secret_keeps_the_saved_one(self):
        loaded = config.load({}, None)
        loaded.values.update(SONARR_URL="http://sonarr:8989", SONARR_API_KEY=SECRET)
        loaded.sources.update(SONARR_URL="config file", SONARR_API_KEY="config file")
        result = self.post(self.client(loaded), "/setup/api/test", {
            "service": "sonarr", "values": {"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": ""},
        }).json()
        self.assertEqual(result["status"], "ok", result)

    def test_bad_url_and_unknown_service(self):
        client = self.client()
        result = self.post(client, "/setup/api/test", {"service": "sonarr", "values": {"SONARR_URL": "sonarr:8989"}})
        self.assertIn("not an http(s) URL", result.json()["detail"])
        self.assertEqual(self.post(client, "/setup/api/test", {"service": "nope"}).status_code, 400)


class SaveTests(SetupUITestCase):
    def test_save_writes_private_file_and_restarts(self):
        response = self.post(self.client(), "/setup/api/save", {
            "enabled": ["sonarr"], "allow_destructive": False,
            "values": {"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": SECRET},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(stat.S_IMODE(os.stat(self.output).st_mode), 0o600)
        saved = config.load({}, str(self.output)).values
        self.assertEqual(saved["ENABLED_SERVICES"], "sonarr")
        self.assertEqual(saved["SONARR_API_KEY"], SECRET)
        self.assertEqual(saved["ALLOW_DESTRUCTIVE"], "false")
        self.assertEqual(self.restarts, [(self.output,)])
        self.assertEqual(response.json()["mcp_url"], "http://testserver/mcp")

    def test_environment_values_are_not_copied_into_the_file(self):
        loaded = config.load({"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": SECRET})
        self.post(self.client(loaded), "/setup/api/save", {
            "enabled": ["sonarr"], "values": {"SONARR_URL": "http://evil:1", "SONARR_API_KEY": "other"},
        })
        text = self.output.read_text()
        self.assertNotIn(SECRET, text)
        self.assertNotIn("evil", text)  # locked fields can't be overridden from the page
        self.assertIn('enabled_services = "sonarr"', text)

    def test_unpicked_services_keep_values_but_are_disabled(self):
        self.output.write_text('[radarr]\nurl = "http://radarr:7878"\napi_key = "rk"\n')
        loaded = config.load({}, str(self.output))
        self.post(self.client(loaded), "/setup/api/save", {
            "enabled": ["sonarr"], "values": {"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": SECRET},
        })
        saved = config.load({}, str(self.output)).values
        self.assertEqual(saved["RADARR_API_KEY"], "rk")
        self.assertEqual(saved["ENABLED_SERVICES"], "sonarr")
        self.assertTrue(list(self.tmp.glob("arrstack.toml.bak.*")))

    def test_save_validation(self):
        client = self.client()
        self.assertEqual(self.post(client, "/setup/api/save", {"enabled": []}).status_code, 400)
        missing = self.post(client, "/setup/api/save", {"enabled": ["sonarr"], "values": {}})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("URL is empty", missing.json()["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.restarts, [])


class DiscoverTests(SetupUITestCase):
    def test_discover_returns_urls_only(self):
        found = self.post(self.client(), "/setup/api/discover", {}).json()
        self.assertEqual(found, {"sonarr": "http://sonarr:8989"})


if __name__ == "__main__":
    unittest.main()
