import unittest
from unittest.mock import Mock, patch

import server


def response(data=None, status_code=200, text=""):
    result = Mock()
    result.status_code = status_code
    result.text = text
    result.json.return_value = data
    result.raise_for_status.return_value = None
    return result


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.sonarr_url = server.SONARR_URL
        self.qbt_url = server.QBT_URL
        self.qbt_sid = server._qbt_sid

    def tearDown(self):
        server.SONARR_URL = self.sonarr_url
        server.QBT_URL = self.qbt_url
        server._qbt_sid = self.qbt_sid

    @patch("server.httpx.request")
    def test_search_term_is_passed_as_query_parameter(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        request.return_value = response([])

        server.sonarr_search("show&apikey=leak")

        args, kwargs = request.call_args
        self.assertEqual(args[1], "http://sonarr:8989/api/v3/series/lookup")
        self.assertEqual(kwargs["params"], {"term": "show&apikey=leak"})

    @patch("server.httpx.request")
    @patch("server.httpx.post")
    def test_qbt_retries_only_once_after_403(self, post, request):
        server.QBT_URL = "http://qbittorrent:8080"
        server._qbt_sid = None
        login = response()
        login.cookies.get.return_value = "sid"
        post.return_value = login
        request.return_value = response(status_code=403)

        output = server._qbt("/torrents/info")

        self.assertIn("403 after retry", output)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(post.call_count, 2)

    def test_invalid_ids_are_rejected_before_http(self):
        self.assertEqual(server.sonarr_get_series(0), "Invalid series_id.")
        self.assertEqual(server.radarr_get_movie(-1), "Invalid movie_id.")
        self.assertEqual(server.prowlarr_test_indexer(0), "Invalid indexer_id.")

    def test_dns_rebinding_protection_is_enabled(self):
        settings = server.mcp.settings.transport_security
        self.assertTrue(settings.enable_dns_rebinding_protection)
        self.assertIn("localhost:*", settings.allowed_hosts)


if __name__ == "__main__":
    unittest.main()
