import json
import os
import pathlib
import subprocess
import sys
import unittest

import httpx

from arrstack_mcp import config
from arrstack_mcp import doctor

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SECRET = "sentinel-secret-9f8e7d"


def values(**overrides):
    """Default settings plus overrides, as the server would load them."""
    loaded = config.load({}).values
    loaded.update(overrides)
    return loaded


class Router:
    """MockTransport handler: (method, path) -> response or exception; records requests."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, text="not found")
        if isinstance(handler, Exception):
            raise handler
        return handler(request) if callable(handler) else handler


def check(key, settings, routes, selected=None):
    router = Router(routes)
    result = doctor.check_service(
        config.SERVICES[key], settings, selected if selected is not None else {key},
        transport=httpx.MockTransport(router),
    )
    return result, router


class ArrProbeTests(unittest.TestCase):
    SETTINGS = values(SONARR_URL="http://sonarr:8989/sonarr", SONARR_API_KEY=SECRET)

    def test_ok_reports_version_and_sends_key(self):
        result, router = check("sonarr", self.SETTINGS, {
            ("GET", "/sonarr/api/v3/system/status"):
                httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.9.2244"}),
        })
        self.assertEqual(result.status, doctor.OK, result.detail)
        self.assertEqual(result.version, "4.0.9.2244")
        self.assertIn("Sonarr 4.0.9.2244 at http://sonarr:8989/sonarr", result.detail)
        self.assertEqual(router.requests[0].headers["X-Api-Key"], SECRET)

    def test_rejected_key(self):
        result, _ = check("sonarr", self.SETTINGS, {
            ("GET", "/sonarr/api/v3/system/status"): httpx.Response(401, text=f"bad key {SECRET}"),
        })
        self.assertEqual(result.status, doctor.ERROR)
        self.assertIn("credentials rejected (HTTP 401): check SONARR_API_KEY", result.detail)
        self.assertNotIn(SECRET, result.detail)

    def test_missing_url_base_hint(self):
        result, _ = check("sonarr", values(SONARR_URL="http://sonarr:8989", SONARR_API_KEY="k"), {})
        self.assertEqual(result.status, doctor.ERROR)
        self.assertIn("HTTP 404", result.detail)
        self.assertIn("http://sonarr:8989/sonarr", result.detail)

    def test_redirect_to_login(self):
        result, _ = check("sonarr", self.SETTINGS, {
            ("GET", "/sonarr/api/v3/system/status"):
                httpx.Response(302, headers={"location": "http://sonarr:8989/login?returnUrl=x"}),
        })
        self.assertIn("redirected to /login", result.detail)

    def test_html_instead_of_api(self):
        result, _ = check("sonarr", self.SETTINGS, {
            ("GET", "/sonarr/api/v3/system/status"):
                httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}),
        })
        self.assertIn("returned an HTML page instead of the API", result.detail)

    def test_wrong_application_at_url(self):
        result, _ = check("sonarr", self.SETTINGS, {
            ("GET", "/sonarr/api/v3/system/status"): httpx.Response(200, json={"appName": "Radarr"}),
        })
        self.assertIn("this URL is Radarr, not Sonarr", result.detail)

    def test_connection_refused_and_timeout(self):
        for error, expected in ((httpx.ConnectError("refused"), "cannot connect to sonarr:8989"),
                                (httpx.ReadTimeout("slow"), "no answer from sonarr:8989")):
            result, _ = check("sonarr", self.SETTINGS, {("GET", "/sonarr/api/v3/system/status"): error})
            self.assertEqual(result.status, doctor.ERROR)
            self.assertIn(expected, result.detail)

    def test_bookshelf_accepts_readarr_app_name(self):
        result, _ = check("bookshelf", values(BOOKSHELF_URL="http://bookshelf:8787", BOOKSHELF_API_KEY="k"), {
            ("GET", "/api/v1/system/status"): httpx.Response(200, json={"appName": "Readarr", "version": "0.4"}),
        })
        self.assertEqual(result.status, doctor.OK, result.detail)


class QbtProbeTests(unittest.TestCase):
    SETTINGS = values(QBT_URL="http://qbittorrent:8080", QBT_PASS=SECRET)

    def test_login_then_version(self):
        result, router = check("qbittorrent", self.SETTINGS, {
            ("POST", "/api/v2/auth/login"):
                httpx.Response(200, text="Ok.", headers={"set-cookie": "QBT_SID_8080=abc; path=/"}),
            ("GET", "/api/v2/app/version"): httpx.Response(200, text="v5.0.1"),
        })
        self.assertEqual(result.status, doctor.OK, result.detail)
        self.assertEqual(result.version, "5.0.1")
        self.assertIn("QBT_SID_8080=abc", router.requests[1].headers.get("cookie", ""))

    def test_wrong_password(self):
        result, _ = check("qbittorrent", self.SETTINGS, {
            ("POST", "/api/v2/auth/login"): httpx.Response(200, text="Fails."),
        })
        self.assertIn("login rejected: check QBT_USER and QBT_PASS", result.detail)

    def test_ip_ban(self):
        result, _ = check("qbittorrent", self.SETTINGS, {
            ("POST", "/api/v2/auth/login"): httpx.Response(403, text="banned"),
        })
        self.assertIn("ban", result.detail)

    def test_empty_password_is_a_warning_not_an_error(self):
        result, _ = check("qbittorrent", values(QBT_URL="http://qbittorrent:8080"), {
            ("POST", "/api/v2/auth/login"): httpx.Response(200, text="Ok."),
            ("GET", "/api/v2/app/version"): httpx.Response(200, text="v4.6.7"),
        })
        self.assertEqual(result.status, doctor.WARNING)
        self.assertIn("QBT_PASS is empty", result.detail)


class OtherProbeTests(unittest.TestCase):
    def test_sab_bad_key_reported_with_http_200(self):
        result, _ = check("sabnzbd", values(SAB_URL="http://sabnzbd:8080", SAB_API_KEY=SECRET), {
            ("GET", "/sabnzbd/api"): lambda r: httpx.Response(200, json=(
                {"version": "4.3.3"} if r.url.params["mode"] == "version"
                else {"status": False, "error": "API Key Incorrect"})),
        })
        self.assertEqual(result.status, doctor.ERROR)
        self.assertIn("API key rejected: check SAB_API_KEY", result.detail)

    def test_jellyfin_without_key_warns_with_version(self):
        result, router = check("jellyfin", values(JELLYFIN_URL="http://jellyfin:8096/jellyfin"), {
            ("GET", "/jellyfin/System/Info/Public"): httpx.Response(200, json={"Version": "10.11.8"}),
        })
        self.assertEqual(result.status, doctor.WARNING)
        self.assertEqual(result.version, "10.11.8")
        self.assertEqual(len(router.requests), 1)

    def test_jellyfin_rejected_key_uses_mediabrowser_header(self):
        result, router = check("jellyfin", values(JELLYFIN_URL="http://jellyfin:8096", JELLYFIN_API_KEY=SECRET), {
            ("GET", "/System/Info/Public"): httpx.Response(200, json={"Version": "10.11.8"}),
            ("GET", "/System/Info"): httpx.Response(401),
        })
        self.assertEqual(result.status, doctor.ERROR)
        self.assertIn("JELLYFIN_API_KEY", result.detail)
        self.assertEqual(router.requests[1].headers["authorization"], f'MediaBrowser Token="{SECRET}"')

    def test_romm_needs_credentials_before_probing(self):
        result, router = check("romm", values(ROMM_URL="http://romm:8080"), {})
        self.assertEqual(result.status, doctor.ERROR)
        self.assertIn("ROMM_API_TOKEN", result.detail)
        self.assertEqual(router.requests, [])

    def test_romm_token(self):
        result, router = check("romm", values(ROMM_URL="http://romm:8080", ROMM_API_TOKEN=SECRET), {
            ("GET", "/api/heartbeat"): httpx.Response(200, json={"SYSTEM": {"VERSION": "3.7.0"}}),
            ("GET", "/api/platforms"): httpx.Response(200, json=[]),
        })
        self.assertEqual(result.status, doctor.OK, result.detail)
        self.assertEqual(result.version, "3.7.0")
        self.assertEqual(router.requests[1].headers["authorization"], f"Bearer {SECRET}")

    def test_gamevault_and_seerr(self):
        result, _ = check("gamevault", values(GAMEVAULT_URL="http://gv:8080", GAMEVAULT_API_KEY="k"), {
            ("GET", "/api/games"): httpx.Response(200, json={"data": []}),
        })
        self.assertEqual(result.status, doctor.OK, result.detail)
        result, _ = check("seerr", values(SEERR_URL="http://seerr:5055", SEERR_API_KEY="k"), {
            ("GET", "/api/v1/status"): httpx.Response(200, json={"version": "2.7.0"}),
            ("GET", "/api/v1/auth/me"): httpx.Response(403),
        })
        self.assertEqual(result.status, doctor.ERROR)
        self.assertIn("SEERR_API_KEY", result.detail)


class SelectionTests(unittest.TestCase):
    def test_not_configured_disabled_and_explicitly_listed(self):
        settings = values(RADARR_URL="http://radarr:7878", RADARR_API_KEY="k", ENABLED_SERVICES="sonarr")
        self.assertEqual(check("radarr", settings, {}, selected={"sonarr"})[0].status, doctor.DISABLED)
        result = check("sonarr", settings, {}, selected={"sonarr"})[0]
        self.assertEqual(result.status, doctor.ERROR)
        self.assertIn("listed in ENABLED_SERVICES but SONARR_URL is not set", result.detail)
        lidarr = check("lidarr", values(), {}, selected=set())[0]
        self.assertEqual(lidarr.status, doctor.NOT_CONFIGURED)

    def test_all_does_not_flag_unconfigured_services(self):
        settings = values(ENABLED_SERVICES="all")
        self.assertEqual(check("lidarr", settings, {}, selected=set(config.SERVICES))[0].status,
                         doctor.NOT_CONFIGURED)


class RenderTests(unittest.TestCase):
    def results(self):
        return [
            doctor.CheckResult("sonarr", "Sonarr", doctor.OK, "Sonarr 4 at http://sonarr", "4"),
            doctor.CheckResult("radarr", "Radarr", doctor.ERROR, "credentials rejected"),
        ]

    def test_text_and_exit_code(self):
        text = doctor.render_text(self.results(), ["bad thing"], "/etc/arrstack.toml")
        self.assertIn("config file /etc/arrstack.toml", text)
        self.assertIn("❌ bad thing", text)
        self.assertIn("1 ok, 0 warning(s), 1 error(s)", text)
        self.assertEqual(doctor.exit_code(self.results(), []), 1)
        self.assertEqual(doctor.exit_code(self.results()[:1], []), 0)
        self.assertEqual(doctor.exit_code(self.results()[:1], ["problem"]), 1)

    def test_json(self):
        data = json.loads(doctor.render_json(self.results(), [], None))
        self.assertEqual(data["services"][0]["version"], "4")
        self.assertIsNone(data["config_file"])


class CommandLineTests(unittest.TestCase):
    def test_check_json_offline(self):
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_URL", "_API_KEY", "_PASS"))}
        env.update(SONARR_URL="http://127.0.0.1:1", SONARR_API_KEY=SECRET, ENABLED_SERVICES="sonarr")
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "server.py"), "--check", "--json", "--timeout", "2"],
            capture_output=True, text=True, env=env, cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        sonarr = next(s for s in data["services"] if s["service"] == "sonarr")
        self.assertEqual(sonarr["status"], "error")
        self.assertIn("cannot connect", sonarr["detail"])
        self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_check_reports_config_problems_instead_of_crashing(self):
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_URL", "_API_KEY", "_PASS"))}
        env.update(SONARR_URL="sonarr:8989", ENABLED_SERVICES="sonarr,nope")
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "server.py"), "--check"],
            capture_output=True, text=True, env=env, cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("SONARR_URL: 'sonarr:8989' is not an http(s) URL", result.stdout)
        self.assertIn("Unknown service 'nope'", result.stdout)


if __name__ == "__main__":
    unittest.main()
