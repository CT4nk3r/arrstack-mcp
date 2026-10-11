import unittest
from unittest.mock import Mock, patch

import server


def response(data):
    result = Mock()
    result.status_code = 200
    result.text = ""
    result.json.return_value = data
    result.raise_for_status.return_value = None
    return result


def flat_item(name, allowed):
    return {"quality": {"id": 1, "name": name}, "items": [], "allowed": allowed}


def group_item(name, allowed, member_names):
    return {
        "id": 1000,
        "name": name,
        "allowed": allowed,
        "items": [flat_item(m, allowed) for m in member_names],
    }


class ApplyQualityAllowlistTests(unittest.TestCase):
    def test_toggles_flat_item(self):
        items = [flat_item("Bluray-1080p", False)]
        changed, not_found = server._apply_quality_allowlist(items, {"bluray-1080p"}, set())
        self.assertTrue(items[0]["allowed"])
        self.assertEqual(changed, ["+Bluray-1080p"])
        self.assertEqual(not_found, [])

    def test_toggles_group_by_group_name(self):
        items = [group_item("WEB 1080p", False, ["WEBDL-1080p", "WEBRip-1080p"])]
        changed, not_found = server._apply_quality_allowlist(items, {"web 1080p"}, set())
        self.assertTrue(items[0]["allowed"])
        self.assertEqual(changed, ["+WEB 1080p"])
        self.assertEqual(not_found, [])

    def test_toggles_group_by_member_name(self):
        # Requesting a name that only exists inside a group must toggle the
        # enclosing group, since the group's own flag is what Radarr/Sonarr
        # actually honor — toggling just the nested leaf would have no effect.
        items = [group_item("WEB 1080p", False, ["WEBDL-1080p", "WEBRip-1080p"])]
        changed, not_found = server._apply_quality_allowlist(items, {"webdl-1080p"}, set())
        self.assertTrue(items[0]["allowed"])
        self.assertEqual(changed, ["+WEB 1080p"])
        self.assertEqual(not_found, [])

    def test_disallow_toggles_off(self):
        items = [flat_item("CAM", True)]
        changed, not_found = server._apply_quality_allowlist(items, set(), {"cam"})
        self.assertFalse(items[0]["allowed"])
        self.assertEqual(changed, ["-CAM"])

    def test_no_op_when_already_in_desired_state(self):
        items = [flat_item("Bluray-1080p", True)]
        changed, not_found = server._apply_quality_allowlist(items, {"bluray-1080p"}, set())
        self.assertEqual(changed, [])
        self.assertEqual(not_found, [])

    def test_reports_names_not_found(self):
        items = [flat_item("Bluray-1080p", False)]
        changed, not_found = server._apply_quality_allowlist(items, {"not-a-real-quality"}, set())
        self.assertEqual(changed, [])
        self.assertEqual(not_found, ["not-a-real-quality"])

    def test_matching_is_based_on_lowercased_item_name(self):
        # The helper's contract is that callers pass already-lowercased
        # allow/disallow sets (see test_user_facing_input_is_case_insensitive
        # for where the actual case-folding of user input happens); this just
        # confirms the item's own name casing doesn't matter on this side.
        items = [flat_item("BLURAY-1080P", False)]
        changed, _ = server._apply_quality_allowlist(items, {"bluray-1080p"}, set())
        self.assertEqual(changed, ["+BLURAY-1080P"])


class SetQualityProfileQualitiesToolTests(unittest.TestCase):
    def setUp(self):
        self.radarr_url = server.RADARR_URL
        self.sonarr_url = server.SONARR_URL

    def tearDown(self):
        server.RADARR_URL = self.radarr_url
        server.SONARR_URL = self.sonarr_url

    def test_invalid_profile_id_rejected(self):
        self.assertEqual(server.radarr_set_quality_profile_qualities(0, allow="x"), "Invalid profile_id.")
        self.assertEqual(server.sonarr_set_quality_profile_qualities(-1, allow="x"), "Invalid profile_id.")

    def test_requires_at_least_one_change(self):
        result = server.radarr_set_quality_profile_qualities(1)
        self.assertIn("Provide at least one quality name", result)

    def test_rejects_overlap_between_allow_and_disallow(self):
        result = server.radarr_set_quality_profile_qualities(1, allow="CAM", disallow="cam")
        self.assertIn("can't be in both allow and disallow", result)

    @patch("server.httpx.request")
    def test_applies_change_and_puts_full_profile_back(self, request):
        server.RADARR_URL = "http://radarr:7878"
        profile = {
            "id": 4,
            "name": "HD-1080p",
            "cutoff": 20,
            "language": {"id": 1},
            "items": [flat_item("Bluray-1080p", False)],
        }
        # First call: GET the profile. Second call: PUT the mutated profile.
        request.side_effect = [response(profile), response(profile)]

        result = server.radarr_set_quality_profile_qualities(4, allow="Bluray-1080p")

        self.assertEqual(request.call_count, 2)
        put_call = request.call_args_list[1]
        method_arg = put_call.args[0] if put_call.args else put_call.kwargs.get("method")
        self.assertEqual(method_arg, "PUT")
        sent_profile = put_call.kwargs["json"]
        self.assertTrue(sent_profile["items"][0]["allowed"])
        # Untouched fields must survive the round-trip unchanged.
        self.assertEqual(sent_profile["id"], 4)
        self.assertEqual(sent_profile["cutoff"], 20)
        self.assertIn("Updated 'HD-1080p'", result)
        self.assertIn("+Bluray-1080p", result)

    @patch("server.httpx.request")
    def test_user_facing_input_is_case_insensitive(self, request):
        server.RADARR_URL = "http://radarr:7878"
        profile = {"id": 4, "name": "HD-1080p", "items": [flat_item("Bluray-1080p", False)]}
        request.side_effect = [response(profile), response(profile)]

        result = server.radarr_set_quality_profile_qualities(4, allow="BLURAY-1080P")

        self.assertIn("+Bluray-1080p", result)

    @patch("server.httpx.request")
    def test_no_http_put_when_nothing_changed(self, request):
        server.RADARR_URL = "http://radarr:7878"
        profile = {"id": 4, "name": "HD-1080p", "items": [flat_item("Bluray-1080p", True)]}
        request.side_effect = [response(profile)]

        result = server.radarr_set_quality_profile_qualities(4, allow="Bluray-1080p")

        self.assertEqual(request.call_count, 1)  # GET only, no PUT
        self.assertEqual(result, "No changes made.")


if __name__ == "__main__":
    unittest.main()
