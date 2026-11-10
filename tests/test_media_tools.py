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


SERVERS = [
    {"id": 1, "name": "Radarr 4K", "isDefault": True, "is4k": True},
    {"id": 2, "name": "Radarr", "isDefault": True, "is4k": False},
    {"id": 3, "name": "Radarr spare", "isDefault": False, "is4k": False},
]
DETAIL = {
    "server": {"id": 2},
    "profiles": [{"id": 4, "name": "HD-1080p"}, {"id": 6, "name": "Ultra-HD"}],
}


def seerr_router(kind, servers=SERVERS, detail=DETAIL, request_result=None):
    def route(path, method="GET", json=None, params=None):
        if method == "POST" and path == "/request":
            return request_result if request_result is not None else {"id": 55, "status": 1}
        if path == f"/service/{kind}":
            return servers
        if path.startswith(f"/service/{kind}/"):
            return detail
        raise AssertionError(f"unexpected seerr call {method} {path}")

    return route


class SeerrDeleteRequestTests(unittest.TestCase):
    @patch("arrstack_mcp.server._seerr")
    def test_deletes_request(self, seerr):
        seerr.return_value = {}
        out = server.seerr_delete_request(12)
        seerr.assert_called_once_with("/request/12", method="DELETE")
        self.assertTrue(out.startswith("✅"))

    @patch("arrstack_mcp.server._seerr")
    def test_rejects_bad_id(self, seerr):
        self.assertEqual(server.seerr_delete_request(0), "Invalid request_id.")
        seerr.assert_not_called()

    @patch("arrstack_mcp.server._seerr")
    def test_reports_failure(self, seerr):
        seerr.return_value = "seerr request failed: HTTP 404"
        self.assertTrue(server.seerr_delete_request(12).startswith("❌"))

    def test_destructive_gated(self):
        self.assertIn("seerr_delete_request", server.DESTRUCTIVE_TOOLS)


class SeerrQualityTests(unittest.TestCase):
    @patch("arrstack_mcp.server._seerr")
    def test_movie_default_quality_resolves_profile(self, seerr):
        seerr.side_effect = seerr_router("radarr", detail={
            "profiles": [{"id": 9, "name": "SD"}, {"id": 4, "name": "1080p"}],
        })
        out = server.seerr_request_movie(603)
        paths = [c.args[0] for c in seerr.call_args_list]
        self.assertEqual(paths, ["/service/radarr", "/service/radarr/2", "/request"])
        self.assertEqual(
            seerr.call_args_list[-1].kwargs["json"],
            {"mediaType": "movie", "mediaId": 603, "serverId": 2, "profileId": 4},
        )
        self.assertTrue(out.startswith("✅"))

    @patch("arrstack_mcp.server._seerr")
    def test_movie_quality_match_is_case_insensitive(self, seerr):
        seerr.side_effect = seerr_router("radarr")
        server.seerr_request_movie(603, quality="hd-1080P")
        body = seerr.call_args_list[-1].kwargs["json"]
        self.assertEqual((body["serverId"], body["profileId"]), (2, 4))

    @patch("arrstack_mcp.server._seerr")
    def test_movie_unknown_quality_lists_valid_names(self, seerr):
        seerr.side_effect = seerr_router("radarr")
        out = server.seerr_request_movie(603, quality="720p")
        self.assertTrue(out.startswith("❌"))
        self.assertIn("720p", out)
        self.assertIn("HD-1080p", out)
        self.assertIn("Ultra-HD", out)
        self.assertNotIn("/request", [c.args[0] for c in seerr.call_args_list if c.kwargs.get("method") == "POST"])

    @patch("arrstack_mcp.server._seerr")
    def test_movie_no_default_non_4k_server(self, seerr):
        servers = [SERVERS[0], SERVERS[2]]
        seerr.side_effect = seerr_router("radarr", servers=servers)
        out = server.seerr_request_movie(603)
        self.assertTrue(out.startswith("❌"))
        self.assertIn("default non-4K", out)
        self.assertEqual(seerr.call_count, 1)

    @patch("arrstack_mcp.server._seerr")
    def test_movie_service_lookup_error_is_returned(self, seerr):
        seerr.return_value = "seerr request failed: HTTP 500"
        self.assertEqual(server.seerr_request_movie(603), "seerr request failed: HTTP 500")

    @patch("arrstack_mcp.server._seerr")
    def test_tv_resolves_profile_through_sonarr(self, seerr):
        seerr.side_effect = seerr_router("sonarr", servers=[
            {"id": 8, "name": "Sonarr", "isDefault": True, "is4k": False},
        ])
        out = server.seerr_request_tv(1399, seasons="1,2", quality="HD-1080p")
        paths = [c.args[0] for c in seerr.call_args_list]
        self.assertEqual(paths, ["/service/sonarr", "/service/sonarr/8", "/request"])
        self.assertEqual(
            seerr.call_args_list[-1].kwargs["json"],
            {"mediaType": "tv", "mediaId": 1399, "seasons": [1, 2], "serverId": 8, "profileId": 4},
        )
        self.assertTrue(out.startswith("✅"))

    @patch("arrstack_mcp.server._seerr")
    def test_tv_unknown_quality_lists_valid_names(self, seerr):
        seerr.side_effect = seerr_router("sonarr", servers=[
            {"id": 8, "name": "Sonarr", "isDefault": True, "is4k": False},
        ])
        out = server.seerr_request_tv(1399, quality="nope")
        self.assertTrue(out.startswith("❌"))
        self.assertIn("HD-1080p", out)
        self.assertIn("Ultra-HD", out)

    @patch("arrstack_mcp.server._seerr")
    def test_tv_invalid_seasons_skips_lookup(self, seerr):
        out = server.seerr_request_tv(1399, seasons="x")
        self.assertIn("Invalid seasons", out)
        seerr.assert_not_called()
