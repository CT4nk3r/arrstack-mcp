import os
import pathlib
import stat
import tempfile
import unittest

import httpx

from arrstack_mcp import config
from arrstack_mcp import discover
from arrstack_mcp import wizard

SECRET = "wizard-secret-5c4b3a"


class ScriptedIO:
    """Answers prompts by substring; a list answers successive matching prompts."""

    def __init__(self, answers, secrets=None):
        self.answers = answers  # [(substring, answer or [answers])], first match wins
        self.secrets = list(secrets or [])
        self.output = []
        self.prompts = []

    def _answer(self, prompt):
        self.prompts.append(prompt)
        for needle, answer in self.answers:
            if needle in prompt:
                if isinstance(answer, list):
                    return answer.pop(0) if len(answer) > 1 else answer[0]
                return answer
        return ""

    def ask(self, prompt):
        return self._answer(prompt)

    def secret(self, prompt):
        self.prompts.append(prompt)
        return self.secrets.pop(0) if self.secrets else ""

    def prompter(self):
        return wizard.Prompter(ask_fn=self.ask, secret_fn=self.secret, out=self.output.append)

    @property
    def text(self):
        return "\n".join(self.output)


def sonarr_transport(good_key=SECRET):
    def handler(request):
        if request.url.path.endswith("/api/v3/system/status"):
            if request.headers.get("x-api-key") == good_key:
                return httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.9"})
            return httpx.Response(401)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


ONLY_SONARR = [("Numbers or names", "sonarr"), ("Docker", "n")]


class WizardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.output = self.tmp / "arrstack.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def run_wizard(self, io, loaded=None, fmt="toml", output=None, **kwargs):
        return wizard.run_setup(
            loaded or config.load({}), output or self.output, fmt, prompter=io.prompter(),
            transport=kwargs.pop("transport", sonarr_transport()), **kwargs,
        )


class FreshSetupTests(WizardTestCase):
    def test_configures_tests_and_writes_private_file(self):
        io = ScriptedIO(ONLY_SONARR, secrets=[SECRET])
        result = self.run_wizard(io)

        self.assertEqual(result.configured, ["sonarr"])
        mode = stat.S_IMODE(os.stat(self.output).st_mode)
        self.assertEqual(mode, 0o600)
        loaded = config.load({}, str(self.output))
        self.assertEqual(loaded.problems, [])
        self.assertEqual(loaded.values["SONARR_URL"], "http://localhost:8989")
        self.assertEqual(loaded.values["SONARR_API_KEY"], SECRET)
        self.assertIn("✅ Sonarr 4.0.9 at http://localhost:8989", io.text)
        self.assertIn("--config", io.text)  # client snippets
        self.assertIn("claude mcp add arrstack", io.text)
        self.assertNotIn(SECRET, io.text)
        self.assertNotIn(SECRET, "\n".join(io.prompts))

    def test_docker_answer_defaults_to_container_names(self):
        io = ScriptedIO([("Numbers or names", "1"), ("Docker", "y")], secrets=[SECRET])
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"appName": "Sonarr"}))
        self.run_wizard(io, transport=transport)
        self.assertEqual(config.load({}, str(self.output)).values["SONARR_URL"], "http://sonarr:8989")

    def test_retry_after_rejected_key(self):
        io = ScriptedIO(ONLY_SONARR + [("re-enter", "r")], secrets=["wrong", SECRET])
        result = self.run_wizard(io)
        self.assertEqual(result.values["SONARR_API_KEY"], SECRET)
        self.assertIn("credentials rejected (HTTP 401)", io.text)

    def test_skip_after_failure_drops_the_service(self):
        io = ScriptedIO(ONLY_SONARR + [("re-enter", "s")], secrets=["wrong"])
        result = self.run_wizard(io)
        self.assertIsNone(result)  # nothing left to write
        self.assertFalse(self.output.exists())

    def test_keep_anyway_after_failure(self):
        io = ScriptedIO(ONLY_SONARR + [("re-enter", "k")], secrets=["offline-key"])
        result = self.run_wizard(io)
        self.assertEqual(result.values["SONARR_API_KEY"], "offline-key")
        self.assertTrue(self.output.exists())

    def test_url_without_scheme_is_caught_before_probing(self):
        io = ScriptedIO(ONLY_SONARR + [("Sonarr URL", ["sonarr:8989", "http://localhost:8989"]),
                                       ("re-enter", "r")], secrets=[SECRET, ""])
        result = self.run_wizard(io)
        self.assertIn("is not an http(s) URL", io.text)
        self.assertEqual(result.values["SONARR_URL"], "http://localhost:8989")

    def test_env_format(self):
        io = ScriptedIO(ONLY_SONARR, secrets=[SECRET])
        output = self.tmp / ".env"
        self.run_wizard(io, fmt="env", output=output)
        text = output.read_text()
        self.assertIn("SONARR_URL=http://localhost:8989\n", text)
        self.assertIn(f"SONARR_API_KEY={SECRET}\n", text)
        self.assertIn("env_file:", io.text)

    def test_disallowing_destructive_tools_is_saved(self):
        io = ScriptedIO(ONLY_SONARR + [("Allow tools", "n")], secrets=[SECRET])
        self.run_wizard(io)
        self.assertEqual(config.load({}, str(self.output)).values["ALLOW_DESTRUCTIVE"], "false")

    def test_picker_rejects_unknown_names_then_accepts_aliases(self):
        io = ScriptedIO([("Numbers or names", ["books, sonar", "qbt sonarr"]), ("Docker", "n"),
                         ("re-enter", "s")], secrets=["pw", SECRET])
        result = self.run_wizard(io)
        self.assertIn("Unknown: books, sonar", io.text)
        self.assertEqual(result.configured, ["sonarr"])  # qBittorrent skipped after its test failed
        self.assertIn("Movies & TV", io.text)  # grouped list

    def test_picker_defaults_to_configured_services(self):
        loaded = config.load({"SONARR_URL": "http://localhost:8989"})
        io = ScriptedIO([("Docker", "n")], secrets=[SECRET])  # Enter accepts the default pick
        result = self.run_wizard(io, loaded=loaded)
        self.assertEqual(result.configured, ["sonarr"])
        picker = next(p for p in io.prompts if "Numbers or names" in p)
        self.assertIn("[1]", picker)


