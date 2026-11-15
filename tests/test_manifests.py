import json
import tempfile
import unittest
from pathlib import Path

from plugins_manager import PlatformSpec, PluginSpec, SyncError, Tree
from plugins_manager.manifest import write_manifest

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
            platforms={
                "codex": PlatformSpec("codex", manifest_overrides={
                    "interface": {"displayName": "Research", "defaultPrompt": ["Find papers"]},
                }),
                "claude": PlatformSpec("claude", manifest_overrides={
                    "userConfig": {"label": {"type": "string", "title": "Label"}},
                }),
                "copilot": PlatformSpec("copilot", manifest_overrides={
                    "extensions": {"com.github.copilot": {}},
                }),
            },
        )
        write_manifest(self.root, spec, self.stage, COMMIT[:7])

        codex = self.read(".codex-plugin/plugin.json")
        self.assertEqual(codex["skills"], "./skills/")
        self.assertEqual(codex["mcpServers"], "./mcp.json")
        self.assertEqual(codex["interface"]["displayName"], "Research")
        self.assertEqual(codex["interface"]["developerName"], "Publisher")
        self.assertEqual(codex["interface"]["websiteURL"], codex["homepage"])
        self.assertEqual(codex["interface"]["defaultPrompt"], ["Find papers"])
        self.assertEqual(codex["version"], COMMIT[:7])
        self.assertNotIn("logo", codex["interface"])
        self.assertNotIn("userConfig", codex)

        claude = self.read(".claude-plugin/plugin.json")
        self.assertEqual(claude["userConfig"]["label"]["type"], "string")
        self.assertEqual(claude["version"], COMMIT[:7])
        self.assertNotIn("interface", claude)
        self.assertNotIn("skills", claude)

        copilot = self.read("plugin.json")
        self.assertEqual(copilot["version"], COMMIT[:7])
        self.assertEqual(copilot["$schema"], AGENT_SCHEMA)
        self.assertEqual(copilot["extensions"]["com.github.copilot"], {})
        self.assertEqual(copilot["extensions"]["com.openai"]["interface"], codex["interface"])
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
            metadata={"homepage": "https://example.test/project"},
            platforms={"codex": PlatformSpec("codex", manifest_overrides={
                "interface": {"websiteURL": "https://example.test/product"},
            })},
        )
        write_manifest(self.root, spec, self.stage, "1.2.3")
        codex = self.read(".codex-plugin/plugin.json")
        self.assertEqual(codex["interface"]["websiteURL"], "https://example.test/product")

    def test_copilot_does_not_migrate_claude_hooks(self):
        self.stage.write_text("hooks/hooks.json", '{"hooks":{"SessionStart":[]}}')
        spec = PluginSpec("fixture", "upstream", platforms={"copilot": PlatformSpec("copilot")})
        write_manifest(self.root, spec, self.stage, "1.2.3-rc.1")
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
        spec = PluginSpec("fixture", "upstream", platforms={"copilot": PlatformSpec("copilot")})
        write_manifest(self.root, spec, self.stage, "1.2.3")
        copilot = self.read("plugin.json")
        self.assertEqual(copilot["$schema"], AGENT_SCHEMA)
        self.assertEqual(copilot["name"], "fixture")
        self.assertEqual(copilot["extensions"]["com.openai"]["interface"]["displayName"], "Upstream")
        self.assertNotIn("skills", copilot)

    def test_preserve_rejects_legacy_copilot_manifest(self):
        self.stage.write_text("plugin.json", '{"name":"fixture"}')
        spec = PluginSpec("fixture", "upstream", platforms={"copilot": PlatformSpec("copilot", mode="preserve")})
        with self.assertRaisesRegex(SyncError, r"Copilot requires a supported Agent Plugins " + r"\$schema"):
            write_manifest(self.root, spec, self.stage, "1.2.3")

    def test_mismatched_mcp_schema_is_rejected(self):
        self.stage.write_text("mcp.json", '{"$schema":"wrong","mcpServers":{}}')
        spec = PluginSpec("fixture", "upstream", platforms={"copilot": PlatformSpec("copilot")})
        with self.assertRaisesRegex(SyncError, "mcp.json must use"):
            write_manifest(self.root, spec, self.stage, "1.2.3")
        self.assertFalse((self.root / "plugin.json").exists())

    def test_portable_config_rejects_legacy_fields(self):
        with self.assertRaisesRegex(SyncError, "unknown fields.*hooks"):
            PlatformSpec("copilot", manifest_overrides={"hooks": "hooks.json"})
        self.assertFalse((self.root / "plugin.json").exists())

    def test_shared_root_requires_consistent_core_metadata(self):
        codex = PlatformSpec("codex", manifest_overrides={"version": "codex-version"})
        copilot = PlatformSpec("copilot", {"version": "copilot-version"})
        spec = PluginSpec("fixture", "upstream", platforms={"codex": codex, "copilot": copilot})
        with self.assertRaisesRegex(SyncError, "share a root with different version"):
            write_manifest(self.root, spec, self.stage, "shared-version")
        spec = PluginSpec("fixture", "upstream", platforms={
            "codex": codex, "copilot": PlatformSpec("copilot", copilot.metadata, root="copilot"),
        })
        write_manifest(self.root, spec, self.stage, "shared-version")
        self.assertEqual(self.read(".codex-plugin/plugin.json")["version"], "codex-version")
        self.assertEqual(self.read("copilot/plugin.json")["version"], "copilot-version")

    def test_rejects_two_platforms_with_the_same_manifest_path(self):
        with self.assertRaisesRegex(SyncError, r"claude and copilot use the same manifest path"):
            PluginSpec.parse("fixture", {
                "repo": "upstream",
                "metadata": {
                    "claude": {},
                    "copilot": {"manifest": {"root": ".claude-plugin"}},
                },
            })
