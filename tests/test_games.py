import unittest
from unittest.mock import Mock, patch

import server


def response(data):
    result = Mock()
    result.json.return_value = data
    result.raise_for_status.return_value = None
    return result


class GameLibraryTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "ROMM_URL": server.ROMM_URL,
            "ROMM_API_TOKEN": server.ROMM_API_TOKEN,
            "ROMM_USER": server.ROMM_USER,
            "ROMM_PASS": server.ROMM_PASS,
            "GAMEVAULT_URL": server.GAMEVAULT_URL,
            "GAMEVAULT_API_KEY": server.GAMEVAULT_API_KEY,
        }

    def tearDown(self):
        for name, value in self.settings.items():
            setattr(server, name, value)

    @patch("server.httpx.request")
    def test_romm_list_games_uses_bearer_auth_and_params(self, request):
        server.ROMM_URL = "http://romm:8080"
        server.ROMM_API_TOKEN = "token"
        request.return_value = response(
            {
                "total": 1,
                "items": [
                    {
                        "id": 7,
                        "name": "Mario Kart DS",
                        "platform_display_name": "Nintendo DS",
                        "fs_size_bytes": 1024,
                    }
                ],
            }
        )

        output = server.romm_list_games("Mario", platform_id=3, limit=500)

        self.assertIn("Mario Kart DS", output)
        _, kwargs = request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token")
        self.assertEqual(kwargs["params"]["search_term"], "Mario")
        self.assertEqual(kwargs["params"]["platform_ids"], 3)
        self.assertEqual(kwargs["params"]["limit"], 100)

    @patch("server.httpx.request")
    def test_romm_system_info_is_public(self, request):
        server.ROMM_URL = "http://romm:8080"
        server.ROMM_API_TOKEN = ""
        request.return_value = response(
            {
                "SYSTEM": {"VERSION": "4.9.0"},
                "FILESYSTEM": {"FS_PLATFORMS": ["nds", "3ds"]},
                "METADATA_SOURCES": {"LIBRETRO_API_ENABLED": True},
            }
        )

        output = server.romm_system_info()

        self.assertIn("4.9.0", output)
        self.assertIn("nds, 3ds", output)

    @patch("server.httpx.request")
    def test_gamevault_search_uses_api_key_and_params(self, request):
        server.GAMEVAULT_URL = "http://gamevault:8080"
        server.GAMEVAULT_API_KEY = "key"
        request.return_value = response(
            {
                "data": [
                    {
                        "id": 4,
                        "title": "The Witcher 3",
                        "type": "WINDOWS_SETUP",
                        "size": "2147483648",
                    }
                ],
                "meta": {"totalItems": 1},
            }
        )

        output = server.gamevault_list_games("Witcher", page=0, limit=500)

        self.assertIn("The Witcher 3", output)
        _, kwargs = request.call_args
        self.assertEqual(kwargs["headers"]["X-Api-Key"], "key")
        self.assertEqual(kwargs["params"], {"page": 1, "limit": 100, "search": "Witcher"})

    def test_missing_gamevault_api_key_has_clear_error(self):
        server.GAMEVAULT_URL = "http://gamevault:8080"
        server.GAMEVAULT_API_KEY = ""

        output = server.gamevault_list_games()

        self.assertIn("GAMEVAULT_API_KEY", output)


if __name__ == "__main__":
    unittest.main()
