import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from arrstack_mcp import config

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)


class RegistryTests(unittest.TestCase):
    def test_env_names_are_unique(self):
        names = [s.env for s in config.ALL_SETTINGS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_service_starts_with_a_url_setting(self):
        for service in config.SERVICES.values():
            self.assertTrue(service.url.url, service.key)
            self.assertTrue(service.url.env.endswith("_URL"), service.key)

    def test_credentials_are_marked_secret(self):
        for setting in config.ALL_SETTINGS:
            if setting.env.endswith(("_API_KEY", "_PASS", "_TOKEN")):
                self.assertTrue(setting.secret, setting.env)

    def test_aliases_point_at_real_services(self):
        for alias, key in config.SERVICE_ALIASES.items():
            self.assertIn(key, config.SERVICES, alias)


class EnvironmentLoadingTests(TempDirTestCase):
    def test_defaults_when_nothing_is_set(self):
        loaded = config.load({})
        self.assertEqual(loaded.values["ENABLED_SERVICES"], "auto")
        self.assertEqual(loaded.values["QBT_USER"], "admin")
        self.assertEqual(loaded.values["SONARR_URL"], "")
        self.assertEqual(loaded.sources["SONARR_URL"], "default")
        self.assertEqual(loaded.problems, [])

    def test_environment_values_and_trailing_slash(self):
        loaded = config.load({"SONARR_URL": "http://sonarr:8989/sonarr/", "SONARR_API_KEY": "k"})
        self.assertEqual(loaded.values["SONARR_URL"], "http://sonarr:8989/sonarr")
        self.assertEqual(loaded.sources["SONARR_API_KEY"], "environment")

    def test_secret_file_variable(self):
        path = self.write("key", "abc123\n")
        loaded = config.load({"SONARR_API_KEY_FILE": path})
        self.assertEqual(loaded.values["SONARR_API_KEY"], "abc123")
        self.assertEqual(loaded.sources["SONARR_API_KEY"], "SONARR_API_KEY_FILE")

    def test_environment_beats_secret_file(self):
        path = self.write("key", "from-file")
        loaded = config.load({"SONARR_API_KEY": "from-env", "SONARR_API_KEY_FILE": path})
        self.assertEqual(loaded.values["SONARR_API_KEY"], "from-env")

    def test_missing_secret_file_is_a_problem(self):
        loaded = config.load({"SONARR_API_KEY_FILE": str(self.tmp / "nope")})
        self.assertEqual(len(loaded.problems), 1)
        self.assertIn("SONARR_API_KEY_FILE: cannot read", loaded.problems[0])

    def test_url_without_scheme_is_a_problem(self):
        loaded = config.load({"SONARR_URL": "sonarr:8989"})
        self.assertEqual(len(loaded.problems), 1)
        self.assertIn("missing http://", loaded.problems[0])

    def test_url_with_credentials_is_not_echoed(self):
        loaded = config.load({"SONARR_URL": "ftp://user:pw@host"})
        self.assertNotIn("pw", loaded.problems[0])


class ConfigPathTests(unittest.TestCase):
    def test_argv_forms(self):
        self.assertEqual(config.config_path({}, ["--config", "a.toml"]), "a.toml")
        self.assertEqual(config.config_path({}, ["--transport", "sse", "--config=b.toml"]), "b.toml")

    def test_environment_fallback(self):
        self.assertEqual(config.config_path({"ARRSTACK_CONFIG": "c.toml"}, []), "c.toml")
        self.assertIsNone(config.config_path({}, []))


class TomlLoadingTests(TempDirTestCase):
    def test_values_from_file(self):
        path = self.write("a.toml", """
[server]
enabled_services = ["sonarr", "qbt"]
allow_destructive = false

[sonarr]
url = "http://sonarr:8989/"
api_key = "file-key"

[qbt]
url = "http://qbittorrent:8080"
password = "pw"
""")
        loaded = config.load({}, path)
        self.assertEqual(loaded.problems, [])
        self.assertEqual(loaded.values["ENABLED_SERVICES"], "sonarr,qbt")
        self.assertEqual(loaded.values["ALLOW_DESTRUCTIVE"], "false")
        self.assertEqual(loaded.values["SONARR_URL"], "http://sonarr:8989")
        self.assertEqual(loaded.values["SONARR_API_KEY"], "file-key")
        self.assertEqual(loaded.values["QBT_PASS"], "pw")  # [qbt] alias table
        self.assertEqual(loaded.sources["SONARR_API_KEY"], "config file")

    def test_environment_overrides_file_but_empty_environment_does_not(self):
        path = self.write("a.toml", '[sonarr]\nurl = "http://from-file:8989"\napi_key = "file-key"\n')
        loaded = config.load({"SONARR_URL": "", "SONARR_API_KEY": "env-key"}, path)
        self.assertEqual(loaded.values["SONARR_URL"], "http://from-file:8989")
        self.assertEqual(loaded.values["SONARR_API_KEY"], "env-key")

    def test_key_file_inside_toml(self):
        secret = self.write("radarr.key", "rk\n")
        path = self.write("a.toml", f'[radarr]\nurl = "http://radarr:7878"\napi_key_file = "{secret}"\n')
        loaded = config.load({}, path)
        self.assertEqual(loaded.values["RADARR_API_KEY"], "rk")
        self.assertEqual(loaded.sources["RADARR_API_KEY"], "config file (api_key_file)")

    def test_inline_value_wins_over_its_file_in_either_order(self):
        secret = self.write("k", "from-file")
        for body in (f'api_key = "inline"\napi_key_file = "{secret}"\n',
                     f'api_key_file = "{secret}"\napi_key = "inline"\n'):
            loaded = config.load({}, self.write("a.toml", "[radarr]\n" + body))
            self.assertEqual(loaded.values["RADARR_API_KEY"], "inline")

    def test_unknown_section_and_key_get_suggestions(self):
        path = self.write("a.toml", '[sonar]\nurl = "x"\n\n[radarr]\napikey = "k"\n')
        problems = config.load({}, path).problems
        self.assertEqual(len(problems), 2)
        self.assertIn("unknown section [sonar] (did you mean 'sonarr'?)", problems[0])
        self.assertIn("[radarr] apikey: unknown setting (did you mean 'api_key'?)", problems[1])

    def test_bad_types_and_syntax(self):
        problems = config.load({}, self.write("a.toml", "[sonarr]\nurl = { a = 1 }\n")).problems
        self.assertIn("expected a string", problems[0])
        problems = config.load({}, self.write("b.toml", "[sonarr\n")).problems
        self.assertIn("invalid TOML", problems[0])
        problems = config.load({}, self.write("c.toml", 'sonarr = "x"\n')).problems
        self.assertIn("must be a [section]", problems[0])

    def test_missing_config_file(self):
        problems = config.load({}, str(self.tmp / "missing.toml")).problems
        self.assertIn("config file not found", problems[0])


class RenderTests(TempDirTestCase):
    VALUES = {
        "SONARR_URL": "http://sonarr:8989",
        "SONARR_API_KEY": "abc",
        "QBT_URL": "http://qbittorrent:8080",
        "QBT_USER": "admin",  # default: omitted from output
        "QBT_PASS": "p@ss word#1",
        "ENABLED_SERVICES": "sonarr,qbittorrent",
    }

    def test_toml_round_trips(self):
        path = self.write("out.toml", config.render_toml(self.VALUES))
        loaded = config.load({}, path)
        self.assertEqual(loaded.problems, [])
        for name, value in self.VALUES.items():
            self.assertEqual(loaded.values[name], value, name)

    def test_env_output(self):
        text = config.render_env(self.VALUES)
        self.assertIn("SONARR_API_KEY=abc\n", text)
        self.assertIn("QBT_PASS='p@ss word#1'\n", text)
        self.assertNotIn("QBT_USER", text)

    def test_env_rejects_unwritable_values(self):
        with self.assertRaises(ValueError):
            config.render_env({"QBT_PASS": "two\nlines"})


class GeneratedDocsTests(unittest.TestCase):
    def test_env_example_is_up_to_date(self):
        committed = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertEqual(
            committed, config.render_env_example(),
            "Run: python server.py --print-env-example > .env.example",
        )

    def test_env_example_has_no_values_for_secrets(self):
        for line in config.render_env_example().splitlines():
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                if name in config.SECRET_ENVS:
                    self.assertEqual(value, "", name)

    def test_readme_table_is_up_to_date(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        start = readme.index("<!-- BEGIN CONFIG TABLE")
        start = readme.index("\n", start) + 1
        end = readme.index("<!-- END CONFIG TABLE -->")
        self.assertEqual(readme[start:end], config.render_markdown_table())


class StartupTests(TempDirTestCase):
    def run_server(self, *args, **env):
        full_env = {k: v for k, v in os.environ.items() if not k.endswith(("_URL", "_API_KEY"))}
        full_env.update(env)
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "server.py"), *args],
            capture_output=True, text=True, env=full_env, cwd=REPO_ROOT,
        )

    def test_config_problems_stop_startup_and_list_everything(self):
        path = self.write("bad.toml", '[sonar]\nurl = "x"\n\n[radarr]\nurl = "radarr:7878"\n')
        result = self.run_server("--config", path, "--list-services")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown section [sonar]", result.stderr)
        self.assertIn("RADARR_URL", result.stderr)

    def test_config_file_drives_service_selection(self):
        path = self.write("ok.toml", '[romm]\nurl = "http://romm:8080"\napi_token = "t"\n')
        result = self.run_server("--list-services", ARRSTACK_CONFIG=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        romm_line = next(line for line in result.stdout.splitlines() if line.strip().startswith("romm"))
        self.assertIn("enabled", romm_line)

    def test_print_env_example(self):
        result = self.run_server("--print-env-example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, config.render_env_example())


if __name__ == "__main__":
    unittest.main()
