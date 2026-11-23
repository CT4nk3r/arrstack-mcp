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


class FormatQueueItemTests(unittest.TestCase):
    def test_healthy_item_shows_no_warning(self):
        item = {"id": 1, "title": "Some Show S01E01", "status": "downloading", "sizeleft": 1e9}
        line = server._format_queue_item(item)
        self.assertEqual(line, "• [queueId: 1] Some Show S01E01 — downloading (1.0 GB remaining)")

    def test_ok_tracked_status_shows_no_warning(self):
        item = {
            "id": 1,
            "title": "Some Show S01E01",
            "status": "completed",
            "sizeleft": 0,
            "trackedDownloadStatus": "ok",
        }
        line = server._format_queue_item(item)
        self.assertNotIn("⚠", line)

    def test_surfaces_executable_file_warning(self):
        item = {
            "id": 344205622,
            "title": "Ted Lasso S04E04 1080p HEVC x265-MeGusta",
            "status": "completed",
            "sizeleft": 0,
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [
                {
                    "title": "Ted Lasso S04E04 1080p HEVC x265-MeGusta",
                    "messages": ["Caution: Found executable file with extension: '.exe'"],
                }
            ],
            "indexer": "LimeTorrents (Prowlarr)",
            "downloadClient": "qBittorrent",
        }
        line = server._format_queue_item(item)
        self.assertIn("queueId: 344205622", line)
        self.assertIn("completed (0.0 GB remaining)", line)
        self.assertIn("⚠ warning/importPending", line)
        self.assertIn("Found executable file with extension: '.exe'", line)
        self.assertIn("indexer: LimeTorrents (Prowlarr)", line)
        self.assertIn("client: qBittorrent", line)

    def test_falls_back_to_error_message_when_no_status_messages(self):
        item = {
            "id": 2,
            "title": "Movie 2024",
            "status": "completed",
            "sizeleft": 0,
            "trackedDownloadStatus": "error",
            "trackedDownloadState": "importBlocked",
            "errorMessage": "Not a valid video file",
        }
        line = server._format_queue_item(item)
        self.assertIn("⚠ error/importBlocked: Not a valid video file", line)

    def test_no_message_available_still_flags_the_warning(self):
        item = {
            "id": 3,
            "title": "Movie 2024",
            "status": "completed",
            "sizeleft": 0,
            "trackedDownloadStatus": "warning",
        }
        line = server._format_queue_item(item)
        self.assertIn("⚠ warning/?: no message provided", line)


class SonarrQueueTests(unittest.TestCase):
    def setUp(self):
        self.sonarr_url = server.SONARR_URL

    def tearDown(self):
        server.SONARR_URL = self.sonarr_url

    @patch("arrstack_mcp.server.httpx.request")
    def test_queue_surfaces_import_warning(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        request.return_value = response(
            {
                "records": [
                    {
                        "id": 344205622,
                        "title": "Ted Lasso S04E04 1080p HEVC x265-MeGusta",
                        "status": "completed",
                        "sizeleft": 0,
                        "trackedDownloadStatus": "warning",
                        "trackedDownloadState": "importPending",
                        "statusMessages": [
                            {"messages": ["Caution: Found executable file with extension: '.exe'"]}
                        ],
                        "indexer": "LimeTorrents (Prowlarr)",
                        "downloadClient": "qBittorrent",
                    }
                ]
            }
        )

        result = server.sonarr_queue()

        self.assertIn("Found executable file", result)
        self.assertIn("LimeTorrents", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_empty_queue(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        request.return_value = response({"records": []})

        self.assertEqual(server.sonarr_queue(), "Queue is empty.")


class RadarrQueueTests(unittest.TestCase):
    def setUp(self):
        self.radarr_url = server.RADARR_URL

    def tearDown(self):
        server.RADARR_URL = self.radarr_url

    @patch("arrstack_mcp.server.httpx.request")
    def test_queue_surfaces_import_warning(self, request):
        server.RADARR_URL = "http://radarr:7878"
        request.return_value = response(
            {
                "records": [
                    {
                        "id": 1,
                        "title": "Some Movie 2024",
                        "status": "completed",
                        "sizeleft": 0,
                        "trackedDownloadStatus": "warning",
                        "trackedDownloadState": "importPending",
                        "statusMessages": [
                            {"messages": ["Caution: Found executable file with extension: '.exe'"]}
                        ],
                        "indexer": "SomeIndexer",
                        "downloadClient": "qBittorrent",
                    }
                ]
            }
        )

        result = server.radarr_queue()

        self.assertIn("Found executable file", result)
        self.assertIn("SomeIndexer", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_empty_queue(self, request):
        server.RADARR_URL = "http://radarr:7878"
        request.return_value = response({"records": []})

        self.assertEqual(server.radarr_queue(), "Queue is empty.")


if __name__ == "__main__":
    unittest.main()
