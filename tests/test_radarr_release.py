"""Interactive search: radarr_releases lists releases, radarr_grab_release grabs one."""

import unittest
from unittest.mock import patch

from arrstack_mcp import server

RELEASES = [
    {
        "title": "Some.Movie.2015.UHD.BluRay.2160p.DDP.Atmos.5.1.DV.HDR.x265-hallowed",
        "guid": "guid-1",
        "indexerId": 4,
        "size": 20_000_000_000,
        "quality": {"quality": {"name": "Bluray-2160p"}},
        "protocol": "usenet",
        "indexer": "NZBgeek",
        "rejected": True,
        "rejections": ["Quality Bluray-2160p is not wanted in profile"],
    },
    {
        "title": "Some.Movie.2015.UHD.BluRay.2160p.TrueHD.Atmos.7.1.REMUX",
        "guid": "guid-2",
        "indexerId": 4,
        "size": 47_000_000_000,
        "quality": {"quality": {"name": "Remux-2160p"}},
        "protocol": "usenet",
        "indexer": "NZBgeek",
        "rejected": False,
        "rejections": [],
    },
    {
        "title": "Some.Movie.2015.1080p.WEB-DL.DDP5.1",
        "guid": "guid-3",
        "indexerId": 9,
        "size": 5_000_000_000,
        "quality": {"quality": {"name": "WEBDL-1080p"}},
        "protocol": "torrent",
        "indexer": "Tracker",
    },
]


class RadarrReleaseTests(unittest.TestCase):
    def setUp(self):
        self._saved = (server._radarr_release_cache, server._radarr_release_movie_id)
        self.addCleanup(self._restore)

    def _restore(self):
        server._radarr_release_cache, server._radarr_release_movie_id = self._saved

    @patch("arrstack_mcp.server._radarr")
    def test_lists_releases_with_quality_and_rejections(self, radarr):
        radarr.return_value = RELEASES
        out = server.radarr_releases(453)
        radarr.assert_called_once_with("/release", params={"movieId": 453}, timeout=180)
        self.assertIn("[0] Some.Movie.2015.UHD.BluRay.2160p.DDP.Atmos.5.1.DV.HDR.x265-hallowed", out)
        self.assertIn("20.0 GB, Bluray-2160p, usenet, NZBgeek", out)
        self.assertIn("rejected: Quality Bluray-2160p is not wanted in profile", out)
        self.assertNotIn("rejected", out.splitlines()[1])
        self.assertEqual(len(server._radarr_release_cache), 3)

    @patch("arrstack_mcp.server._radarr")
    def test_query_filters_and_reindexes(self, radarr):
        radarr.return_value = RELEASES
        out = server.radarr_releases(453, query="atmos HALLOWED")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith("[0] "))
        self.assertEqual(server._radarr_release_cache[0]["guid"], "guid-1")

    @patch("arrstack_mcp.server._radarr")
    def test_limit_applies_after_filter(self, radarr):
        radarr.return_value = RELEASES
        out = server.radarr_releases(453, limit=2)
        self.assertEqual(len(out.splitlines()), 2)

    @patch("arrstack_mcp.server._radarr")
    def test_error_from_radarr_is_returned(self, radarr):
        radarr.return_value = "radarr request failed: HTTP 500"
        self.assertEqual(server.radarr_releases(453), "radarr request failed: HTTP 500")

    @patch("arrstack_mcp.server._radarr")
    def test_invalid_arguments_do_not_call_radarr(self, radarr):
        self.assertEqual(server.radarr_releases(0), "Invalid movie_id.")
        self.assertEqual(server.radarr_releases(1, limit=0), "Invalid limit: use 1-100.")
        self.assertEqual(server.radarr_releases(1, limit=101), "Invalid limit: use 1-100.")
        radarr.assert_not_called()

    @patch("arrstack_mcp.server._radarr")
    def test_no_match_message(self, radarr):
        radarr.return_value = RELEASES
        self.assertEqual(server.radarr_releases(453, query="nonexistent"), "No releases found.")

    @patch("arrstack_mcp.server._radarr")
    def test_grab_posts_guid_indexer_and_movie_id(self, radarr):
        server._radarr_release_cache = RELEASES
        server._radarr_release_movie_id = 453
        radarr.return_value = {"guid": "guid-1"}
        out = server.radarr_grab_release(0)
        radarr.assert_called_once_with(
            "/release", method="POST", json={"guid": "guid-1", "indexerId": 4, "movieId": 453}
        )
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn("rejected by the quality profile", out)

    @patch("arrstack_mcp.server._radarr")
    def test_grab_of_accepted_release_has_no_rejection_note(self, radarr):
        server._radarr_release_cache = RELEASES
        server._radarr_release_movie_id = 453
        radarr.return_value = {}
        out = server.radarr_grab_release(1)
        self.assertTrue(out.startswith("✅"), out)
        self.assertNotIn("rejected", out)

    @patch("arrstack_mcp.server._radarr")
    def test_grab_failure_is_reported(self, radarr):
        server._radarr_release_cache = RELEASES
        server._radarr_release_movie_id = 453
        radarr.return_value = "radarr request failed: HTTP 404 — release not in cache"
        out = server.radarr_grab_release(0)
        self.assertTrue(out.startswith("❌"), out)
        self.assertIn("HTTP 404", out)

    @patch("arrstack_mcp.server._radarr")
    def test_grab_validates_cache_and_index(self, radarr):
        server._radarr_release_cache = []
        self.assertIn("Run radarr_releases first", server.radarr_grab_release(0))
        server._radarr_release_cache = RELEASES
        self.assertIn("Invalid index", server.radarr_grab_release(3))
        self.assertIn("Invalid index", server.radarr_grab_release(-1))
        radarr.assert_not_called()

    @patch("arrstack_mcp.server._radarr")
    def test_grab_requires_guid_and_indexer(self, radarr):
        server._radarr_release_cache = [{"title": "x", "guid": "", "indexerId": 1}]
        self.assertIn("Missing guid or indexerId", server.radarr_grab_release(0))
        server._radarr_release_cache = [{"title": "x", "guid": "g"}]
        self.assertIn("Missing guid or indexerId", server.radarr_grab_release(0))
        radarr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
