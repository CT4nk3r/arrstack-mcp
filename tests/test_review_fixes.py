"""Regression tests for the review fixes: error reporting, JSON handling,
Prowlarr indexer tests, Radarr missing search, qbt_add hardening, Docker."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from arrstack_mcp import server

REPO_ROOT = Path(__file__).resolve().parent.parent


def http_response(status=200, text="", json_data=None, method="GET", url="http://svc/"):
    request = httpx.Request(method, url)
    if json_data is not None:
        return httpx.Response(status, json=json_data, request=request)
    return httpx.Response(status, text=text, request=request)


class ConfiguredServices(unittest.TestCase):
    """Point every service at a fake URL for the duration of a test."""

    URLS = {
        "SONARR_URL": "http://sonarr:8989",
        "RADARR_URL": "http://radarr:7878",
        "LIDARR_URL": "http://lidarr:8686",
        "PROWLARR_URL": "http://prowlarr:9696",
        "BOOKSHELF_URL": "http://bookshelf:8787",
        "QBT_URL": "http://qbt:8080",
        "RDT_URL": "http://rdt:6500",
    }

    def setUp(self):
        self._saved = {name: getattr(server, name) for name in self.URLS}
        self._saved_sids = (server._qbt_cookies, server._rdt_sid)
        for name, url in self.URLS.items():
            setattr(server, name, url)
        server._qbt_cookies = {"SID": "sid"}
        server._rdt_sid = "sid"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(server, name, value)
        server._qbt_cookies, server._rdt_sid = self._saved_sids


class JsonHandlingTests(ConfiguredServices):
    @patch("arrstack_mcp.server.httpx.request")
    def test_bookshelf_html_body_is_an_error_not_a_crash(self, request):
        request.return_value = http_response(text="<html>login</html>")
        result = server._bookshelf("/system/status")
        self.assertIsInstance(result, str)
        self.assertIn("non-JSON", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_empty_body_decodes_to_empty_dict(self, request):
        request.return_value = http_response(text="")
        self.assertEqual(server._sonarr("/command", method="POST"), {})

    @patch("arrstack_mcp.server.httpx.request")
    def test_rdt_native_non_json_returns_text(self, request):
        request.return_value = http_response(text="plain text")
        self.assertEqual(server._rdt_native("/api/Settings"), "plain text")


class QbtActionTests(ConfiguredServices):
    @patch("arrstack_mcp.server._qbt")
    def test_delete_reports_failure(self, qbt):
        qbt.return_value = "qBittorrent auth failure: 403 after retry."
        out = server.qbt_delete("abc", delete_files=True)
        self.assertTrue(out.startswith("❌"))
        self.assertIn("403", out)

    @patch("arrstack_mcp.server._qbt")
    def test_delete_success_on_empty_body(self, qbt):
        qbt.return_value = ""
        self.assertEqual(server.qbt_delete("abc"), "🗑️ Deleted. (files kept)")

    @patch("arrstack_mcp.server._qbt")
    def test_pause_falls_back_to_v4_endpoint(self, qbt):
        qbt.side_effect = ["qBittorrent request failed: HTTP 404 — ", ""]
        self.assertEqual(server.qbt_pause("abc"), "⏸️ Paused.")
        self.assertEqual(qbt.call_args_list[1].args[0], "/torrents/pause")

    @patch("arrstack_mcp.server._qbt")
    def test_resume_does_not_retry_on_other_errors(self, qbt):
        qbt.return_value = "qBittorrent is not configured. Set QBT_URL and QBT_PASS."
        out = server.qbt_resume("abc")
        self.assertTrue(out.startswith("❌"))
        self.assertEqual(qbt.call_count, 1)

    def test_empty_hash_is_rejected(self):
        self.assertTrue(server.qbt_delete("  ").startswith("❌"))


class RdtActionTests(ConfiguredServices):
    @patch("arrstack_mcp.server._rdt")
    def test_delete_reports_failure(self, rdt):
        rdt.return_value = "rdt request failed: HTTP 500 — boom"
        self.assertTrue(server.rdt_delete("abc").startswith("❌"))

    @patch("arrstack_mcp.server._rdt")
    def test_pause_success(self, rdt):
        rdt.return_value = "Ok."
        self.assertEqual(server.rdt_pause("abc"), "⏸️ Paused.")

    @patch("arrstack_mcp.server._rdt")
    def test_add_magnet_rejects_non_magnet(self, rdt):
        self.assertTrue(server.rdt_add_magnet("https://example/x.torrent").startswith("❌"))
        rdt.assert_not_called()


class ArrToolTests(ConfiguredServices):
    def test_prowlarr_test_fails_when_not_configured(self):
        server.PROWLARR_URL = ""
        self.assertTrue(server.prowlarr_test_indexer(3).startswith("❌"))

    @patch("arrstack_mcp.server._prowlarr")
    def test_prowlarr_test_posts_indexer_to_test_endpoint(self, prowlarr):
        indexer = {"id": 3, "name": "Example"}
        prowlarr.side_effect = [indexer, {}]
        self.assertEqual(server.prowlarr_test_indexer(3), "✅ Indexer test passed.")
        args, kwargs = prowlarr.call_args
        self.assertEqual(args[0], "/indexer/test")
        self.assertEqual(kwargs["json"], indexer)

    @patch("arrstack_mcp.server._prowlarr")
    def test_prowlarr_test_reports_validation_failure(self, prowlarr):
        prowlarr.side_effect = [{"id": 3}, "prowlarr request failed: HTTP 400 — bad cookie"]
        self.assertIn("HTTP 400", server.prowlarr_test_indexer(3))

    def test_radarr_search_missing_rejects_bad_ids(self):
        self.assertIn("Invalid movie_ids", server.radarr_search_missing("12,abc"))

    @patch("arrstack_mcp.server._radarr")
    def test_radarr_search_missing_uses_missing_movies_command(self, radarr):
        radarr.return_value = {"id": 1}
        server.radarr_search_missing("")
        self.assertEqual(radarr.call_args.kwargs["json"], {"name": "MissingMoviesSearch"})

    @patch("arrstack_mcp.server.httpx.request")
    def test_queue_delete_goes_through_helper(self, request):
        request.return_value = http_response(text="", method="DELETE")
        out = server.lidarr_delete_queue_item(7, blocklist=False)
        self.assertEqual(out, "✅ Removed from queue.")
        args, kwargs = request.call_args
        self.assertEqual(args[0], "DELETE")
        self.assertEqual(args[1], "http://lidarr:8686/api/v1/queue/7")

    @patch("arrstack_mcp.server.httpx.request")
    def test_queue_delete_handles_connection_error(self, request):
        request.side_effect = httpx.ConnectError("refused")
        self.assertTrue(server.lidarr_delete_queue_item(7).startswith("❌"))

    def test_delete_tools_report_unconfigured_service(self):
        server.SONARR_URL = ""
        self.assertIn("not configured", server.sonarr_delete_episode_file(5))

    @patch("arrstack_mcp.server._sonarr")
    def test_list_episode_files_shows_file_ids(self, sonarr):
        sonarr.return_value = [
            {"id": 42, "seasonNumber": 2, "relativePath": "S02E01.mkv", "size": 1024,
             "quality": {"quality": {"name": "WEBDL-1080p"}}},
            {"id": 7, "seasonNumber": 1, "relativePath": "S01E01.mkv", "size": 1024,
             "quality": {"quality": {"name": "HDTV-720p"}}},
        ]
        out = server.sonarr_list_episode_files(10, season=2)
        self.assertIn("[fileId: 42]", out)
        self.assertNotIn("[fileId: 7]", out)


class QbtAddHardeningTests(unittest.TestCase):
    def test_non_torrent_local_file_is_not_read(self):
        with patch("builtins.open") as opener:
            kind, message = server._qbt_resolve_source(os.path.abspath(__file__))
        opener.assert_not_called()
        self.assertEqual(kind, "error")
        self.assertIn("Unrecognized", message)

    def test_existing_and_missing_files_look_the_same(self):
        existing = server._qbt_resolve_source(os.path.abspath(__file__))
        missing = server._qbt_resolve_source("/no/such/file.py")
        self.assertEqual(existing, missing)

    def test_announce_alone_is_not_a_torrent(self):
        self.assertFalse(server._looks_like_torrent(b"d<html>announce</html>"))


class DockerDefaultsTests(unittest.TestCase):
    def test_dockerfile_uses_streamable_http(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        self.assertIn('CMD ["--transport", "streamable-http"]', dockerfile)

    def test_compose_publishes_on_localhost_only(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        self.assertIn('"127.0.0.1:8000:8000"', compose)


if __name__ == "__main__":
    unittest.main()
