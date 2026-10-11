import pathlib
import tempfile
import threading
import unittest

import httpx

import config
import discover


class FakeNetwork:
    """MockTransport handler: (host, port, path) -> response; anything else is unreachable."""

    def __init__(self, routes):
        self.routes = routes
        self.hosts = set()
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.hosts.add(request.url.host)
        response = self.routes.get((request.url.host, request.url.port, request.url.path))
        if response is None:
            raise httpx.ConnectError("unreachable")
        return response


ROUTES = {
    ("sonarr", 8989, "/sonarr/ping"): httpx.Response(200, json={"status": "OK"}),
    ("localhost", 7878, "/ping"): httpx.Response(200, json={"status": "OK"}),
    ("qbittorrent", 8080, "/api/v2/app/version"): httpx.Response(403, text="Forbidden"),
    ("jellyfin", 8096, "/jellyfin/System/Info/Public"):
        httpx.Response(200, json={"Version": "10.11.8", "ProductName": "Jellyfin Server"}),
    ("seerr", 5055, "/api/v1/status"): httpx.Response(200, json={"version": "2.7.0", "commitTag": "x"}),
    # Something on SABnzbd's default port that is not SABnzbd:
    ("sabnzbd", 8080, "/sabnzbd/api"): httpx.Response(200, text="<html></html>"),
}


class NetworkDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.network = FakeNetwork(ROUTES)
        self.found = discover.discover_network(transport=httpx.MockTransport(self.network))

    def test_finds_services_with_url_bases(self):
        self.assertEqual(self.found["sonarr"].url, "http://sonarr:8989/sonarr")
        self.assertEqual(self.found["radarr"].url, "http://localhost:7878")
        self.assertEqual(self.found["qbittorrent"].url, "http://qbittorrent:8080")
        self.assertEqual(self.found["jellyfin"].url, "http://jellyfin:8096/jellyfin")
        self.assertEqual(self.found["seerr"].url, "http://seerr:5055")

    def test_ignores_non_matching_answers(self):
        self.assertNotIn("sabnzbd", self.found)
        self.assertNotIn("lidarr", self.found)

    def test_only_contacts_known_candidate_hosts(self):
        allowed = {"localhost"} | {h for s in config.SERVICES.values() for h in s.hostnames}
        self.assertTrue(self.network.hosts <= allowed, self.network.hosts - allowed)


SONARR_XML = """<Config>
  <Port>8989</Port>
  <UrlBase>/sonarr</UrlBase>
  <ApiKey>0123456789abcdef</ApiKey>
  <InstanceName>Sonarr</InstanceName>
</Config>"""


class ArrConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def config_dir(self, name, xml):
        directory = self.tmp / name
        directory.mkdir()
        (directory / "config.xml").write_text(xml)
        return directory

    def test_reads_port_base_and_key(self):
        found = discover.read_arr_config(self.config_dir("anything", SONARR_XML))
        self.assertEqual(found.service, "sonarr")
        self.assertEqual(found.url, "http://sonarr:8989/sonarr")
        self.assertEqual(found.api_key, "0123456789abcdef")
        self.assertNotIn("0123456789abcdef", found.describe())

    def test_service_from_directory_name_when_no_instance_name(self):
        found = discover.read_arr_config(self.config_dir("radarr", "<Config><ApiKey>k</ApiKey></Config>"))
        self.assertEqual(found.service, "radarr")
        self.assertEqual(found.url, "http://radarr:7878")

    def test_unreadable_or_unknown_configs_are_skipped(self):
        self.assertIsNone(discover.read_arr_config(self.tmp / "missing"))
        self.assertIsNone(discover.read_arr_config(self.config_dir("other", "<Config/>")))
        self.assertIsNone(discover.read_arr_config(self.config_dir("sonarr2", "not xml")))

    def test_network_url_wins_and_config_key_is_kept(self):
        directory = self.config_dir("sonarr", SONARR_XML.replace("<UrlBase>/sonarr</UrlBase>", ""))
        found = discover.discover([str(directory)], transport=httpx.MockTransport(FakeNetwork(ROUTES)))
        self.assertEqual(found["sonarr"].url, "http://sonarr:8989/sonarr")
        self.assertEqual(found["sonarr"].api_key, "0123456789abcdef")
        self.assertIn("+ network", found["sonarr"].source)

    def test_config_only_without_network(self):
        found = discover.discover([str(self.config_dir("sonarr", SONARR_XML))], network=False)
        self.assertEqual(list(found), ["sonarr"])


if __name__ == "__main__":
    unittest.main()
