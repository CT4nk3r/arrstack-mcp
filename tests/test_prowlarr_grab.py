"""prowlarr_grab must route usenet releases through Prowlarr, not qBittorrent."""

import unittest
from unittest.mock import patch

from arrstack_mcp import server


NZB_RELEASE = {
    "title": "Some.Movie.2020.2160p.WEB-DL",
    "protocol": "usenet",
    "guid": "https://indexer.example/details/abc123",
    "indexerId": 18,
    "downloadUrl": "http://prowlarr:9696/18/download?link=abc123&file=Some.Movie",
}

TORRENT_RELEASE = {
    "title": "Some.Movie.2020.1080p",
    "protocol": "torrent",
    "guid": "https://tracker.example/t/1",
    "indexerId": 7,
    "downloadUrl": "magnet:?xt=urn:btih:abcdef",
}


class ProwlarrGrabProtocolTests(unittest.TestCase):
    def setUp(self):
        self._saved_cache = server._prowlarr_search_cache
        self.addCleanup(setattr, server, "_prowlarr_search_cache", self._saved_cache)

    @patch("arrstack_mcp.server._qbt_fetch_torrent")
    @patch("arrstack_mcp.server._qbt_add_url")
    @patch("arrstack_mcp.server._prowlarr")
    def test_usenet_release_goes_through_prowlarr_not_qbittorrent(self, prowlarr, add_url, fetch):
        server._prowlarr_search_cache = [NZB_RELEASE]
        prowlarr.return_value = {"guid": NZB_RELEASE["guid"]}

        result = server.prowlarr_grab(0)

        self.assertTrue(result.startswith("✅"), result)
        prowlarr.assert_called_once_with(
            "/search", method="POST", json={"guid": NZB_RELEASE["guid"], "indexerId": 18}
        )
        add_url.assert_not_called()
        fetch.assert_not_called()

    @patch("arrstack_mcp.server._qbt_add_url")
    @patch("arrstack_mcp.server._prowlarr")
    def test_usenet_failure_is_reported(self, prowlarr, add_url):
        server._prowlarr_search_cache = [NZB_RELEASE]
        prowlarr.return_value = "prowlarr request failed: HTTP 404 — release not in cache"

        result = server.prowlarr_grab(0)

        self.assertTrue(result.startswith("❌"), result)
        self.assertIn("HTTP 404", result)
        add_url.assert_not_called()

    @patch("arrstack_mcp.server._prowlarr")
    def test_usenet_release_without_ids_is_rejected(self, prowlarr):
        server._prowlarr_search_cache = [{**NZB_RELEASE, "guid": ""}]

        result = server.prowlarr_grab(0)

        self.assertTrue(result.startswith("❌"), result)
        prowlarr.assert_not_called()

    @patch("arrstack_mcp.server._prowlarr")
    @patch("arrstack_mcp.server._qbt_add_url", return_value=(True, "Ok."))
    def test_torrent_release_still_uses_qbittorrent(self, add_url, prowlarr):
        server._prowlarr_search_cache = [TORRENT_RELEASE]

        result = server.prowlarr_grab(0)

        self.assertTrue(result.startswith("✅"), result)
        add_url.assert_called_once_with(TORRENT_RELEASE["downloadUrl"])
        prowlarr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
