import unittest
from unittest.mock import patch

from arrstack_mcp import server


class QbtRemoveTests(unittest.TestCase):
    @patch("arrstack_mcp.server._qbt")
    def test_remove_torrent_keeps_files(self, qbt):
        qbt.return_value = ""
        out = server.qbt_remove_torrent(" abc ")
        qbt.assert_called_once_with(
            "/torrents/delete", method="POST", data={"hashes": "abc", "deleteFiles": "false"}
        )
        self.assertEqual(out, "🗑️ Removed. (files kept)")

    @patch("arrstack_mcp.server._qbt")
    def test_remove_torrent_and_files_deletes_files(self, qbt):
        qbt.return_value = "Ok."
        out = server.qbt_remove_torrent_and_files("abc")
        qbt.assert_called_once_with(
            "/torrents/delete", method="POST", data={"hashes": "abc", "deleteFiles": "true"}
        )
        self.assertEqual(out, "🗑️ Removed. (files removed)")

    @patch("arrstack_mcp.server._qbt")
    def test_empty_hash_is_rejected_without_a_call(self, qbt):
        self.assertTrue(server.qbt_remove_torrent("").startswith("❌"))
        self.assertTrue(server.qbt_remove_torrent_and_files(" ").startswith("❌"))
        qbt.assert_not_called()

    @patch("arrstack_mcp.server._qbt")
    def test_failure_is_reported(self, qbt):
        qbt.return_value = "qBittorrent request failed: HTTP 500"
        self.assertTrue(server.qbt_remove_torrent("abc").startswith("❌"))

    def test_both_tools_are_destructive_gated(self):
        self.assertIn("qbt_remove_torrent", server.DESTRUCTIVE_TOOLS)
        self.assertIn("qbt_remove_torrent_and_files", server.DESTRUCTIVE_TOOLS)


class ArrDeleteTests(unittest.TestCase):
    @patch("arrstack_mcp.server._radarr")
    def test_radarr_delete_movie_defaults(self, radarr):
        radarr.return_value = {}
        out = server.radarr_delete_movie(7)
        radarr.assert_called_once_with(
            "/movie/7",
            method="DELETE",
            params={"deleteFiles": "true", "addImportExclusion": "false"},
        )
        self.assertTrue(out.startswith("✅"))

    @patch("arrstack_mcp.server._radarr")
    def test_radarr_delete_movie_flags(self, radarr):
        radarr.return_value = {}
        server.radarr_delete_movie(7, delete_files=False, add_import_exclusion=True)
        self.assertEqual(
            radarr.call_args.kwargs["params"],
            {"deleteFiles": "false", "addImportExclusion": "true"},
        )

    @patch("arrstack_mcp.server._radarr")
    def test_radarr_delete_movie_rejects_bad_id(self, radarr):
        self.assertEqual(server.radarr_delete_movie(0), "Invalid movie_id.")
        radarr.assert_not_called()

    @patch("arrstack_mcp.server._radarr")
    def test_radarr_delete_movie_reports_failure(self, radarr):
        radarr.return_value = "radarr request failed: HTTP 404"
        self.assertTrue(server.radarr_delete_movie(7).startswith("❌"))

    @patch("arrstack_mcp.server._sonarr")
    def test_sonarr_delete_series_defaults(self, sonarr):
        sonarr.return_value = {}
        out = server.sonarr_delete_series(3)
        sonarr.assert_called_once_with(
            "/series/3",
            method="DELETE",
            params={"deleteFiles": "true", "addImportListExclusion": "false"},
        )
        self.assertTrue(out.startswith("✅"))

    @patch("arrstack_mcp.server._sonarr")
    def test_sonarr_delete_series_flags(self, sonarr):
        sonarr.return_value = {}
        server.sonarr_delete_series(3, delete_files=False, add_import_list_exclusion=True)
        self.assertEqual(
            sonarr.call_args.kwargs["params"],
            {"deleteFiles": "false", "addImportListExclusion": "true"},
        )

    @patch("arrstack_mcp.server._sonarr")
    def test_sonarr_delete_series_rejects_bad_id(self, sonarr):
        self.assertEqual(server.sonarr_delete_series(-1), "Invalid series_id.")
        sonarr.assert_not_called()

    @patch("arrstack_mcp.server._sonarr")
    def test_sonarr_delete_series_reports_failure(self, sonarr):
        sonarr.return_value = "sonarr request failed: HTTP 404"
        self.assertTrue(server.sonarr_delete_series(3).startswith("❌"))

    def test_both_tools_are_destructive_gated(self):
        self.assertIn("radarr_delete_movie", server.DESTRUCTIVE_TOOLS)
        self.assertIn("sonarr_delete_series", server.DESTRUCTIVE_TOOLS)
