import base64
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import server

# A minimal but structurally valid bencoded .torrent payload.
TORRENT_BYTES = b"d8:announce15:http://x/announce4:infod6:lengthi1e4:name4:teste e"
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Cool.Release.1080p"


def http_response(content=b"", headers=None, status_code=200):
    resp = Mock()
    resp.content = content
    resp.headers = headers or {}
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


class TorrentDetectionTests(unittest.TestCase):
    def test_looks_like_torrent_accepts_bencoded_dict(self):
        self.assertTrue(server._looks_like_torrent(TORRENT_BYTES))

    def test_looks_like_torrent_rejects_html(self):
        self.assertFalse(server._looks_like_torrent(b"<html>nope</html>"))
        self.assertFalse(server._looks_like_torrent(b""))

    def test_decode_b64_roundtrip(self):
        encoded = base64.b64encode(TORRENT_BYTES).decode()
        self.assertEqual(server._decode_b64_torrent(encoded), TORRENT_BYTES)

    def test_decode_b64_handles_data_uri(self):
        encoded = base64.b64encode(TORRENT_BYTES).decode()
        uri = f"data:application/x-bittorrent;base64,{encoded}"
        self.assertEqual(server._decode_b64_torrent(uri), TORRENT_BYTES)

    def test_decode_b64_rejects_non_torrent(self):
        self.assertIsNone(server._decode_b64_torrent(base64.b64encode(b"hello world").decode()))
        self.assertIsNone(server._decode_b64_torrent("definitely not base64 !!!"))

    def test_magnet_display_name_uses_dn(self):
        self.assertEqual(server._magnet_display_name(MAGNET), "Cool.Release.1080p")

    def test_filename_from_url(self):
        self.assertEqual(
            server._filename_from_url("https://host/dl/My%20Movie.torrent?token=1"),
            "My Movie.torrent",
        )
        self.assertEqual(server._filename_from_url("https://host/dl/123"), "123.torrent")


class AddOptionsTests(unittest.TestCase):
    def test_save_path_disables_auto_tmm(self):
        opts = server._qbt_add_options(save_path="/downloads/tv")
        self.assertEqual(opts["savepath"], "/downloads/tv")
        self.assertEqual(opts["autoTMM"], "false")

    def test_paused_sets_both_version_flags(self):
        opts = server._qbt_add_options(paused=True)
        self.assertEqual(opts["paused"], "true")
        self.assertEqual(opts["stopped"], "true")

    def test_defaults_are_empty(self):
        self.assertEqual(server._qbt_add_options(), {})


