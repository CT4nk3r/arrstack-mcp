"""Cutoff guard, *_list_cutoff_unmet, and jellyfin_recent."""

import copy
import unittest
from unittest.mock import patch

from arrstack_mcp import server

PROFILE = {
    "id": 1, "name": "Any", "cutoff": 20,
    "items": [
        {"quality": {"id": 20, "name": "Bluray-480p"}, "items": [], "allowed": True},
        {"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": True},
    ],
}


class CutoffGuardTests(unittest.TestCase):
    def test_disallowing_the_cutoff_is_refused_before_any_write(self):
        calls = []

        def fake(path, method="GET", json=None, params=None, timeout=30):
            calls.append(method)
            return copy.deepcopy(PROFILE)

        for app in ("radarr", "sonarr"):
            calls.clear()
            with patch(f"arrstack_mcp.server._{app}", side_effect=fake):
                out = getattr(server, f"{app}_set_quality_profile_qualities")(1, disallow="Bluray-480p")
            self.assertIn("is this profile's cutoff", out)
            self.assertNotIn("PUT", calls)

    def test_other_qualities_still_change(self):
        with patch("arrstack_mcp.server._radarr", return_value=copy.deepcopy(PROFILE)) as radarr:
            out = server.radarr_set_quality_profile_qualities(1, disallow="Bluray-1080p")
        self.assertIn("-Bluray-1080p", out)
        self.assertEqual(radarr.call_args.kwargs["method"], "PUT")


class CutoffUnmetTests(unittest.TestCase):
    def test_radarr_fetches_missing_file_details(self):
        wanted = {"totalRecords": 1, "records": [{"id": 40, "title": "Inside", "year": 2021, "movieFileId": 9}]}
        files = [{"movieId": 40, "quality": {"quality": {"name": "WEBDL-1080p"}}, "size": 2e9, "customFormatScore": 100}]

        def fake(path, method="GET", json=None, params=None, timeout=30):
            return wanted if path == "/wanted/cutoff" else files

        with patch("arrstack_mcp.server._radarr", side_effect=fake) as radarr:
            out = server.radarr_list_cutoff_unmet()
        self.assertEqual(radarr.call_args.kwargs["params"], {"movieId": [40]})
        self.assertIn("[id:40] Inside (2021) — has WEBDL-1080p, 2.0 GB, score 100", out)

    def test_radarr_lists_movies(self):
        data = {"totalRecords": 30, "records": [{"id": 5, "title": "Klaus", "year": 2019, "movieFile": {
            "quality": {"quality": {"name": "WEBRip-1080p"}}, "size": 3.4e9, "customFormatScore": -9900}}]}
        with patch("arrstack_mcp.server._radarr", return_value=data) as radarr:
            out = server.radarr_list_cutoff_unmet(1)
        self.assertEqual(radarr.call_args.args[0], "/wanted/cutoff")
        self.assertIn("30 movie(s) below cutoff, showing 1", out)
        self.assertIn("[id:5] Klaus (2019) — has WEBRip-1080p, 3.4 GB, score -9900", out)

    def test_sonarr_lists_episodes(self):
        data = {"totalRecords": 1, "records": [{"seriesId": 7, "seasonNumber": 2, "episodeNumber": 3,
                                                "series": {"title": "Show"}, "episodeFile": {
                                                    "quality": {"quality": {"name": "SDTV"}}, "customFormatScore": 10}}]}
        with patch("arrstack_mcp.server._sonarr", return_value=data):
            out = server.sonarr_list_cutoff_unmet()
        self.assertIn("[series:7] Show S02E03 — has SDTV, score 10", out)

    def test_limits_and_empty(self):
        self.assertIn("Invalid limit", server.radarr_list_cutoff_unmet(0))
        with patch("arrstack_mcp.server._sonarr", return_value={"totalRecords": 0, "records": []}):
            self.assertEqual(server.sonarr_list_cutoff_unmet(), "No episodes below cutoff.")


if __name__ == "__main__":
    unittest.main()


class JellyfinRecentTests(unittest.TestCase):
    def test_uses_sorted_items_not_user_latest(self):
        data = {"Items": [
            {"Name": "Super Troopers 3", "Type": "Movie", "ProductionYear": 2026},
            {"Name": "Chickens", "Type": "Episode", "SeriesName": "Beavis and Butt-Head", "ProductionYear": 2026},
        ]}
        with patch("arrstack_mcp.server._jellyfin", return_value=data) as jf:
            out = server.jellyfin_recent(5)
        path, params = jf.call_args.args[0], jf.call_args.kwargs["params"]
        self.assertEqual(path, "/Items")
        self.assertEqual((params["SortBy"], params["SortOrder"], params["Limit"]), ("DateCreated", "Descending", 5))
        self.assertIn("• Super Troopers 3 (2026) [Movie]", out)
        self.assertIn("• Beavis and Butt-Head — Chickens (2026) [Episode]", out)
