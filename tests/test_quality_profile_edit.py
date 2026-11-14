"""*_get_quality_profile, *_update_quality_profile, *_set_custom_format_scores."""

import copy
import unittest
from unittest.mock import patch

from arrstack_mcp import server

PROFILE = {
    "id": 6, "name": "HD - 720p/1080p", "upgradeAllowed": True, "cutoff": 6,
    "minFormatScore": 10, "cutoffFormatScore": 75, "minUpgradeFormatScore": 1,
    "items": [
        {"quality": {"id": 0, "name": "Unknown"}, "items": [], "allowed": False},
        {"quality": {"id": 6, "name": "HDTV-720p"}, "items": [], "allowed": True},
        {"name": "WEB 1080p", "id": 1003, "allowed": True, "items": [
            {"quality": {"id": 3, "name": "WEBDL-1080p"}, "items": [], "allowed": True},
            {"quality": {"id": 15, "name": "WEBRip-1080p"}, "items": [], "allowed": True},
        ]},
        {"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": True},
        {"quality": {"id": 19, "name": "Bluray-2160p"}, "items": [], "allowed": False},
    ],
    "formatItems": [
        {"format": 8, "name": "English Only - Avoid", "score": -25},
        {"format": 1, "name": "Chromecast Optimal", "score": 100},
        {"format": 9, "name": "No 3D", "score": -100},
    ],
}


class FakeArr:
    def __init__(self):
        self.profile = copy.deepcopy(PROFILE)
        self.puts = []

    def __call__(self, path, method="GET", json=None, params=None, timeout=30):
        assert path == "/qualityprofile/6", path
        if method == "PUT":
            self.puts.append(copy.deepcopy(json))
            self.profile = copy.deepcopy(json)
            return copy.deepcopy(json)
        return copy.deepcopy(self.profile)


class QualityProfileEditTests(unittest.TestCase):
    def setUp(self):
        self.arr = FakeArr()
        for name in ("_radarr", "_sonarr"):
            patcher = patch(f"arrstack_mcp.server.{name}", self.arr)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_get_shows_order_cutoff_and_scores(self):
        out = server.radarr_get_quality_profile(6)
        lines = out.splitlines()
        self.assertIn("Cutoff: HDTV-720p", out)
        self.assertIn("Minimum custom format score: 10", out)
        # Most preferred first, group members listed, cutoff marked.
        self.assertLess(out.index("✗ Bluray-2160p"), out.index("✓ Bluray-1080p"))
        self.assertIn("✓ WEB 1080p (WEBDL-1080p, WEBRip-1080p)", out)
        self.assertIn("  ✓ HDTV-720p  ← cutoff", lines)
        self.assertIn("best possible total: 100", out)
        self.assertLess(out.index("+100  Chromecast Optimal"), out.index("-100  No 3D"))

    def test_update_cutoff_by_group_name_and_upgrade_flag(self):
        out = server.sonarr_update_quality_profile(6, cutoff="web 1080p", upgrade_allowed=0)
        put = self.arr.puts[-1]
        self.assertEqual(put["cutoff"], 1003)
        self.assertFalse(put["upgradeAllowed"])
        self.assertEqual(put["minFormatScore"], 10)  # untouched
        self.assertIn("Cutoff: HDTV-720p  →  WEB 1080p", out)
        self.assertIn("Upgrades allowed: True  →  False", out)

    def test_update_cutoff_rejects_disallowed_and_unknown(self):
        self.assertIn("not allowed", server.radarr_update_quality_profile(6, cutoff="Bluray-2160p"))
        self.assertIn("No quality or group named", server.radarr_update_quality_profile(6, cutoff="Remux-9000p"))
        self.assertEqual(self.arr.puts, [])

    def test_update_scores_and_warns_when_everything_would_be_rejected(self):
        out = server.radarr_update_quality_profile(6, min_format_score=500, cutoff_format_score=0)
        self.assertEqual(self.arr.puts[-1]["minFormatScore"], 500)
        self.assertEqual(self.arr.puts[-1]["cutoffFormatScore"], 0)
        self.assertIn("every release will be rejected", out)

    def test_update_with_nothing_to_change(self):
        self.assertIn("Nothing to change", server.radarr_update_quality_profile(6))
        self.assertIn("Invalid upgrade_allowed", server.radarr_update_quality_profile(6, upgrade_allowed=2))
        self.assertIn("Invalid min_upgrade", server.radarr_update_quality_profile(6, min_upgrade_format_score=0))
        self.assertEqual(self.arr.puts, [])

    def test_set_scores_by_name_case_insensitive(self):
        out = server.radarr_set_custom_format_scores(6, "english only - avoid=-10000, Chromecast Optimal=50")
        scores = {f["name"]: f["score"] for f in self.arr.puts[-1]["formatItems"]}
        self.assertEqual(scores, {"English Only - Avoid": -10000, "Chromecast Optimal": 50, "No 3D": -100})
        self.assertIn("English Only - Avoid: -25  →  -10000", out)
        self.assertIn("Best possible total is now 50", out)

    def test_set_scores_rejects_unknown_names_and_bad_input(self):
        out = server.sonarr_set_custom_format_scores(6, "Nope=5")
        self.assertIn("Unknown custom format(s): Nope", out)
        self.assertIn("Chromecast Optimal", out)
        self.assertIn("Can't read", server.sonarr_set_custom_format_scores(6, "Chromecast Optimal=lots"))
        self.assertIn("Can't read", server.sonarr_set_custom_format_scores(6, "Chromecast Optimal"))
        self.assertEqual(self.arr.puts, [])

    def test_set_scores_noop_does_not_write(self):
        self.assertIn("nothing changed", server.radarr_set_custom_format_scores(6, "No 3D=-100"))
        self.assertEqual(self.arr.puts, [])


if __name__ == "__main__":
    unittest.main()