class AddSourceDispatchTests(unittest.TestCase):
    @patch("server._qbt")
    def test_qbt_add_magnet_uses_urls_field(self, qbt):
        qbt.return_value = "Ok."
        out = server.qbt_add(MAGNET, category="tv")

        args, kwargs = qbt.call_args
        self.assertEqual(args[0], "/torrents/add")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["data"]["urls"], MAGNET)
        self.assertEqual(kwargs["data"]["category"], "tv")
        self.assertNotIn("files", kwargs)
        self.assertIn("✅", out)
        self.assertIn("Cool.Release.1080p", out)

    @patch("server._qbt")
    def test_qbt_add_base64_uploads_file(self, qbt):
        qbt.return_value = "Ok."
        encoded = base64.b64encode(TORRENT_BYTES).decode()

        out = server.qbt_add(encoded, save_path="/downloads")

        _, kwargs = qbt.call_args
        self.assertIn("files", kwargs)
        name, content, content_type = kwargs["files"]["torrents"]
        self.assertEqual(content, TORRENT_BYTES)
        self.assertEqual(content_type, "application/x-bittorrent")
        self.assertEqual(kwargs["data"]["savepath"], "/downloads")
        self.assertEqual(kwargs["data"]["autoTMM"], "false")
        self.assertIn("✅", out)

    @patch("server._qbt")
    def test_qbt_add_torrent_file_from_local_path(self, qbt):
        qbt.return_value = "Ok."
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as handle:
            handle.write(TORRENT_BYTES)
            path = handle.name
        try:
            out = server.qbt_add_torrent_file(path, category="movies")
        finally:
            os.unlink(path)

        _, kwargs = qbt.call_args
        name, content, _ = kwargs["files"]["torrents"]
        self.assertEqual(content, TORRENT_BYTES)
        self.assertTrue(name.endswith(".torrent"))
        self.assertEqual(kwargs["data"]["category"], "movies")
        self.assertIn("✅", out)

    @patch("server._qbt")
    @patch("server.httpx.get")
    def test_qbt_add_url_downloads_then_uploads(self, get, qbt):
        get.return_value = http_response(content=TORRENT_BYTES)
        qbt.return_value = "Ok."

        out = server.qbt_add("https://tracker.example/file.torrent")

        get.assert_called_once()
        _, kwargs = qbt.call_args
        self.assertEqual(kwargs["files"]["torrents"][1], TORRENT_BYTES)
        self.assertIn("✅", out)

    @patch("server._qbt")
    @patch("server.httpx.get")
    def test_qbt_add_url_resolving_to_magnet(self, get, qbt):
        get.return_value = http_response(content=b"magnet:?xt=urn:btih:DEADBEEF&dn=Redir")
        qbt.return_value = "Ok."

        out = server.qbt_add("https://tracker.example/redirect")

        _, kwargs = qbt.call_args
        self.assertIn("urls", kwargs["data"])
        self.assertNotIn("files", kwargs)
        self.assertIn("✅", out)

    @patch("server._qbt")
    def test_paused_flag_is_forwarded(self, qbt):
        qbt.return_value = "Ok."
        out = server.qbt_add(MAGNET, paused=True)

        _, kwargs = qbt.call_args
        self.assertEqual(kwargs["data"]["paused"], "true")
        self.assertEqual(kwargs["data"]["stopped"], "true")
        self.assertIn("stopped", out)


class AddErrorHandlingTests(unittest.TestCase):
    def test_qbt_add_magnet_rejects_non_magnet(self):
        out = server.qbt_add_magnet("https://tracker.example/file.torrent")
        self.assertIn("❌", out)

    def test_qbt_add_torrent_file_rejects_magnet(self):
        out = server.qbt_add_torrent_file(MAGNET)
        self.assertIn("❌", out)

    def test_unrecognized_source(self):
        out = server.qbt_add("definitely not valid !!!")
        self.assertIn("❌", out)
        self.assertIn("Unrecognized", out)

    @patch("server._qbt")
    def test_fails_response_surfaces_error(self, qbt):
        qbt.return_value = "Fails."
        out = server.qbt_add(MAGNET)
        self.assertIn("❌", out)

    @patch("server.httpx.get")
    def test_url_download_failure_is_reported(self, get):
        get.side_effect = server.httpx.RequestError("boom")
        out = server.qbt_add("https://tracker.example/file.torrent")
        self.assertIn("❌", out)
        self.assertIn("download", out.lower())


class QbtTransportTests(unittest.TestCase):
    def setUp(self):
        self.qbt_url = server.QBT_URL
        self.qbt_sid = server._qbt_sid

    def tearDown(self):
        server.QBT_URL = self.qbt_url
        server._qbt_sid = self.qbt_sid

    @patch("server.httpx.request")
    @patch("server.httpx.post")
    def test_qbt_forwards_files_to_request(self, post, request):
        server.QBT_URL = "http://qbittorrent:8080"
        server._qbt_sid = None
        login = Mock()
        login.cookies.get.return_value = "sid"
        login.raise_for_status.return_value = None
        post.return_value = login
        result = Mock()
        result.status_code = 200
        result.text = "Ok."
        result.json.side_effect = ValueError("not json")
        result.raise_for_status.return_value = None
        request.return_value = result

        files = {"torrents": ("a.torrent", TORRENT_BYTES, "application/x-bittorrent")}
        out = server._qbt("/torrents/add", method="POST", data={"category": "tv"}, files=files)

        _, kwargs = request.call_args
        self.assertEqual(kwargs["files"], files)
        self.assertEqual(out, "Ok.")


if __name__ == "__main__":
    unittest.main()