class ExistingConfigTests(WizardTestCase):
    def test_existing_file_is_backed_up_and_kept_values_survive(self):
        self.output.write_text(
            '[server]\nenabled_services = "radarr"\n\n[radarr]\nurl = "http://radarr:7878"\napi_key = "rk"\n'
        )
        loaded = config.load({}, str(self.output))
        io = ScriptedIO(ONLY_SONARR + [("Replace it", "y")], secrets=[SECRET])
        result = self.run_wizard(io, loaded=loaded)

        self.assertIsNotNone(result.backup)
        self.assertIn("radarr", result.backup.read_text())
        new = config.load({}, str(self.output)).values
        self.assertEqual(new["RADARR_API_KEY"], "rk")  # unpicked service keeps its values...
        self.assertEqual(new["ENABLED_SERVICES"], "sonarr")  # ...but only picked ones are enabled

    def test_keeping_a_current_secret(self):
        self.output.write_text('[sonarr]\nurl = "http://localhost:8989"\napi_key = "' + SECRET + '"\n')
        loaded = config.load({}, str(self.output))
        io = ScriptedIO(ONLY_SONARR + [("Replace it", "y")], secrets=[""])  # Enter keeps it
        result = self.run_wizard(io, loaded=loaded)
        self.assertEqual(result.values["SONARR_API_KEY"], SECRET)
        self.assertIn("Enter keeps the current value", "\n".join(io.prompts))

    def test_environment_secrets_are_not_copied_into_the_file(self):
        loaded = config.load({"RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "env-only"})
        io = ScriptedIO(ONLY_SONARR, secrets=[SECRET])
        self.run_wizard(io, loaded=loaded)
        self.assertNotIn("env-only", self.output.read_text())

    def test_declining_replace_writes_nothing(self):
        self.output.write_text("# keep me\n")
        io = ScriptedIO(ONLY_SONARR + [("Replace it", "n")], secrets=[SECRET])
        self.assertIsNone(self.run_wizard(io))
        self.assertEqual(self.output.read_text(), "# keep me\n")


class DiscoveredDefaultsTests(WizardTestCase):
    def test_discovered_url_and_key_are_offered(self):
        found = {"sonarr": discover.Found("sonarr", "http://sonarr:8989/sonarr", SECRET, "/cfg/sonarr/config.xml")}
        io = ScriptedIO(ONLY_SONARR + [("Use the SONARR_API_KEY", "y")])
        result = self.run_wizard(io, discovered=found)
        self.assertEqual(result.values["SONARR_URL"], "http://sonarr:8989/sonarr")
        self.assertEqual(result.values["SONARR_API_KEY"], SECRET)
        self.assertIn("API key found", io.text)
        self.assertNotIn(SECRET, io.text)


if __name__ == "__main__":
    unittest.main()
