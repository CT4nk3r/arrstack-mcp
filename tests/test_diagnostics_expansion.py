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


class SonarrManualImportTests(unittest.TestCase):
    def setUp(self):
        self.sonarr_url = server.SONARR_URL

    def tearDown(self):
        server.SONARR_URL = self.sonarr_url

    def test_requires_download_id_or_folder(self):
        self.assertEqual(server.sonarr_manual_import(), "Provide download_id or folder.")

    def test_invalid_import_mode_rejected(self):
        result = server.sonarr_manual_import(download_id="x", import_mode="bogus")
        self.assertIn("Invalid import_mode", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_no_candidates(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        request.return_value = response([])
        result = server.sonarr_manual_import(download_id="abc")
        self.assertEqual(result, "No importable files found.")

    @patch("arrstack_mcp.server.httpx.request")
    def test_confident_match_is_imported(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        candidate = {
            "path": "/data/downloads/Show.S01E01.mkv",
            "relativePath": "Show.S01E01.mkv",
            "series": {"id": 5, "title": "Show"},
            "seasonNumber": 1,
            "episodes": [{"id": 100, "episodeNumber": 1}],
            "quality": {"quality": {"id": 9, "name": "HDTV-1080p"}},
            "languages": [{"id": 1, "name": "English"}],
            "releaseGroup": "GROUP",
            "downloadId": "abc",
            "rejections": [],
        }
        request.side_effect = [
            response([candidate]),  # GET /manualimport
            response({}),  # POST /command
        ]

        result = server.sonarr_manual_import(download_id="abc")

        post_call = request.call_args_list[-1]
        self.assertEqual(post_call.kwargs["json"]["name"], "ManualImport")
        self.assertEqual(post_call.kwargs["json"]["files"][0]["seriesId"], 5)
        self.assertEqual(post_call.kwargs["json"]["files"][0]["episodeIds"], [100])
        self.assertIn("✅ Imported", result)
        self.assertIn("Show", result)
        self.assertIn("S1E1", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_ambiguous_match_is_reported_not_imported(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        candidate = {
            "path": "/data/downloads/mystery.mkv",
            "relativePath": "mystery.mkv",
            "series": None,
            "episodes": [],
            "quality": None,
            "rejections": [{"reason": "Unable to parse episode", "type": "error"}],
        }
        request.return_value = response([candidate])

        result = server.sonarr_manual_import(folder="/data/downloads")

        self.assertNotIn("✅", result)
        self.assertIn("need manual review", result)
        self.assertIn("Unable to parse episode", result)
        # Only the GET call happened, no POST /command
        self.assertEqual(request.call_count, 1)


class RadarrManualImportTests(unittest.TestCase):
    def setUp(self):
        self.radarr_url = server.RADARR_URL

    def tearDown(self):
        server.RADARR_URL = self.radarr_url

    def test_requires_download_id_or_folder(self):
        self.assertEqual(server.radarr_manual_import(), "Provide download_id or folder.")

    @patch("arrstack_mcp.server.httpx.request")
    def test_confident_match_is_imported(self, request):
        server.RADARR_URL = "http://radarr:7878"
        candidate = {
            "path": "/data/downloads/Movie.2024.mkv",
            "relativePath": "Movie.2024.mkv",
            "movie": {"id": 9, "title": "Movie"},
            "quality": {"quality": {"id": 3, "name": "WEBDL-1080p"}},
            "languages": [],
            "releaseGroup": "GROUP",
            "downloadId": "xyz",
            "rejections": [],
        }
        request.side_effect = [response([candidate]), response({})]

        result = server.radarr_manual_import(download_id="xyz")

        post_call = request.call_args_list[-1]
        self.assertEqual(post_call.kwargs["json"]["files"][0]["movieId"], 9)
        self.assertIn("✅ Imported", result)
        self.assertIn("Movie", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_ambiguous_match_is_reported_not_imported(self, request):
        server.RADARR_URL = "http://radarr:7878"
        candidate = {
            "path": "/data/downloads/mystery.mkv",
            "relativePath": "mystery.mkv",
            "movie": None,
            "quality": None,
            "rejections": [],
        }
        request.return_value = response([candidate])

        result = server.radarr_manual_import(folder="/data/downloads")

        self.assertNotIn("✅", result)
        self.assertIn("need manual review", result)
        self.assertEqual(request.call_count, 1)


class QbtTorrentTrackersTests(unittest.TestCase):
    @patch("arrstack_mcp.server._qbt")
    def test_surfaces_not_working_tracker_with_message(self, qbt):
        qbt.return_value = [
            {
                "url": "udp://tracker.example:80/announce",
                "status": 4,
                "num_seeds": 0,
                "num_peers": 0,
                "msg": "This torrent is not authorized for use on this tracker",
            }
        ]

        result = server.qbt_torrent_trackers("abc123")

        self.assertIn("Not working", result)
        self.assertIn("not authorized for use", result)

    @patch("arrstack_mcp.server._qbt")
    def test_working_tracker_has_no_reason_line(self, qbt):
        qbt.return_value = [
            {"url": "udp://tracker.example:80/announce", "status": 2, "num_seeds": 5, "num_peers": 3, "msg": ""}
        ]

        result = server.qbt_torrent_trackers("abc123")

        self.assertIn("Working", result)
        self.assertNotIn("reason:", result)

    @patch("arrstack_mcp.server._qbt")
    def test_blank_tracker_message_still_reports_status(self, qbt):
        """qBittorrent sometimes reports a failure with an empty msg; the status must still show."""
        qbt.return_value = [
            {"url": "udp://tracker.example:80/announce", "status": 4, "num_seeds": 0, "num_peers": 0, "msg": ""}
        ]

        result = server.qbt_torrent_trackers("abc123")

        self.assertIn("Not working", result)
        self.assertIn("tracker.example", result)

    @patch("arrstack_mcp.server._qbt")
    def test_no_trackers(self, qbt):
        qbt.return_value = []
        self.assertEqual(server.qbt_torrent_trackers("abc123"), "No trackers found for torrent abc123.")


class RootFoldersTests(unittest.TestCase):
    def setUp(self):
        self.sonarr_url = server.SONARR_URL
        self.radarr_url = server.RADARR_URL

    def tearDown(self):
        server.SONARR_URL = self.sonarr_url
        server.RADARR_URL = self.radarr_url

    @patch("arrstack_mcp.server.httpx.request")
    def test_sonarr_flags_inaccessible_folder(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        request.return_value = response(
            [{"id": 1, "path": "/data/tv", "accessible": False, "freeSpace": 0, "totalSpace": 0}]
        )

        result = server.sonarr_list_root_folders()

        self.assertIn("NOT ACCESSIBLE", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_sonarr_shows_free_space_percentage(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        request.return_value = response(
            [{"id": 1, "path": "/data/tv", "accessible": True, "freeSpace": 5e9, "totalSpace": 10e9}]
        )

        result = server.sonarr_list_root_folders()

        self.assertIn("50%", result)
        self.assertNotIn("NOT ACCESSIBLE", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_radarr_flags_inaccessible_folder(self, request):
        server.RADARR_URL = "http://radarr:7878"
        request.return_value = response(
            [{"id": 1, "path": "/data/movies", "accessible": False, "freeSpace": 0, "totalSpace": 0}]
        )

        result = server.radarr_list_root_folders()

        self.assertIn("NOT ACCESSIBLE", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_empty_list(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        request.return_value = response([])
        self.assertEqual(server.sonarr_list_root_folders(), "No root folders configured.")

    @patch("arrstack_mcp.server.httpx.request")
    def test_joins_rootfolder_with_diskspace_for_total(self, request):
        # Real Sonarr/Radarr /rootfolder responses never include totalSpace —
        # only /diskspace does, keyed by path. Model that accurately here
        # instead of relying on a single reused mock payload.
        server.SONARR_URL = "http://sonarr:8989"
        request.side_effect = [
            response([{"id": 2, "path": "/data/tv", "accessible": True, "freeSpace": 3860779470848}]),
            response(
                [
                    {"path": "/data/downloads", "freeSpace": 1, "totalSpace": 2},
                    {"path": "/data/tv", "freeSpace": 3860779470848, "totalSpace": 7937303740416},
                ]
            ),
        ]

        result = server.sonarr_list_root_folders()

        self.assertIn("3860.8/7937.3 GB free", result)
        self.assertIn("49%", result)


class SabHistoryStatusTests(unittest.TestCase):
    @patch("arrstack_mcp.server._sab")
    def test_history_surfaces_fail_message(self, sab):
        sab.return_value = {
            "history": {
                "slots": [
                    {"status": "Completed", "name": "Good.Release", "bytes": 1e9, "fail_message": ""},
                    {
                        "status": "Failed",
                        "name": "Bad.Release.exe",
                        "bytes": 2e9,
                        "fail_message": "Aborted, unwanted extension detected",
                    },
                ]
            }
        }

        result = server.sab_history()

        self.assertIn("[Completed] Good.Release", result)
        self.assertNotIn("reason:", result.split("\n")[0])
        self.assertIn("[Failed] Bad.Release.exe", result)
        self.assertIn("reason: Aborted, unwanted extension detected", result)

    @patch("arrstack_mcp.server._sab")
    def test_history_empty(self, sab):
        sab.return_value = {"history": {"slots": []}}
        self.assertEqual(server.sab_history(), "History is empty.")

    @patch("arrstack_mcp.server._sab")
    def test_status_surfaces_warnings(self, sab):
        sab.return_value = {
            "status": {
                "version": "5.0.1",
                "paused": False,
                "diskspace1_norm": "314.9 G",
                "diskspacetotal1": "468.38",
                "warnings": [
                    {"type": "WARNING", "text": "Unwanted Extension in file bad.exe (Some.Release)"},
                ],
            }
        }

        result = server.sab_status()

        self.assertIn("5.0.1", result)
        self.assertIn("314.9 G free of 468.38 GB total", result)
        self.assertIn("Unwanted Extension", result)

    @patch("arrstack_mcp.server._sab")
    def test_status_no_warnings(self, sab):
        sab.return_value = {
            "status": {"version": "5.0.1", "paused": False, "diskspace1_norm": "1 T", "diskspacetotal1": "2000"}
        }

        result = server.sab_status()

        self.assertNotIn("warning(s)", result)


if __name__ == "__main__":
    unittest.main()
