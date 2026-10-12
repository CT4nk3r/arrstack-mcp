import os
import subprocess
import sys
import unittest

import server


def _advertised_tools(**env_overrides):
    env = os.environ.copy()
    env.update(env_overrides)
    code = (
        "import server;"
        "server._configure_service_tools();"
        "print(','.join(t.name for t in server.mcp._tool_manager.list_tools()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env
    )
    return set(result.stdout.strip().split(","))


class InstructionPresetTests(unittest.TestCase):
    def test_default_instructions_have_no_deployment_specific_workflow(self):
        text = server._build_instructions()
        self.assertIn("Homelab media stack tools", text)
        self.assertNotIn("nCore", text)

    def test_preset_is_appended(self):
        text = server._build_instructions("hun-ncore")
        self.assertIn("Homelab media stack tools", text)
        self.assertIn("## Hungarian (HuN) / nCore workflow", text)

    def test_preset_names_are_case_and_space_insensitive(self):
        self.assertEqual(server._instruction_presets(" HUN-nCore , "), ["hun-ncore"])

    def test_extra_instructions_come_last(self):
        text = server._build_instructions("hun-ncore", "  Always answer in Hungarian.  ")
        self.assertTrue(text.rstrip().endswith("Always answer in Hungarian."))

    def test_unknown_preset_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown instruction preset 'nope'"):
            server._instruction_presets("hun-ncore,nope")

    def test_unknown_preset_fails_startup(self):
        env = os.environ.copy()
        env["INSTRUCTION_PRESETS"] = "nope"
        result = subprocess.run(
            [sys.executable, "server.py", "--list-services"],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown instruction preset", result.stderr)


class DestructiveToolTests(unittest.TestCase):
    def test_parse_bool(self):
        for value in ("1", "true", "Yes", " on "):
            self.assertTrue(server._parse_bool("X", value))
        for value in ("0", "false", "NO", "off"):
            self.assertFalse(server._parse_bool("X", value))
        with self.assertRaisesRegex(ValueError, "X must be true or false"):
            server._parse_bool("X", "maybe")

    def test_destructive_list_covers_every_delete_tool(self):
        # Guards new tools: anything named *delete* must be classified.
        names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
        self.assertTrue(server.DESTRUCTIVE_TOOLS <= names, server.DESTRUCTIVE_TOOLS - names)
        deleting = {name for name in names if "delete" in name}
        self.assertEqual(deleting - server.DESTRUCTIVE_TOOLS, set())

    def test_destructive_tools_advertised_by_default(self):
        names = _advertised_tools(ENABLED_SERVICES="all", ALLOW_DESTRUCTIVE="true")
        self.assertIn("radarr_delete_movie_file", names)
        self.assertIn("qbt_delete", names)

    def test_allow_destructive_false_hides_delete_tools(self):
        names = _advertised_tools(ENABLED_SERVICES="all", ALLOW_DESTRUCTIVE="false")
        self.assertFalse(names & server.DESTRUCTIVE_TOOLS)
        self.assertIn("radarr_add_movie", names)
        self.assertIn("qbt_pause", names)


if __name__ == "__main__":
    unittest.main()
