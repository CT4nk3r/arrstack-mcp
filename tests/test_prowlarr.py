"""Unit tests for Prowlarr application management and indexer statistics tools."""

import unittest
from unittest.mock import patch

from arrstack_mcp import server


class ProwlarrApplicationsTests(unittest.TestCase):
    @patch("arrstack_mcp.server._prowlarr")
    def test_list_applications_success(self, prowlarr):
        prowlarr.return_value = [
            {
                "id": 1,
                "name": "Radarr",
                "implementationName": "Radarr",
                "syncLevel": "fullSync",
                "fields": [{"name": "baseUrl", "value": "http://radarr:7878"}],
            },
            {
                "id": 2,
                "name": "Sonarr",
                "implementationName": "Sonarr",
                "syncLevel": "addOnly",
                "fields": [{"name": "prowlarrUrl", "value": "http://prowlarr:9696"}],
            },
        ]

        out = server.prowlarr_list_applications()

        prowlarr.assert_called_once_with("/applications")
        self.assertIn("• [1] Radarr (Radarr) [sync: fullSync] — http://radarr:7878", out)
        self.assertIn("• [2] Sonarr (Sonarr) [sync: addOnly] — http://prowlarr:9696", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_list_applications_empty(self, prowlarr):
        prowlarr.return_value = []
        out = server.prowlarr_list_applications()
        self.assertEqual(out, "No applications configured in Prowlarr.")

    @patch("arrstack_mcp.server._prowlarr")
    def test_list_applications_error(self, prowlarr):
        prowlarr.return_value = "Prowlarr is not configured. Set PROWLARR_URL and PROWLARR_API_KEY."
        out = server.prowlarr_list_applications()
        self.assertIn("not configured", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_sync_applications_all(self, prowlarr):
        prowlarr.return_value = {"id": 10, "name": "ApplicationSync", "status": "queued"}

        out = server.prowlarr_sync_applications(0)

        prowlarr.assert_called_once_with(
            "/command", method="POST", json={"name": "ApplicationSync"}
        )
        self.assertIn("🔄 Application sync triggered for all applications.", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_sync_applications_single(self, prowlarr):
        prowlarr.return_value = {"id": 11, "name": "ApplicationSync", "status": "queued"}

        out = server.prowlarr_sync_applications(3)

        prowlarr.assert_called_once_with(
            "/command", method="POST", json={"name": "ApplicationSync", "applicationId": 3}
        )
        self.assertIn("🔄 Application sync triggered for application 3.", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_sync_applications_error(self, prowlarr):
        prowlarr.return_value = "prowlarr request failed: HTTP 500"
        out = server.prowlarr_sync_applications(0)
        self.assertIn("HTTP 500", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_test_applications_single_success(self, prowlarr):
        prowlarr.return_value = {}

        out = server.prowlarr_test_applications(1)

        prowlarr.assert_called_once_with("/applications/1/test", method="POST")
        self.assertIn("✅ Application 1 connection test passed.", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_test_applications_single_failure(self, prowlarr):
        prowlarr.return_value = "prowlarr request failed: HTTP 400 — Invalid API key"

        out = server.prowlarr_test_applications(1)

        self.assertIn("❌ Application 1 test failed:", out)
        self.assertIn("Invalid API key", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_test_applications_all_mixed(self, prowlarr):
        prowlarr.side_effect = [
            [{"id": 1, "name": "Radarr"}, {"id": 2, "name": "Sonarr"}],
            {},  # Radarr test -> OK
            "prowlarr request failed: HTTP 502",  # Sonarr test -> Fail
        ]

        out = server.prowlarr_test_applications(0)

        self.assertIn("✅ [1] Radarr — OK", out)
        self.assertIn("❌ [2] Sonarr — prowlarr request failed: HTTP 502", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_test_applications_all_empty(self, prowlarr):
        prowlarr.return_value = []
        out = server.prowlarr_test_applications(0)
        self.assertEqual(out, "No applications configured.")


class ProwlarrIndexerStatsTests(unittest.TestCase):
    @patch("arrstack_mcp.server._prowlarr")
    def test_indexer_stats_success(self, prowlarr):
        prowlarr.return_value = {
            "indexers": [
                {
                    "indexerId": 5,
                    "indexerName": "1337x",
                    "numberOfQueries": 120,
                    "numberOfGrabs": 15,
                    "numberOfFailedQueries": 2,
                    "averageResponseTime": 350,
                }
            ]
        }

        out = server.prowlarr_indexer_stats()

        prowlarr.assert_called_once_with("/indexerstats")
        self.assertIn("• 1337x: 120 queries, 15 grabs, 2 failed (avg 350ms)", out)

    @patch("arrstack_mcp.server._prowlarr")
    def test_indexer_stats_empty(self, prowlarr):
        prowlarr.return_value = {"indexers": []}
        out = server.prowlarr_indexer_stats()
        self.assertEqual(out, "No indexer statistics available.")

    @patch("arrstack_mcp.server._prowlarr")
    def test_indexer_stats_error(self, prowlarr):
        prowlarr.return_value = "Prowlarr is not configured."
        out = server.prowlarr_indexer_stats()
        self.assertEqual(out, "Prowlarr is not configured.")


if __name__ == "__main__":
    unittest.main()
