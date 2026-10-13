import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from arrstack_mcp import server


def response(data):
    result = Mock()
    result.status_code = 200
    result.text = ""
    result.json.return_value = data
    result.raise_for_status.return_value = None
    return result


class ServiceSelectionTests(unittest.TestCase):
    def test_auto_selects_only_configured_services(self):
        selected = server._selected_services("auto")

        expected = {
            key for key, (_, url, _) in server.SERVICE_CONFIG.items() if url
        }
        self.assertEqual(selected, expected)

    def test_aliases_are_accepted(self):
        selected = server._selected_services("qbt,sab,rdt,romm")

        self.assertEqual(selected, {"qbittorrent", "sabnzbd", "rdtclient", "romm"})

    def test_unknown_service_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown service"):
            server._selected_services("sonarr,not-a-service")

    def test_catalog_filter_removes_disabled_tools(self):
        env = os.environ.copy()
        env["ENABLED_SERVICES"] = "romm,gamevault"
        code = (
            "from arrstack_mcp import server;"
            "server._configure_service_tools();"
            "print(','.join(t.name for t in server.mcp._tool_manager.list_tools()))"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        names = set(result.stdout.strip().split(","))

        self.assertIn("romm_system_info", names)
        self.assertIn("gamevault_list_games", names)
        self.assertFalse(any(name.startswith("sonarr_") for name in names))
        self.assertFalse(any(name.startswith("lidarr_") for name in names))
        self.assertFalse(any(name.startswith("sab_") for name in names))

    def test_all_selects_every_service(self):
        self.assertEqual(server._selected_services("all"), set(server.SERVICE_CONFIG))


class OptionalServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "LIDARR_URL": server.LIDARR_URL,
            "SAB_URL": server.SAB_URL,
            "SAB_API_KEY": server.SAB_API_KEY,
            "BOOKSHELF_URL": server.BOOKSHELF_URL,
            "SEERR_URL": server.SEERR_URL,
            "SEERR_API_KEY": server.SEERR_API_KEY,
        }

    def tearDown(self):
        for name, value in self.settings.items():
            setattr(server, name, value)

    @patch("arrstack_mcp.server.httpx.request")
    def test_lidarr_search_uses_structured_params(self, request):
        server.LIDARR_URL = "http://lidarr:8686"
        request.return_value = response([])

        server.lidarr_search("artist&apikey=leak")

        _, kwargs = request.call_args
        self.assertEqual(kwargs["params"], {"term": "artist&apikey=leak"})

    @patch("arrstack_mcp.server.httpx.get")
    def test_sab_api_uses_structured_params(self, get):
        server.SAB_URL = "http://sab:8080"
        server.SAB_API_KEY = "key"
        get.return_value = response({"queue": {}})

        server.sab_queue()

        _, kwargs = get.call_args
        self.assertEqual(kwargs["params"]["mode"], "queue")
        self.assertEqual(kwargs["params"]["apikey"], "key")

    @patch("arrstack_mcp.server.httpx.request")
    def test_bookshelf_search_uses_structured_params(self, request):
        server.BOOKSHELF_URL = "http://bookshelf:8787"
        request.return_value = response([])

        server.bookshelf_search_book("book&apikey=leak")

        _, kwargs = request.call_args
        self.assertEqual(kwargs["params"], {"term": "book&apikey=leak"})

    @patch("arrstack_mcp.server.httpx.request")
    def test_seerr_search_uses_structured_params(self, request):
        server.SEERR_URL = "http://seerr:5055"
        request.return_value = response({"results": []})

        server.seerr_search("inception&apikey=leak")

        _, kwargs = request.call_args
        self.assertEqual(kwargs["params"], {"query": "inception&apikey=leak"})

    @patch("arrstack_mcp.server.httpx.request")
    def test_seerr_request_movie_posts_correct_body(self, request):
        server.SEERR_URL = "http://seerr:5055"
        request.return_value = response({"id": 1, "status": 2})

        server.seerr_request_movie(27205)

        _, kwargs = request.call_args
        self.assertEqual(kwargs["json"], {"mediaType": "movie", "mediaId": 27205})

    @patch("arrstack_mcp.server.httpx.request")
    def test_seerr_request_tv_accepts_all(self, request):
        server.SEERR_URL = "http://seerr:5055"
        request.return_value = response({"id": 1, "status": 2})

        server.seerr_request_tv(1399, seasons="all")

        _, kwargs = request.call_args
        self.assertEqual(
            kwargs["json"], {"mediaType": "tv", "mediaId": 1399, "seasons": "all"}
        )

    @patch("arrstack_mcp.server.httpx.request")
    def test_seerr_request_tv_parses_season_list(self, request):
        server.SEERR_URL = "http://seerr:5055"
        request.return_value = response({"id": 1, "status": 2})

        server.seerr_request_tv(1399, seasons="1, 2,3")

        _, kwargs = request.call_args
        self.assertEqual(
            kwargs["json"], {"mediaType": "tv", "mediaId": 1399, "seasons": [1, 2, 3]}
        )

    def test_seerr_request_tv_rejects_invalid_seasons(self):
        server.SEERR_URL = "http://seerr:5055"

        result = server.seerr_request_tv(1399, seasons="one,two")

        self.assertIn("Invalid seasons", result)


if __name__ == "__main__":
    unittest.main()
