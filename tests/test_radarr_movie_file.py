"""radarr_get_movie_file, radarr_delete_movie_file(blocklist=...), radarr_manual_import(preview=...)."""

import unittest
from unittest.mock import patch

from arrstack_mcp import server

MOVIE = {"id": 495, "title": "Klaus", "movieFile": {"id": 651}}
FILE = {
    "id": 651,
    "path": "/data/movies/Klaus (2019)/Klaus.2019.MULTi.1080p.WEBRip.x264-GRP.mkv",
    "size": 3_413_292_762,
    "sceneName": "Klaus.2019.MULTi.1080p.WEBRip.x264-GRP",
    "releaseGroup": "GRP",
    "languages": [{"id": 2, "name": "French"}, {"id": 1, "name": "English"}],
    "quality": {"quality": {"name": "WEBRip-1080p"}},
    "customFormats": [{"id": 1, "name": "Chromecast Optimal"}],
    "customFormatScore": 100,
    "mediaInfo": {
        "videoCodec": "x264", "resolution": "1920x1036", "runTime": "1:38:03",
        "audioCodec": "EAC3", "audioChannels": 5.1, "audioStreamCount": 2,
        "audioLanguages": "fre/eng", "subtitles": "fre/fre",
    },
}
HISTORY = [
    {"id": 1975, "eventType": "downloadFailed", "downloadId": "nzo_b", "sourceTitle": "Other.Release"},
    {"id": 1973, "eventType": "downloadFolderImported", "downloadId": "nzo_a",
     "sourceTitle": "Klaus.2019.MULTi.1080p.WEBRip.x264-GRP", "data": {"fileId": "651"}},
    {"id": 1972, "eventType": "grabbed", "downloadId": "nzo_a", "sourceTitle": "Klaus.2019.MULTi.1080p.WEBRip.x264-GRP"},
    {"id": 1971, "eventType": "grabbed", "downloadId": "nzo_b", "sourceTitle": "Other.Release"},
]


def fake_radarr(history=HISTORY):
    calls = []

    def call(path, method="GET", json=None, params=None, timeout=30):
        calls.append((method, path))
        if path == "/movie/495":
            return MOVIE
        if path == "/history/movie":
            return history
        if path == "/moviefile":
            return [FILE]
        return {}

    return call, calls


class RadarrMovieFileTests(unittest.TestCase):
    def test_get_movie_file_shows_languages_and_codecs(self):
        call, _ = fake_radarr()
        with patch("arrstack_mcp.server._radarr", side_effect=call):
            out = server.radarr_get_movie_file(495)
        self.assertIn("Klaus.2019.MULTi.1080p.WEBRip.x264-GRP.mkv", out)
        self.assertIn("3.41 GB", out)
        self.assertIn("WEBRip-1080p", out)
        self.assertIn("Languages: French, English", out)
        self.assertIn("languages fre/eng", out)
        self.assertIn("Subtitles: fre/fre", out)

    def test_get_movie_file_without_file(self):
        with patch("arrstack_mcp.server._radarr", return_value=[]):
            self.assertIn("no file", server.radarr_get_movie_file(495))

    def test_delete_without_blocklist_skips_history(self):
        call, calls = fake_radarr()
        with patch("arrstack_mcp.server._radarr", side_effect=call):
            out = server.radarr_delete_movie_file(495)
        self.assertIn("✅ Deleted", out)
        self.assertEqual(calls, [("GET", "/movie/495"), ("DELETE", "/moviefile/651")])

    def test_delete_with_blocklist_marks_the_files_grab_failed(self):
        call, calls = fake_radarr()
        with patch("arrstack_mcp.server._radarr", side_effect=call):
            out = server.radarr_delete_movie_file(495, blocklist=True)
        self.assertIn("🚫 Blocklisted 'Klaus.2019.MULTi.1080p.WEBRip.x264-GRP'", out)
        # Delete first, then mark failed, so the redownload can import into an empty slot.
        self.assertLess(calls.index(("DELETE", "/moviefile/651")), calls.index(("POST", "/history/failed/1972")))
        self.assertNotIn(("POST", "/history/failed/1971"), calls)

    def test_delete_with_blocklist_and_no_history_still_deletes(self):
        call, calls = fake_radarr(history=[])
        with patch("arrstack_mcp.server._radarr", side_effect=call):
            out = server.radarr_delete_movie_file(495, blocklist=True)
        self.assertIn("✅ Deleted", out)
        self.assertIn("⚠ Not blocklisted", out)
        self.assertFalse(any(path.startswith("/history/failed") for _, path in calls))

    def test_manual_import_preview_imports_nothing(self):
        candidates = [{
            "path": "/data/downloads/x/Klaus.mkv", "relativePath": "Klaus.mkv", "size": 9_000_000_000,
            "movie": {"id": 495, "title": "Klaus", "year": 2019},
            "quality": {"quality": {"name": "Bluray-1080p"}},
            "languages": [{"name": "Dutch"}],
            "rejections": [{"type": "permanent", "reason": "Not an upgrade"}],
        }]
        with patch("arrstack_mcp.server._radarr", return_value=candidates) as radarr:
            out = server.radarr_manual_import(folder="/data/downloads/x", preview=True)
        radarr.assert_called_once()
        self.assertIn("Preview only", out)
        self.assertIn("Klaus.mkv — 9.00 GB, Bluray-1080p, languages: Dutch → Klaus (2019)", out)
        self.assertIn("Not an upgrade", out)


if __name__ == "__main__":
    unittest.main()
