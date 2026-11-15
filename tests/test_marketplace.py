import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from plugins_manager import PlatformSpec, PluginSpec, SyncError, Tree, load_specs
from plugins_manager.marketplace import MARKETPLACE_PATHS, replace_marketplaces, write_marketplaces


CODEX_MARKETPLACE = MARKETPLACE_PATHS["codex"]


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="test-marketplace-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.spec = PluginSpec(
            "split-plugin",
            "local-source",
            platforms={
                "claude": PlatformSpec("claude", root="claude"),
                "codex": PlatformSpec("codex", root="codex"),
                "copilot": PlatformSpec("copilot", root="claude"),
            },
        )
        self.write_fixture(self.spec)

    def write_fixture(self, spec):
        for target in spec.platforms.values():
            Tree(self.root).write_text(
                f"plugins/{spec.name}/{target.manifest}", json.dumps({"name": spec.name})
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
        listed = recipe + "[plugins.fixture.metadata.codex]\n[plugins.fixture.metadata.copilot]\n"
        config.write_text(listed, encoding="utf-8")
        claude = self.root / MARKETPLACE_PATHS["claude"]

        self.write_fixture(load_specs(config)["fixture"])
        write_marketplaces(self.root, load_specs(config).values())
        self.assertFalse(claude.exists())

        initial = PluginSpec("fixture", "local-source", platforms={"claude": PlatformSpec("claude")})
        self.write_fixture(initial)
        write_marketplaces(self.root, [initial], types=["claude"])
        unrelated = {"name": "unmanaged", "source": "./other"}
        marketplace = json.loads(claude.read_text(encoding="utf-8"))
        marketplace["plugins"].append(unrelated)
        claude.write_text(json.dumps(marketplace), encoding="utf-8")

        write_marketplaces(self.root, load_specs(config).values())
        self.assertEqual(json.loads(claude.read_text(encoding="utf-8"))["plugins"], [unrelated])
        for kind in ("codex", "copilot"):
            marketplace = json.loads((self.root / MARKETPLACE_PATHS[kind]).read_text(encoding="utf-8"))
            self.assertEqual([entry["name"] for entry in marketplace["plugins"]], ["fixture"])

        config.write_text(recipe, encoding="utf-8")
        write_marketplaces(self.root, load_specs(config).values())
        for kind in ("codex", "copilot"):
            marketplace = json.loads((self.root / MARKETPLACE_PATHS[kind]).read_text(encoding="utf-8"))
            self.assertEqual(marketplace["plugins"], [])

    def test_recipe_rejects_unknown_platform(self):
        config = self.root / "recipe.toml"
        config.write_text(
            '[plugins.fixture]\nrepo = "local-source"\n[plugins.fixture.metadata.unknown]\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SyncError, "unknown fields.*unknown"):
            load_specs(config)

    def test_recipe_rejects_fields_outside_their_scope(self):
        config = self.root / "recipe.toml"
        for table, assignment, field in (
            ("metadata", 'category = "Research"', "category"),
            ("metadata.codex", 'interface.displayName = "Example"', "interface"),
            ("metadata.codex", 'policy.installation = "AVAILABLE"', "policy"),
            ("metadata.codex", 'version = "1.2.3"', "version"),
            ("metadata.claude", 'userConfig = {}', "userConfig"),
            ("metadata.copilot", 'extensions = {}', "extensions"),
            ("metadata.codex.marketplace", 'interface.displayName = "Example"', "interface"),
            ("metadata.codex.manifest", 'policy.installation = "AVAILABLE"', "policy"),
        ):
            with self.subTest(table=table, field=field):
                config.write_text(
                    f'[plugins.fixture]\nrepo = "source"\n[plugins.fixture.{table}]\n{assignment}\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SyncError, field):
                    load_specs(config)

    def test_existing_marketplace_keeps_unmanaged_entries_and_custom_fields(self):
        path = self.root / CODEX_MARKETPLACE
        path.parent.mkdir(parents=True)
        unmanaged = {"name": "unmanaged", "source": {"source": "local", "path": "./other"}}
        managed = {
            "name": self.spec.name,
            "source": {"source": "local", "path": "./plugins/split-plugin/codex"},
            "category": "Research",
            "custom": "keep",
            "policy": {"installation": "INSTALLED_BY_DEFAULT"},
        }
        path.write_text(json.dumps({"name": "custom", "plugins": [unmanaged, managed]}))

        write_marketplaces(self.root, [self.spec])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "custom")
        self.assertEqual(data["plugins"][0], unmanaged)
        self.assertEqual(data["plugins"][1]["category"], "Productivity")
        self.assertEqual(data["plugins"][1]["custom"], "keep")
        self.assertEqual(data["plugins"][1]["source"]["path"], "./plugins/split-plugin/codex")
        self.assertEqual(
            data["plugins"][1]["policy"],
            {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"},
        )
        before = path.read_bytes()
        write_marketplaces(self.root, [self.spec])
        self.assertEqual(path.read_bytes(), before)

        target = replace(self.spec.platforms["codex"], marketplace={
            "category": "Education & Research", "policy": {"installation": "AVAILABLE"},
        })
        updated = replace(self.spec, platforms={**self.spec.platforms, "codex": target})
        write_marketplaces(self.root, [updated])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["plugins"][1]["category"], "Education & Research")
        self.assertEqual(data["plugins"][1]["policy"]["installation"], "AVAILABLE")

    def test_regeneration_updates_metadata_from_recipe(self):
        relative = ".claude-plugin/marketplace.json"
        path = self.root / relative
        write_marketplaces(self.root, [self.spec])
        target = replace(self.spec.platforms["claude"], marketplace={"description": "Claude summary"})
        copilot = self.spec.platforms["copilot"]
        Tree(self.root).write_text(
            f"plugins/{self.spec.name}/{copilot.manifest}",
            '{"name": "split-plugin", "description": "Packaged description"}',
        )
        write_marketplaces(self.root, [replace(
            self.spec, platforms={**self.spec.platforms, "claude": target},
        )])
        [entry] = json.loads(path.read_text(encoding="utf-8"))["plugins"]
        self.assertEqual(entry["description"], "Claude summary")
        copilot = json.loads((self.root / MARKETPLACE_PATHS["copilot"]).read_text(encoding="utf-8"))
        self.assertEqual(copilot["plugins"][0]["description"], "Packaged description")

    def test_marketplace_versions_follow_existing_split_manifest(self):
        spec = replace(self.spec, metadata={"version": "next-release"})
        plugin = self.root / "plugins" / spec.name
        claude = plugin / "claude/.claude-plugin/plugin.json"
        claude.write_text('{"version": "preserved-release"}', encoding="utf-8")
        (plugin / "claude/plugin.json").write_text('{"version": "abcdef1"}', encoding="utf-8")

        write_marketplaces(self.root, [spec])

        for kind, expected in (("claude", "preserved-release"), ("copilot", "abcdef1")):
            marketplace = json.loads(
                (self.root / MARKETPLACE_PATHS[kind]).read_text(encoding="utf-8")
            )
            self.assertEqual(marketplace["plugins"][0]["version"], expected)

    def test_unbuilt_plugin_is_not_listed(self):
        spec = PluginSpec(
            "unbuilt",
            "local-source",
            platforms={"claude": PlatformSpec("claude")},
        )
        self.assertEqual(write_marketplaces(self.root, [spec]), [])
        self.assertFalse((self.root / MARKETPLACE_PATHS["claude"]).exists())

    def test_rollback_retains_backup_and_restores_other_catalogs(self):
        files = [self.root / f"market-{index}.json" for index in range(3)]
        for path in files:
            path.write_text("old", encoding="utf-8")
        replace_path = Path.replace

        def fail_write_and_restore(path, destination):
            if path.name in {"2.json", "1.previous"}:
                raise PermissionError(path.name)
            return replace_path(path, destination)

        with patch.object(Path, "replace", fail_write_and_restore):
            with self.assertRaisesRegex(SyncError, "recovery files retained") as error:
                replace_marketplaces(self.root, dict.fromkeys(files, "new"))
        [recovery] = self.root.glob("marketplace-*")
        previous = recovery / "1.previous"
        self.assertEqual(previous.read_text(encoding="utf-8"), "old")
        self.assertIn(str(previous), str(error.exception))
        self.assertIn(str(files[1]), str(error.exception))
        self.assertEqual([path.read_text(encoding="utf-8") for path in files], ["old", "new", "old"])
