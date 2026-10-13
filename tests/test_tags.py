import unittest
from unittest.mock import Mock, patch

from arrstack_mcp import server


def response(data):
    result = Mock()
    result.status_code = 200
    result.text = ""
    result.json.return_value = data
    result.raise_for_status.return_value = None
    return result


ALL_TAGS = [{"id": 2, "label": "2 - bryan"}, {"id": 3, "label": "3 - laura"}]


class ResolveTagIdsTests(unittest.TestCase):
    def test_resolves_matching_labels(self):
        ids, unmatched = server._resolve_tag_ids(ALL_TAGS, "2 - bryan, 3 - laura")
        self.assertEqual(sorted(ids), [2, 3])
        self.assertEqual(unmatched, [])

    def test_reports_unmatched_labels(self):
        ids, unmatched = server._resolve_tag_ids(ALL_TAGS, "2 - bryan, nope")
        self.assertEqual(ids, [2])
        self.assertEqual(unmatched, ["nope"])

    def test_case_insensitive(self):
        ids, unmatched = server._resolve_tag_ids(ALL_TAGS, "2 - BRYAN")
        self.assertEqual(ids, [2])
        self.assertEqual(unmatched, [])

    def test_ignores_blank_entries(self):
        ids, unmatched = server._resolve_tag_ids(ALL_TAGS, "2 - bryan,, ")
        self.assertEqual(ids, [2])
        self.assertEqual(unmatched, [])


