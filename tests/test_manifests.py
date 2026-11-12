import json
import tempfile
import unittest
from pathlib import Path

from plugins_manager import PluginSpec, SyncError, Tree
from plugins_manager.manifests import write_manifests

COMMIT = "abcdef123456789"
AGENT_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


class ManifestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.stage = Tree(self.root)

    def read(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def test_platform_fields_and_discovered_components(self):
        self.stage.write_text("skills/example/SKILL.md", "# Skill")
        legacy_mcp = {"mcpServers": {
            "local": {"command": "python", "args": ["-m", "example"]},
            "remote": {"type": "http", "url": "https://example.test/mcp"},
        }}
        self.stage.write_text(".mcp.json", json.dumps(legacy_mcp))
        self.stage.write_text("hooks/hooks.json", '{"version":1,"hooks":{}}')
        self.stage.write_text(".github/plugin/plugin.json", '{"name":"old"}')
        spec = PluginSpec(
            "fixture", "upstream",
            metadata={
                "description": "Summarize research references",
                "author": {"name": "Publisher"},
                "homepage": "https://example.test/research",
            },
            manifest_fields={
                "codex": {"interface": {"displayName": "Research", "defaultPrompt": ["Find papers"]}},
                "claude": {"userConfig": {"label": {"type": "string", "title": "Label"}}},
                "copilot": {"extensions": {"com.github.copilot": {}}},
            },
        )
        write_manifests(self.root, spec, self.stage, COMMIT[:7], COMMIT)

        codex = self.read(".codex-plugin/plugin.json")
        self.assertEqual(codex["skills"], "./skills/")
        self.assertEqual(codex["mcpServers"], "./.mcp.json")
        self.assertEqual(codex["interface"]["displayName"], "Research")
        self.assertEqual(codex["interface"]["developerName"], "Publisher")
        self.assertEqual(codex["interface"]["websiteURL"], codex["homepage"])
        self.assertEqual(codex["interface"]["defaultPrompt"], ["Find papers"])
        self.assertEqual(codex["version"], "0.0.0+git.abcdef1")
        self.assertNotIn("logo", codex["interface"])
        self.assertNotIn("userConfig", codex)

        claude = self.read(".claude-plugin/plugin.json")
        self.assertEqual(claude["userConfig"]["label"]["type"], "string")
        self.assertEqual(claude["version"], COMMIT[:7])
        self.assertNotIn("interface", claude)
        self.assertNotIn("skills", claude)

        copilot = self.read("plugin.json")
        self.assertEqual(copilot["$schema"], AGENT_SCHEMA)
        self.assertEqual(copilot["extensions"], {"com.github.copilot": {}})
        self.assertTrue({"skills", "hooks", "mcpServers", "interface"}.isdisjoint(copilot))
        self.assertFalse((self.root / ".github/plugin/plugin.json").exists())
        self.assertTrue((self.root / "skills/example/SKILL.md").is_file())
        self.assertEqual(self.read(".mcp.json"), legacy_mcp)
        self.assertEqual(self.read("com.github.copilot/hooks/hooks.json"), self.read("hooks/hooks.json"))
        mcp = self.read("mcp.json")
        self.assertEqual(mcp["$schema"], AGENT_SCHEMA.replace("plugin.schema.json", "mcp.schema.json"))
        self.assertEqual(mcp["mcpServers"]["local"]["type"], "stdio")
        self.assertEqual(mcp["mcpServers"]["remote"]["type"], "streamable-http")

    def test_codex_keeps_explicit_website(self):
        spec = PluginSpec(
            "fixture",
            "upstream",
            manifests={".codex-plugin/plugin.json": "template"},
            metadata={"homepage": "https://example.test/project"},
            manifest_fields={"codex": {"interface": {"websiteURL": "https://example.test/product"}}},
        )
        write_manifests(self.root, spec, self.stage, "1.2.3", COMMIT)
        codex = self.read(".codex-plugin/plugin.json")
        self.assertEqual(codex["interface"]["websiteURL"], "https://example.test/product")

    def test_copilot_does_not_migrate_claude_hooks(self):
        self.stage.write_text("hooks/hooks.json", '{"hooks":{"SessionStart":[]}}')
        spec = PluginSpec("fixture", "upstream", manifests={"plugin.json": "template"})
        write_manifests(self.root, spec, self.stage, "1.2.3-rc.1", COMMIT)
        copilot = self.read("plugin.json")
        self.assertNotIn("hooks", copilot)
        self.assertFalse((self.root / "com.github.copilot/hooks/hooks.json").exists())
        self.assertEqual(copilot["version"], "1.2.3-rc.1")

    def test_existing_portable_manifest_keeps_schema_and_extensions(self):
        self.stage.write_text("skills/example/SKILL.md", "# Skill")
        self.stage.write_text("plugin.json", json.dumps({
            "$schema": AGENT_SCHEMA,
            "name": "upstream",
            "skills": ["obsolete"],
            "extensions": {"com.openai": {"interface": {"displayName": "Upstream"}}},
        }))
        spec = PluginSpec("fixture", "upstream", manifests={"plugin.json": "template"})
        write_manifests(self.root, spec, self.stage, "1.2.3", COMMIT)
        copilot = self.read("plugin.json")
        self.assertEqual(copilot["$schema"], AGENT_SCHEMA)
        self.assertEqual(copilot["name"], "fixture")
        self.assertEqual(copilot["extensions"]["com.openai"]["interface"]["displayName"], "Upstream")
        self.assertNotIn("skills", copilot)

    def test_legacy_manifest_paths_and_preserve_mode_are_rejected(self):
        with self.assertRaisesRegex(SyncError, "Copilot manifest must use nested/plugin.json"):
            PluginSpec("fixture", "upstream", manifests={"nested/.github/plugin/plugin.json": "merge"})
        self.stage.write_text("plugin.json", '{"name":"fixture"}')
        spec = PluginSpec("fixture", "upstream", manifests={"plugin.json": "preserve"})
        with self.assertRaisesRegex(SyncError, r"Copilot requires a supported Agent Plugins " + r"\$schema"):
            write_manifests(self.root, spec, self.stage, "1.2.3", COMMIT)

    def test_mismatched_mcp_schema_is_rejected(self):
        self.stage.write_text("mcp.json", '{"$schema":"wrong","mcpServers":{}}')
        spec = PluginSpec("fixture", "upstream", manifests={"plugin.json": "template"})
        with self.assertRaisesRegex(SyncError, "mcp.json must use"):
            write_manifests(self.root, spec, self.stage, "1.2.3", COMMIT)
        self.assertFalse((self.root / "plugin.json").exists())

    def test_portable_config_rejects_legacy_fields(self):
        spec = PluginSpec(
            "fixture", "upstream", manifests={"plugin.json": "template"},
            manifest_fields={"copilot": {"$schema": AGENT_SCHEMA, "hooks": "hooks.json"}},
        )
        with self.assertRaisesRegex(SyncError, "Unsupported copilot manifest fields: hooks"):
            write_manifests(self.root, spec, self.stage, "1.2.3", COMMIT)
        self.assertFalse((self.root / "plugin.json").exists())
