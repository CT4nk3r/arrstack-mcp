"""sonarr_delete_episode_file(blocklist=...)."""

import unittest
from unittest.mock import patch

from arrstack_mcp import server

SINGLE = [
    {"id": 40198, "eventType": "downloadFolderImported", "episodeId": 24129, "downloadId": "nzo_a",
     "sourceTitle": "Show.S04E12.1080p.WEB-DL-NTb", "data": {"fileId": "10005"}},
    {"id": 40196, "eventType": "grabbed", "episodeId": 24129, "downloadId": "nzo_a",
     "sourceTitle": "Show.S04E12.1080p.WEB-DL-NTb"},
    {"id": 40195, "eventType": "grabbed", "episodeId": 24128, "downloadId": "nzo_b", "sourceTitle": "Other"},
]
PACK = [
    {"id": 50, "eventType": "downloadFolderImported", "episodeId": 2, "downloadId": "nzo_p",
     "sourceTitle": "Show.S01.1080p.BluRay-GRP", "data": {"fileId": "10005"}},
    {"id": 41, "eventType": "grabbed", "episodeId": 1, "downloadId": "nzo_p", "sourceTitle": "Show.S01.1080p.BluRay-GRP"},
    {"id": 42, "eventType": "grabbed", "episodeId": 2, "downloadId": "nzo_p", "sourceTitle": "Show.S01.1080p.BluRay-GRP"},
    {"id": 43, "eventType": "grabbed", "episodeId": 3, "downloadId": "nzo_p", "sourceTitle": "Show.S01.1080p.BluRay-GRP"},
]


def fake_sonarr(history):
    calls = []

    def call(path, method="GET", json=None, params=None):
        calls.append((method, path))
        if path == "/episodefile/10005" and method == "GET":
            return {"id": 10005, "seriesId": 107}
        if path == "/history/series":
            assert params == {"seriesId": 107}
            return history
        return {}

    return call, calls


class SonarrDeleteBlocklistTests(unittest.TestCase):
    def test_delete_without_blocklist_only_deletes(self):
        call, calls = fake_sonarr(SINGLE)
        with patch("arrstack_mcp.server._sonarr", side_effect=call):
            out = server.sonarr_delete_episode_file(10005)
        self.assertIn("✅ Deleted", out)
        self.assertEqual(calls, [("DELETE", "/episodefile/10005")])

    def test_blocklist_marks_the_files_grab_failed_after_delete(self):
        call, calls = fake_sonarr(SINGLE)
        with patch("arrstack_mcp.server._sonarr", side_effect=call):
            out = server.sonarr_delete_episode_file(10005, blocklist=True)
        self.assertIn("🚫 Blocklisted 'Show.S04E12.1080p.WEB-DL-NTb'", out)
        self.assertNotIn("pack", out)
        self.assertLess(calls.index(("DELETE", "/episodefile/10005")), calls.index(("POST", "/history/failed/40196")))
        self.assertNotIn(("POST", "/history/failed/40195"), calls)

    def test_season_pack_is_blocklisted_once_and_reported(self):
        call, calls = fake_sonarr(PACK)
        with patch("arrstack_mcp.server._sonarr", side_effect=call):
            out = server.sonarr_delete_episode_file(10005, blocklist=True)
        self.assertEqual(sum(1 for _, p in calls if p.startswith("/history/failed/")), 1)
        self.assertIn("pack covering 3 episodes", out)

    def test_missing_history_still_deletes_and_warns(self):
        call, calls = fake_sonarr([])
        with patch("arrstack_mcp.server._sonarr", side_effect=call):
            out = server.sonarr_delete_episode_file(10005, blocklist=True)
        self.assertIn("✅ Deleted", out)
        self.assertIn("⚠ Not blocklisted", out)
        self.assertFalse(any(p.startswith("/history/failed") for _, p in calls))


if __name__ == "__main__":
    unittest.main()
