import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from plugins_manager import PluginSpec, SyncError, load_specs
from plugins_manager.marketplace import MARKETPLACE_PATHS, write_marketplaces


CODEX_MARKETPLACE = MARKETPLACE_PATHS["codex"]


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="test-marketplace-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.spec = PluginSpec(
            "split-plugin",
            "local-source",
            manifests={
                "claude/.claude-plugin/plugin.json": "template",
                "codex/.codex-plugin/plugin.json": "template",
                "claude/plugin.json": "template",
            },
        )

    def test_marketplaces_match_platform_plugin_roots(self):
        paths = write_marketplaces(self.root, [self.spec])
        self.assertEqual(paths, [self.root.resolve() / path for path in MARKETPLACE_PATHS.values()])
        for relative in MARKETPLACE_PATHS.values():
            marketplace = json.loads((self.root / relative).read_text(encoding="utf-8"))
            [entry] = marketplace["plugins"]
            self.assertEqual(entry["name"], "split-plugin")
            if relative == CODEX_MARKETPLACE:
                self.assertEqual(
                    entry["source"], {"source": "local", "path": "./plugins/split-plugin/codex"}
                )
                self.assertEqual(entry["category"], "Productivity")
            else:
                self.assertEqual(entry["source"], "./plugins/split-plugin/claude")
                self.assertIn("name", marketplace["owner"])

    def test_recipe_marketplaces_filter_new_and_existing_entries(self):
        config = self.root / "recipe.toml"
        recipe = '[plugins.fixture]\nrepo = "local-source"\n'
        config.write_text(recipe + 'marketplaces = ["codex", "copilot"]\n', encoding="utf-8")
        claude = self.root / MARKETPLACE_PATHS["claude"]

        write_marketplaces(self.root, load_specs(config).values())
        self.assertFalse(claude.exists())

        write_marketplaces(self.root, [PluginSpec("fixture", "local-source")], types=["claude"])
        unrelated = {"name": "unmanaged", "source": "./other"}
        marketplace = json.loads(claude.read_text(encoding="utf-8"))
        marketplace["plugins"].append(unrelated)
        claude.write_text(json.dumps(marketplace), encoding="utf-8")

        write_marketplaces(self.root, load_specs(config).values())
        self.assertEqual(json.loads(claude.read_text(encoding="utf-8"))["plugins"], [unrelated])
        for kind in ("codex", "copilot"):
            marketplace = json.loads((self.root / MARKETPLACE_PATHS[kind]).read_text(encoding="utf-8"))
            self.assertEqual([entry["name"] for entry in marketplace["plugins"]], ["fixture"])

        config.write_text(recipe + "marketplaces = []\n", encoding="utf-8")
        write_marketplaces(self.root, load_specs(config).values())
        for kind in ("codex", "copilot"):
            marketplace = json.loads((self.root / MARKETPLACE_PATHS[kind]).read_text(encoding="utf-8"))
            self.assertEqual(marketplace["plugins"], [])

    def test_recipe_rejects_unknown_marketplace(self):
        config = self.root / "recipe.toml"
        config.write_text(
            '[plugins.fixture]\nrepo = "local-source"\nmarketplaces = ["unknown"]\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SyncError, "Expected one of codex, claude, copilot"):
            load_specs(config)

    def test_existing_marketplace_keeps_unmanaged_entries_and_custom_fields(self):
        path = self.root / CODEX_MARKETPLACE
        path.parent.mkdir(parents=True)
        unmanaged = {"name": "unmanaged", "source": {"source": "local", "path": "./other"}}
        managed = {
            "name": self.spec.name,
            "source": {},
            "category": "Research",
            "policy": {"installation": "INSTALLED_BY_DEFAULT"},
        }
        path.write_text(json.dumps({"name": "custom", "plugins": [unmanaged, managed]}))

        write_marketplaces(self.root, [self.spec])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "custom")
        self.assertEqual(data["plugins"][0], unmanaged)
        self.assertEqual(data["plugins"][1]["category"], "Research")
        self.assertEqual(data["plugins"][1]["source"]["path"], "./plugins/split-plugin/codex")
        self.assertEqual(
            data["plugins"][1]["policy"],
            {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"},
        )
        before = path.read_bytes()
        write_marketplaces(self.root, [self.spec])
        self.assertEqual(path.read_bytes(), before)

        updated = replace(
            self.spec, manifest_fields={"codex": {"interface": {"category": "Education & Research"}}},
        )
        write_marketplaces(self.root, [updated])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["plugins"][1]["category"], "Education & Research")

    def test_regeneration_updates_metadata_from_recipe(self):
        relative = ".claude-plugin/marketplace.json"
        path = self.root / relative
        write_marketplaces(self.root, [replace(self.spec, metadata={"description": "Before"})])
        write_marketplaces(self.root, [replace(
            self.spec, metadata={"description": "After"},
            manifest_fields={"claude": {"description": "Claude summary"}},
        )])
        [entry] = json.loads(path.read_text(encoding="utf-8"))["plugins"]
        self.assertEqual(entry["description"], "Claude summary")
        copilot = json.loads((self.root / MARKETPLACE_PATHS["copilot"]).read_text(encoding="utf-8"))
        self.assertEqual(copilot["plugins"][0]["description"], "After")

    def test_ambiguous_manifest_roots_are_rejected_before_writing(self):
        spec = PluginSpec(
            "ambiguous",
            "local-source",
            manifests={
                ".claude-plugin/plugin.json": "template",
                "nested/.claude-plugin/plugin.json": "template",
            },
        )
        with self.assertRaisesRegex(SyncError, "multiple manifests"):
            write_marketplaces(self.root, [self.spec, spec])
        self.assertEqual(list(self.root.iterdir()), [])
