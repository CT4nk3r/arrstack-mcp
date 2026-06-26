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


class MagnetDisplayNameTests(unittest.TestCase):
    def test_falls_back_to_btih_hash(self):
        name = server._magnet_display_name("magnet:?xt=urn:btih:DEADBEEFCAFEBABE0000")
        self.assertIn("DEADBEEFCAFEBABE", name)

    def test_falls_back_to_generic_label(self):
        self.assertEqual(server._magnet_display_name("magnet:?tr=udp://x"), "magnet link")


class DecodeBase64EdgeTests(unittest.TestCase):
    def test_whitespace_and_newlines_are_ignored(self):
        encoded = base64.b64encode(TORRENT_BYTES).decode()
        wrapped = "\n".join(encoded[i : i + 8] for i in range(0, len(encoded), 8))
        self.assertEqual(server._decode_b64_torrent(f"  {wrapped}\n"), TORRENT_BYTES)

    def test_too_short_returns_none(self):
        self.assertIsNone(server._decode_b64_torrent("abc="))


class AddResultNormalizationTests(unittest.TestCase):
    def test_ok_is_success(self):
        self.assertEqual(server._qbt_add_result("Ok."), (True, ""))

    def test_ok_is_case_insensitive(self):
        ok, _ = server._qbt_add_result("ok.")
        self.assertTrue(ok)

    def test_fails_is_reported(self):
        ok, detail = server._qbt_add_result("Fails.")
        self.assertFalse(ok)
        self.assertIn("rejected", detail.lower())

    def test_plain_error_text_passes_through(self):
        ok, detail = server._qbt_add_result("qBittorrent is not configured.")
        self.assertFalse(ok)
        self.assertEqual(detail, "qBittorrent is not configured.")

    def test_non_string_result_is_failure(self):
        ok, detail = server._qbt_add_result({"unexpected": True})
        self.assertFalse(ok)

    def test_empty_response_is_friendly(self):
        ok, detail = server._qbt_add_result("")
        self.assertFalse(ok)
        self.assertIn("empty", detail.lower())


class FetchTorrentTests(unittest.TestCase):
    @patch("server.httpx.get")
    def test_uses_content_disposition_filename(self, get):
        get.return_value = http_response(
            content=TORRENT_BYTES,
            headers={"content-disposition": 'attachment; filename="Cool.Release.torrent"'},
        )
        kind, value = server._qbt_fetch_torrent("https://host/dl?id=9")
        self.assertEqual(kind, "file")
        self.assertEqual(value[0], "Cool.Release.torrent")
        self.assertEqual(value[1], TORRENT_BYTES)

    @patch("server.httpx.get")
    def test_appends_torrent_extension_to_disposition_name(self, get):
        get.return_value = http_response(
            content=TORRENT_BYTES,
            headers={"content-disposition": "attachment; filename=release"},
        )
        _, value = server._qbt_fetch_torrent("https://host/dl")
        self.assertTrue(value[0].endswith(".torrent"))

    @patch("server.httpx.get")
    def test_html_body_is_rejected(self, get):
        get.return_value = http_response(content=b"<html>login required</html>")
        kind, message = server._qbt_fetch_torrent("https://host/dl")
        self.assertEqual(kind, "error")
        self.assertIn("valid .torrent", message)

    @patch("server.httpx.get")
    def test_http_status_error_is_reported(self, get):
        resp = http_response()
        error_response = Mock()
        error_response.status_code = 403
        resp.raise_for_status.side_effect = server.httpx.HTTPStatusError(
            "403", request=Mock(), response=error_response
        )
        get.return_value = resp
        kind, message = server._qbt_fetch_torrent("https://host/dl")
        self.assertEqual(kind, "error")
        self.assertIn("download", message.lower())
        self.assertIn("403", message)


class UrlRedactionTests(unittest.TestCase):
    def test_redact_strips_query_and_userinfo(self):
        redacted = server._redact_url("https://user:pass@tracker.example/dl?apikey=SECRET&passkey=PRIV")
        self.assertIn("tracker.example/dl", redacted)
        for leak in ("SECRET", "PRIV", "apikey", "passkey", "user", "pass"):
            self.assertNotIn(leak, redacted)

    @patch("server.httpx.get")
    def test_fetch_error_does_not_leak_token(self, get):
        get.return_value = http_response(content=b"<html>denied</html>")
        url = "https://tracker.example/download?apikey=TOPSECRET&passkey=PRIVATE"
        kind, message = server._qbt_fetch_torrent(url)
        self.assertEqual(kind, "error")
        self.assertNotIn("TOPSECRET", message)
        self.assertNotIn("PRIVATE", message)
        self.assertIn("tracker.example", message)

    @patch("server.httpx.get")
    def test_oversized_response_is_refused(self, get):
        big = b"d" + b"0" * 64
        get.return_value = http_response(content=big)
        with patch.object(server, "MAX_TORRENT_BYTES", 16):
            kind, message = server._qbt_fetch_torrent("https://host/x?token=zzz")
        self.assertEqual(kind, "error")
        self.assertIn("too large", message)
        self.assertNotIn("zzz", message)


