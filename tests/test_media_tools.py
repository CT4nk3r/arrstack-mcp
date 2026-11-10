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
