import unittest
from unittest.mock import Mock, patch

import server


@patch.object(server, "JELLYFIN_URL", "http://jellyfin:8096")
@patch.object(server, "JELLYFIN_API_KEY", "test-api-key")
class JellyfinAuthenticationTests(unittest.TestCase):
    @patch("server.httpx.get")
    def test_library_listing_uses_mediabrowser_authorization(self, get):
        get.return_value = Mock()
        get.return_value.json.return_value = [
            {"Name": "Movies", "CollectionType": "movies", "Locations": ["/movies"]}
        ]

        result = server.jellyfin_libraries()

        self.assertIn("Movies", result)
        get.assert_called_once_with(
            "http://jellyfin:8096/Library/VirtualFolders",
            headers={"Authorization": 'MediaBrowser Token="test-api-key"'},
            params=None,
            timeout=30,
        )

    @patch("server.httpx.post")
    def test_library_scan_uses_mediabrowser_authorization(self, post):
        post.return_value = Mock()

        result = server.jellyfin_scan_library()

        self.assertIn("Library scan triggered", result)
        post.assert_called_once_with(
            "http://jellyfin:8096/Library/Refresh",
            headers={"Authorization": 'MediaBrowser Token="test-api-key"'},
            timeout=30,
        )

    @patch("server.httpx.get")
    def test_public_system_info_without_api_key_omits_authorization(self, get):
        get.return_value = Mock()
        get.return_value.json.return_value = {"ServerName": "Test", "Version": "12.1.0"}

        with patch.object(server, "JELLYFIN_API_KEY", ""):
            result = server.jellyfin_system_info()

        self.assertIn("12.1.0", result)
        self.assertEqual(get.call_args.kwargs["headers"], {})

    @patch("server.httpx.post")
    def test_library_scan_without_api_key_does_not_send_request(self, post):
        with patch.object(server, "JELLYFIN_API_KEY", ""):
            result = server.jellyfin_scan_library()

        self.assertIn("JELLYFIN_API_KEY is required", result)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