class DispositionParsingTests(unittest.TestCase):
    def test_quoted_filename_with_trailing_params(self):
        self.assertEqual(
            server._filename_from_disposition('attachment; filename="My File.torrent"; size=123'),
            "My File.torrent",
        )

    def test_rfc5987_filename_star_is_decoded(self):
        self.assertEqual(
            server._filename_from_disposition("attachment; filename*=UTF-8''My%20Release.torrent"),
            "My Release.torrent",
        )

    def test_empty_disposition_returns_blank(self):
        self.assertEqual(server._filename_from_disposition(""), "")


class TrackerlessTorrentTests(unittest.TestCase):
    def test_torrent_without_announce_is_accepted(self):
        trackerless = b"d10:created by3:foo4:infod6:lengthi1e4:name4:teste e"
        self.assertNotIn(b"announce", trackerless)
        self.assertTrue(server._looks_like_torrent(trackerless))

    def test_info_key_beyond_old_window_is_accepted(self):
        padded = b"d13:announce-list" + b"l5:x:000e" * 400 + b"4:infod6:lengthi1e e"
        self.assertGreater(len(padded), 2048)
        self.assertTrue(server._looks_like_torrent(padded))


class ResolveSourceTests(unittest.TestCase):
    def test_magnet_is_detected(self):
        self.assertEqual(server._qbt_resolve_source(f"  {MAGNET}  "), ("magnet", MAGNET))

    def test_existing_non_torrent_file_is_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as handle:
            handle.write(b"not a torrent at all")
            path = handle.name
        try:
            kind, message = server._qbt_resolve_source(path)
        finally:
            os.unlink(path)
        self.assertEqual(kind, "error")
        self.assertIn("not a valid", message)

    def test_unreadable_garbage_is_rejected(self):
        kind, message = server._qbt_resolve_source("/no/such/path/file.torrent")
        self.assertEqual(kind, "error")
        self.assertIn("Unrecognized", message)


class TorrentFileSourceVariantTests(unittest.TestCase):
    @patch("server._qbt")
    def test_accepts_base64_content(self, qbt):
        qbt.return_value = "Ok."
        encoded = base64.b64encode(TORRENT_BYTES).decode()

        out = server.qbt_add_torrent_file(encoded)

        _, kwargs = qbt.call_args
        self.assertEqual(kwargs["files"]["torrents"][1], TORRENT_BYTES)
        self.assertIn("✅", out)

    @patch("server._qbt")
    @patch("server.httpx.get")
    def test_accepts_http_url(self, get, qbt):
        get.return_value = http_response(content=TORRENT_BYTES)
        qbt.return_value = "Ok."

        out = server.qbt_add_torrent_file("https://tracker.example/file.torrent")

        get.assert_called_once()
        _, kwargs = qbt.call_args
        self.assertEqual(kwargs["files"]["torrents"][1], TORRENT_BYTES)
        self.assertIn("✅", out)


class SuccessMessageTests(unittest.TestCase):
    @patch("server._qbt")
    def test_category_appears_in_message(self, qbt):
        qbt.return_value = "Ok."
        out = server.qbt_add(MAGNET, category="movies")
        self.assertIn("→ movies", out)

    @patch("server._qbt")
    def test_add_magnet_reports_category_and_stopped(self, qbt):
        qbt.return_value = "Ok."
        out = server.qbt_add_magnet(MAGNET, category="tv", paused=True)
        self.assertIn("→ tv", out)
        self.assertIn("stopped", out)
        self.assertIn("Cool.Release.1080p", out)

    @patch("server._qbt")
    def test_not_configured_message_surfaces(self, qbt):
        qbt.return_value = "qBittorrent is not configured. Set QBT_URL and QBT_PASS."
        out = server.qbt_add(MAGNET)
        self.assertIn("❌", out)
        self.assertIn("not configured", out)


if __name__ == "__main__":
    unittest.main()
