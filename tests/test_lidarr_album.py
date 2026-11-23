import unittest

from arrstack_mcp.server import prepare_lidarr_album


class PrepareLidarrAlbumTests(unittest.TestCase):
    def test_monitors_only_the_album(self):
        album = {
            "title": "Homework",
            "foreignAlbumId": "00054665-89fa-33d5-a8f0-1728ea8c32c3",
            "monitored": False,
            "artist": {"artistName": "Daft Punk", "foreignArtistId": "artist", "monitored": True},
        }

        prepared = prepare_lidarr_album(album, 1, 1, "/data/music", search=True)

        self.assertTrue(prepared["monitored"])
        self.assertEqual(prepared["addOptions"], {"searchForNewAlbum": True})
        self.assertFalse(prepared["artist"]["monitored"])
        self.assertEqual(
            prepared["artist"]["addOptions"],
            {"monitor": "none", "searchForMissingAlbums": False},
        )
        self.assertEqual(prepared["artist"]["qualityProfileId"], 1)
        self.assertEqual(prepared["artist"]["rootFolderPath"], "/data/music")
        self.assertTrue(album["artist"]["monitored"])