class SetMovieTagsTests(unittest.TestCase):
    def setUp(self):
        self.radarr_url = server.RADARR_URL

    def tearDown(self):
        server.RADARR_URL = self.radarr_url

    def test_invalid_movie_id_rejected(self):
        self.assertEqual(server.radarr_set_movie_tags(0, tags="x"), "Invalid movie_id.")

    def test_invalid_mode_rejected(self):
        result = server.radarr_set_movie_tags(1, tags="x", mode="bogus")
        self.assertIn("Invalid mode", result)

    def test_empty_tags_rejected(self):
        result = server.radarr_set_movie_tags(1, tags="")
        self.assertIn("Provide at least one tag", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_add_mode_unions_with_existing_tags(self, request):
        server.RADARR_URL = "http://radarr:7878"
        movie = {"id": 5, "title": "Movie", "tags": [2]}
        request.side_effect = [
            response(movie),  # GET movie
            response(ALL_TAGS),  # GET /tag
            response({**movie, "tags": [2]}),  # PUT movie
        ]

        result = server.radarr_set_movie_tags(5, tags="2 - bryan", mode="add")

        put_call = request.call_args_list[-1]
        self.assertEqual(put_call.kwargs["json"]["tags"], [2])
        self.assertIn("now [2]", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_add_mode_creates_missing_tag(self, request):
        server.RADARR_URL = "http://radarr:7878"
        movie = {"id": 5, "title": "Movie", "tags": []}
        request.side_effect = [
            response(movie),  # GET movie
            response([]),  # GET /tag — nothing exists yet
            response({"id": 10, "label": "rewatch"}),  # POST /tag — create it
            response({**movie, "tags": [10]}),  # PUT movie
        ]

        result = server.radarr_set_movie_tags(5, tags="rewatch", mode="add")

        create_call = request.call_args_list[2]
        self.assertEqual(create_call.args[0] if create_call.args else create_call.kwargs.get("method"), "POST")
        self.assertEqual(create_call.kwargs["json"], {"label": "rewatch"})
        put_call = request.call_args_list[-1]
        self.assertEqual(put_call.kwargs["json"]["tags"], [10])
        self.assertIn("now [10]", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_remove_mode_with_unmatched_tag_errors_without_put(self, request):
        server.RADARR_URL = "http://radarr:7878"
        movie = {"id": 5, "title": "Movie", "tags": [2]}
        request.side_effect = [response(movie), response(ALL_TAGS)]

        result = server.radarr_set_movie_tags(5, tags="does-not-exist", mode="remove")

        self.assertIn("not found", result)
        self.assertEqual(request.call_count, 2)  # GET movie + GET tag only, no PUT

    @patch("arrstack_mcp.server.httpx.request")
    def test_remove_mode_subtracts(self, request):
        server.RADARR_URL = "http://radarr:7878"
        movie = {"id": 5, "title": "Movie", "tags": [2, 3]}
        request.side_effect = [response(movie), response(ALL_TAGS), response({**movie, "tags": [3]})]

        server.radarr_set_movie_tags(5, tags="2 - bryan", mode="remove")

        put_call = request.call_args_list[-1]
        self.assertEqual(put_call.kwargs["json"]["tags"], [3])

    @patch("arrstack_mcp.server.httpx.request")
    def test_set_mode_replaces_entirely(self, request):
        server.RADARR_URL = "http://radarr:7878"
        movie = {"id": 5, "title": "Movie", "tags": [2, 3]}
        request.side_effect = [response(movie), response(ALL_TAGS), response({**movie, "tags": [3]})]

        server.radarr_set_movie_tags(5, tags="3 - laura", mode="set")

        put_call = request.call_args_list[-1]
        self.assertEqual(put_call.kwargs["json"]["tags"], [3])


class ListTagsAndFilterTests(unittest.TestCase):
    def setUp(self):
        self.radarr_url = server.RADARR_URL

    def tearDown(self):
        server.RADARR_URL = self.radarr_url

    @patch("arrstack_mcp.server.httpx.request")
    def test_list_tags_shows_counts(self, request):
        server.RADARR_URL = "http://radarr:7878"
        request.return_value = response(
            [{"id": 2, "label": "2 - bryan", "movieIds": [1, 2, 3]}]
        )

        result = server.radarr_list_tags()

        self.assertIn("2 - bryan", result)
        self.assertIn("3 movies", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_list_movies_by_tag_filters(self, request):
        server.RADARR_URL = "http://radarr:7878"
        movies = [
            {"id": 1, "title": "Tagged", "year": 2020, "tags": [2]},
            {"id": 2, "title": "Untagged", "year": 2021, "tags": []},
        ]
        request.side_effect = [response(ALL_TAGS), response(movies)]

        result = server.radarr_list_movies_by_tag("2 - bryan")

        self.assertIn("Tagged", result)
        self.assertNotIn("Untagged", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_list_movies_by_tag_reports_unknown_tag(self, request):
        server.RADARR_URL = "http://radarr:7878"
        request.side_effect = [response(ALL_TAGS)]

        result = server.radarr_list_movies_by_tag("nonexistent")

        self.assertIn("Tag not found", result)
        self.assertEqual(request.call_count, 1)  # never fetched /movie


class SonarrTagWiringTests(unittest.TestCase):
    """Sonarr's tag tools share _resolve_tag_ids (fully covered above) and
    mirror Radarr's implementation exactly — this just confirms the Sonarr
    side is wired to the right URL/endpoints, not a full behavior re-test."""

    def setUp(self):
        self.sonarr_url = server.SONARR_URL

    def tearDown(self):
        server.SONARR_URL = self.sonarr_url

    @patch("arrstack_mcp.server.httpx.request")
    def test_set_series_tags_add_mode(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        series = {"id": 7, "title": "Show", "tags": []}
        request.side_effect = [
            response(series),
            response(ALL_TAGS),
            response({**series, "tags": [2]}),
        ]

        result = server.sonarr_set_series_tags(7, tags="2 - bryan", mode="add")

        put_call = request.call_args_list[-1]
        self.assertEqual(put_call.kwargs["json"]["tags"], [2])
        self.assertIn("now [2]", result)

    @patch("arrstack_mcp.server.httpx.request")
    def test_list_series_by_tag_filters(self, request):
        server.SONARR_URL = "http://sonarr:8989"
        series_list = [
            {"id": 1, "title": "Tagged", "year": 2020, "tags": [2]},
            {"id": 2, "title": "Untagged", "year": 2021, "tags": []},
        ]
        request.side_effect = [response(ALL_TAGS), response(series_list)]

        result = server.sonarr_list_series_by_tag("2 - bryan")

        self.assertIn("Tagged", result)
        self.assertNotIn("Untagged", result)


if __name__ == "__main__":
    unittest.main()


class TagLabelLookupTests(unittest.TestCase):
    def test_failed_tag_lookup_yields_no_labels(self):
        # An error string from the helper must not be iterated as tag data.
        self.assertEqual(server._tag_labels(lambda path: "radarr request error: boom"), {})

    def test_tag_lookup_maps_ids_to_labels(self):
        data = [{"id": 1, "label": "2 - bryan"}, {"id": 2, "label": "rewatch"}]
        self.assertEqual(server._tag_labels(lambda path: data), {1: "2 - bryan", 2: "rewatch"})
