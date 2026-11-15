import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from plugins_manager import Operation, PlatformSpec, PluginSpec, SyncError, Tree, Updater, load_specs
from plugins_manager.cli import main
from plugins_manager.manifest import PLATFORM_PATHS
from plugins_manager.utils.git import checkout
from plugins_manager.utils.locks import plugin_lock


PROJECT = Path(__file__).resolve().parents[1]
DEFAULTS = PROJECT / "plugins_manager/defaults"


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "local upstream"
        self.source.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "commit.gpgSign", "false")
        self.git("config", "tag.gpgSign", "false")
        self.git("config", "core.autocrlf", "false")
        self.updater = Updater(self.root)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.source, text=True).strip()

    def commit(self, files):
        for relative, content in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def spec(self, **options):
        return PluginSpec("fixture", self.source.name, **options)

    def test_monorepo_merge_and_preserve(self):
        commit = self.commit({
            "components/example/.codex-plugin/plugin.json":
                '{"name":"upstream","author":{"name":"Original","url":"https://example.test"},'
                '"skills":"./skills"}',
            "components/example/.claude-plugin/plugin.json": '{"name":"fixture"}\n',
            "components/example/skills/demo/SKILL.md": "# Demo",
            "components/example/.gitignore": "upstream-only\n",
            "outside.txt": "not imported",
        })
        self.git("tag", "-a", "v1.2.0", "-m", "Release")
        spec = self.spec(
            source_dir="components/example",
            ref="refs/tags/v1.2.0",
            platforms={
                "codex": PlatformSpec("codex", mode="merge"),
                "claude": PlatformSpec("claude", mode="preserve"),
            },
            metadata={"author": {"name": "New"}},
            required=("skills/demo/SKILL.md",),
            gitignore="keep",
        )
        result = self.updater.update(spec)
        self.assertEqual((result.commit, result.version), (commit, commit[:7]))
        dest = result.destination
        self.assertFalse((dest / "outside.txt").exists())
        self.assertFalse((dest / ".git").exists())
        self.assertEqual((dest / ".gitignore").read_text(), "upstream-only\n")
        self.assertEqual((dest / ".claude-plugin/plugin.json").read_text(), '{"name":"fixture"}\n')
        codex = json.loads((dest / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(codex["version"], commit[:7])
        self.assertEqual(codex["author"], {"name": "New", "url": "https://example.test"})
        self.assertEqual(codex["skills"], "./skills")

    def test_dry_run_and_failures_keep_previous_tree(self):
        self.commit({"payload": "old tagged commit"})
        self.git("tag", "v2.0")
        commit = self.commit({"payload": "new"})
        spec = self.spec(
            layout="selective",
            gitignore="keep",
            required=("payload",),
            operations=(Operation.parse({"action": "copy", "from": "payload", "to": "payload"}),),
        )
        destination = self.root / "plugins/fixture"
        destination.mkdir(parents=True)
        old = destination / "old"
        old.write_text("previous")
        result = self.updater.update(spec, dry_run=True)
        self.assertFalse(result.published)
        self.assertEqual(result.version, commit[:7])
        self.assertTrue(old.exists())

        with self.assertRaisesRegex(SyncError, "Expected path missing"):
            self.updater.update(replace(spec, required=("missing",)))
        self.assertTrue(old.exists())

        rename = Path.rename

        def reject_stage(path, target):
            if path.name == "stage":
                raise PermissionError("simulated publish failure")
            return rename(path, target)

        with patch.object(Path, "rename", reject_stage):
            with self.assertRaisesRegex(PermissionError, "simulated publish failure"):
                self.updater.update(spec)
        self.assertEqual(old.read_text(), "previous")
        self.assertFalse((self.root / ".tmp").exists())

        def adapt(source, stage):
            stage.copy_from(source, "payload", "references/payload")

        self.updater.update(spec, transform=adapt)
        self.assertFalse(old.exists())
        self.assertEqual((destination / "payload").read_text(), "new")
        self.assertEqual((destination / "references/payload").read_text(), "new")

    def test_generate_selects_marketplace_types_and_update_reuses_them(self):
        self.commit({"SKILL.md": "fixture"})
        configs = self.root / "configs"
        config = configs / "plugins/recipes.toml"
        config.parent.mkdir(parents=True)

        def recipe(name):
            settings = (
                f'[plugins.{name}]\nrepo = "../local upstream"\n'
                f'[plugins.{name}.rules]\ngitignore = "keep"\n'
                'operations = [{ action = "mkdir", path = "extra" }]\n'
            )
            return settings + "".join(
                f"[plugins.{name}.metadata.{kind}]\n" for kind in PLATFORM_PATHS
            )

        config.write_text(
            recipe("first") + recipe("second") + recipe("third"),
            encoding="utf-8",
        )
        command = ["--configs", str(configs)]
        codex_root = self.root / "codex/research-marketplace"
        copilot_root = self.root / "copilot/research-marketplace"
        claude_root = self.root / "claude/research-marketplace"
        codex = codex_root / ".agents/plugins/marketplace.json"
        copilot = copilot_root / ".github/plugin/marketplace.json"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main([*command, "generate", "first", "--exclude", "first"]), 0)
            self.assertFalse(codex_root.exists())
            with (
                patch("plugins_manager.updater.checkout", wraps=checkout) as fetch,
                patch.object(Operation, "apply", autospec=True, side_effect=Operation.apply) as apply,
            ):
                self.assertEqual(
                    main([
                        *command, "generate", "first", "second", "--exclude", "second",
                        "--type", "codex", "--type", "copilot",
                    ]),
                    0,
                )
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(apply.call_count, 1)
            for output, kind in ((codex_root, "codex"), (copilot_root, "copilot")):
                [plugin] = (output / "plugins").iterdir()
                self.assertEqual(plugin.name, "first")
                for platform, paths in PLATFORM_PATHS.items():
                    self.assertEqual((plugin / paths.manifest).exists(), platform == kind)
            for marketplace_file in (codex, copilot):
                entries = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
                self.assertEqual([entry["name"] for entry in entries], ["first"])
            self.assertFalse(claude_root.exists())

            config.write_text(recipe("first") + recipe("second"), encoding="utf-8")
            self.assertEqual(main([*command, "update", "--type", "claude", "first"]), 1)
            with (
                plugin_lock(codex_root, "first"),
                patch("plugins_manager.updater.checkout", wraps=checkout) as fetch,
            ):
                self.assertEqual(main([*command, "update", "--type", "codex", "first"]), 1)
                fetch.assert_not_called()
            self.assertEqual(main([*command, "update", "--exclude", "second"]), 0)
            self.assertFalse((codex_root / "plugins/second").exists())
            self.assertFalse((copilot_root / "plugins/second").exists())
            for marketplace_file in (codex, copilot):
                entries = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
                self.assertEqual([entry["name"] for entry in entries], ["first"])
            self.assertEqual(main([*command, "update", "first"]), 0)
            for marketplace_file in (codex, copilot):
                entries = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
                self.assertEqual([entry["name"] for entry in entries], ["first"])
            self.assertEqual(main([*command, "update", "second", "--type", "codex"]), 0)
            self.assertFalse((copilot_root / "plugins/second").exists())
            self.assertEqual(main([*command, "update", "second", "--type", "copilot"]), 0)

        self.assertFalse(claude_root.exists())
        for marketplace_file in (codex, copilot):
            entries = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
            self.assertEqual([entry["name"] for entry in entries], ["first", "second"])

    def test_batch_stops_at_first_failed_plugin_and_keeps_existing_marketplace(self):
        self.commit({"SKILL.md": "fixture"})
        configs = self.root / "configs"
        config = configs / "plugins/recipes.toml"
        config.parent.mkdir(parents=True)

        def recipe(name, required=""):
            return (
                f'[plugins.{name}]\nrepo = "../local upstream"\n'
                f'[plugins.{name}.rules]\ngitignore = "keep"\n{required}'
                f'[plugins.{name}.metadata.codex.marketplace]\nenabled = false\n'
            )

        config.write_text(
            recipe("first") + recipe("broken", 'required = ["missing.txt"]\n') + recipe("later"),
            encoding="utf-8",
        )
        output = self.root / "codex/research-marketplace"
        marketplace_file = output / ".agents/plugins/marketplace.json"
        marketplace_file.parent.mkdir(parents=True)
        original = b'{"name":"existing","plugins":[]}\n'
        marketplace_file.write_bytes(original)

        command = ["--configs", str(configs), "update"]
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            status = main(command)

        self.assertEqual(status, 1)
        self.assertIn("Plugin 'broken' failed: codex: Expected path missing", stderr.getvalue())
        self.assertNotIn("later: staging", stdout.getvalue())
        self.assertTrue((output / "plugins/first/SKILL.md").is_file())
        self.assertFalse((output / "plugins/broken").exists())
        self.assertFalse((output / "plugins/later").exists())
        self.assertEqual(marketplace_file.read_bytes(), original)

        self.commit({"SKILL.md": "new fixture"})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                main([*command, "--exclude", "unknown"])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("unknown plugin(s): unknown", stderr.getvalue())
            self.assertEqual(main([*command, "--exclude", "first", "broken"]), 0)

        self.assertEqual((output / "plugins/first/SKILL.md").read_text(encoding="utf-8"), "fixture")
        self.assertEqual((output / "plugins/later/SKILL.md").read_text(encoding="utf-8"), "new fixture")
        self.assertFalse((output / "plugins/broken").exists())

    def test_update_cli_writes_plugins_and_marketplaces_to_separate_output(self):
        self.commit({".codex-plugin/plugin.json": '{"name":"upstream"}', "SKILL.md": "fixture"})
        configs = self.root / "configs"
        config = configs / "plugins/fixture.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[plugins.fixture]\nrepo = "../local upstream"\n'
            '[plugins.fixture.rules]\nicon = "branding/icon.png"\n'
            '[plugins.fixture.metadata.codex.manifest]\nmode = "merge"\n',
            encoding="utf-8",
        )
        icon = configs / "branding/icon.png"
        icon.parent.mkdir()
        icon.write_bytes((PROJECT / "configs/icons/zotero-cli.png").read_bytes())
        template = configs / "template/.gitignore"
        template.parent.mkdir()
        template.write_text("local-rule\n", encoding="utf-8")
        base = self.root / "export" / "marketplace"
        output = base / "codex/research-marketplace"
        command = ["--configs", str(configs)]
        update = [*command, "update", "--output", str(base), "fixture"]

        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(update), 1)
            self.assertFalse(base.exists())
            base.mkdir(parents=True)
            self.assertEqual(main([*update, "--dry-run"]), 1)
            self.assertIn("Run generate first", stderr.getvalue())
            self.assertNotIn("staging", stdout.getvalue())
            self.assertEqual(list(base.iterdir()), [])

            self.assertEqual(main([*command, "generate", "--output", str(base)]), 0)
            self.commit({"SKILL.md": "updated fixture"})
            self.assertEqual(main(update), 0)

        plugin = output / "plugins/fixture"
        self.assertEqual((plugin / "SKILL.md").read_text(encoding="utf-8"), "updated fixture")
        self.assertEqual((plugin / "assets/icon.png").read_bytes(), icon.read_bytes())
        self.assertEqual((plugin / ".gitignore").read_bytes(), template.read_bytes())
        marketplace_file = output / ".agents/plugins/marketplace.json"
        [entry] = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
        self.assertEqual(entry["source"]["path"], "./plugins/fixture")
        self.assertFalse((output / ".tmp").exists())
        for relative in ("plugins", ".agents", ".tmp"):
            self.assertFalse((self.root / relative).exists())

    def test_cli_uses_metadata_version_and_reverts_to_short_commit(self):
        commit = self.commit({"SKILL.md": "fixture"})
        self.git("tag", "v1.0.0")
        configs = self.root / "configs"
        config = configs / "plugins/fixture.toml"
        config.parent.mkdir(parents=True)
        recipe = (
            '[plugins.fixture]\nrepo = "../local upstream"\n'
            'ref = "refs/tags/v1.0.0"\n'
        )
        output = self.root / "all/research-marketplace"
        command = ["--configs", str(configs)]
        plugin = output / "plugins/fixture"
        for action, metadata, version in (
            ("generate", '[plugins.fixture.metadata]\nversion = "preview-1"\n', "preview-1"),
            ("update", "", commit[:7]),
        ):
            platforms = "".join(f"[plugins.fixture.metadata.{kind}]\n" for kind in PLATFORM_PATHS)
            config.write_text(recipe + metadata + platforms, encoding="utf-8")
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                self.assertEqual(main([*command, action, "--combined", "fixture"]), 0)
            self.assertIn(f" {version} ({commit[:12]})", stdout.getvalue())
            for kind, paths in PLATFORM_PATHS.items():
                manifest = json.loads((plugin / paths.manifest).read_text(encoding="utf-8"))
                self.assertEqual(manifest["version"], version)
                [entry] = json.loads(
                    (output / paths.marketplace).read_text(encoding="utf-8")
                )["plugins"]
                if kind == "codex":
                    self.assertNotIn("version", entry)
                else:
                    self.assertEqual(entry["version"], version)

    def test_platform_overrides_render_native_desktop_catalog_fields(self):
        self.commit({"SKILL.md": "fixture"})
        configs = self.root / "configs"
        config = configs / "plugins/fixture.toml"
        config.parent.mkdir(parents=True)
        recipe = """
[plugins.fixture]
repo = "../local upstream"

[plugins.fixture.metadata]
version = "shared"
description = "Shared summary"
author = { name = "Lab", url = "https://example.test" }
homepage = "https://example.test/plugin"
repository = "https://example.test/source"
license = "MIT"
keywords = ["shared"]

[plugins.fixture.metadata.claude]
version = "claude-default"
description = "Claude common summary"
displayName = "Claude Research"
keywords = ["claude"]
author = { name = "Claude team" }

[plugins.fixture.metadata.claude.manifest]
version = "claude-release"
displayName = "Claude Native Name"
description = "Claude native summary"

[plugins.fixture.metadata.claude.marketplace]
description = "Claude catalog summary"
relevance = { topic = "Papers", signals = { filesRead = ["**/*.bib"] } }

[plugins.fixture.metadata.copilot.marketplace]
category = "Research"
tags = ["references"]
strict = false

[plugins.fixture.metadata.copilot.manifest]
schema = "1.1.0"

[plugins.fixture.metadata.codex]
category = "Common research"

[plugins.fixture.metadata.codex.manifest]
interface.displayName = "Codex Research"
interface.shortDescription = "Find and review papers"
interface.longDescription = "Review citations and academic papers."
interface.defaultPrompt = ["Check these references"]
interface.capabilities = ["Interactive"]
interface.category = "Native research"

[plugins.fixture.metadata.codex.marketplace]
category = "Catalog research"
policy = { installation = "INSTALLED_BY_DEFAULT" }
"""
        config.write_text(recipe, encoding="utf-8")
        command = ["--configs", str(configs)]
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main([*command, "generate", "--combined"]), 0)
        self.assertIn("claude version: claude-release", stdout.getvalue())
        output = self.root / "all/research-marketplace"
        plugin = output / "plugins/fixture"

        def documents(kind):
            paths = PLATFORM_PATHS[kind]
            manifest = json.loads((plugin / paths.manifest).read_text(encoding="utf-8"))
            [entry] = json.loads((output / paths.marketplace).read_text(encoding="utf-8"))["plugins"]
            return manifest, entry

        claude, claude_entry = documents("claude")
        self.assertEqual(claude["description"], "Claude native summary")
        self.assertEqual(claude["displayName"], "Claude Native Name")
        self.assertEqual(claude_entry["displayName"], "Claude Research")
        self.assertEqual(claude_entry["description"], "Claude catalog summary")
        self.assertEqual(claude_entry["author"], {"name": "Claude team", "url": "https://example.test"})
        self.assertEqual(claude_entry["keywords"], ["claude"])
        self.assertEqual(claude_entry["version"], "claude-release")
        self.assertEqual(claude_entry["relevance"]["topic"], "Papers")

        copilot, copilot_entry = documents("copilot")
        self.assertIn("/1.1.0/", copilot["$schema"])
        self.assertEqual(copilot_entry["repository"], "https://example.test/source")
        self.assertEqual(copilot_entry["license"], "MIT")
        self.assertEqual(copilot_entry["keywords"], ["shared"])
        self.assertEqual(copilot_entry["tags"], ["references"])
        self.assertFalse(copilot_entry["strict"])
        self.assertNotIn("displayName", copilot_entry)

        codex, codex_entry = documents("codex")
        self.assertEqual(codex["interface"]["displayName"], "Codex Research")
        self.assertEqual(codex["interface"]["category"], "Native research")
        self.assertEqual(copilot["extensions"]["com.openai"]["interface"], codex["interface"])
        self.assertEqual(codex_entry["policy"]["installation"], "INSTALLED_BY_DEFAULT")
        self.assertEqual(codex_entry["category"], "Catalog research")
        self.assertNotIn("description", codex_entry)

        config.write_text(recipe.split("[plugins.fixture.metadata.copilot.marketplace]")[0], encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([*command, "update", "--combined", "fixture"]), 0)
        for kind in ("codex", "copilot"):
            paths = PLATFORM_PATHS[kind]
            self.assertFalse((plugin / paths.manifest).exists())
            self.assertEqual(
                json.loads((output / paths.marketplace).read_text(encoding="utf-8"))["plugins"], []
            )

    def test_marketplace_write_failure_restores_existing_plugin_and_catalogs(self):
        self.commit({"SKILL.md": "old"})
        configs = self.root / "configs"
        config = configs / "plugins/fixture.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[plugins.fixture]\nrepo = "../local upstream"\n'
            '[plugins.fixture.metadata.claude]\n[plugins.fixture.metadata.copilot]\n',
            encoding="utf-8",
        )
        command = ["--configs", str(configs)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([*command, "generate", "--combined"]), 0)
        output = self.root / "all/research-marketplace"
        files = [output / "plugins/fixture/SKILL.md"]
        files.extend(output / PLATFORM_PATHS[kind].marketplace for kind in ("claude", "copilot"))
        before = [path.read_bytes() for path in files]
        self.commit({"SKILL.md": "new"})
        replace_path = Path.replace

        def fail_second_catalog(path, destination):
            if path.suffix == ".json" and destination == files[-1].resolve():
                raise PermissionError("simulated catalog write failure")
            return replace_path(path, destination)

        with patch.object(Path, "replace", fail_second_catalog):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main([*command, "update", "--combined"]), 1)
        self.assertIn("simulated catalog write failure", stderr.getvalue())
        self.assertEqual([path.read_bytes() for path in files], before)

    def test_only_validates_marketplaces_used_by_selected_build(self):
        self.commit({"SKILL.md": "fixture"})
        configs = self.root / "configs"
        config = configs / "plugins/fixture.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[plugins.fixture]\nrepo = "../local upstream"\n'
            '[plugins.fixture.metadata]\n'
            f'description = "{"x" * 1025}"\n'
            '[plugins.fixture.metadata.codex]\n[plugins.fixture.metadata.copilot]\n',
            encoding="utf-8",
        )
        base = self.root / "out"
        command = ["--configs", str(configs), "generate", "fixture", "--output", str(base)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main([*command, "--type", "codex", "--dry-run"]), 0)
            self.assertEqual(main([*command, "--combined", "--type", "codex", "--dry-run"]), 0)
            self.assertEqual(main([*command, "--type", "copilot", "--dry-run"]), 1)
            self.assertIn("Copilot marketplace.description exceeds 1024 characters", stderr.getvalue())
            self.assertFalse(base.exists())
            self.assertEqual(main([*command, "--combined", "--type", "codex"]), 0)
        self.assertTrue((base / "all/research-marketplace/.agents/plugins/marketplace.json").is_file())
        self.assertFalse((base / "all/research-marketplace/.github/plugin/marketplace.json").exists())

    def test_split_build_uses_platform_roots_and_removes_disabled_platform(self):
        self.commit({
            "dist/claude/SKILL.md": "Claude",
            "dist/codex/SKILL.md": "Codex",
            "dist/codex/.github/plugin/plugin.json": '{"name":"legacy"}',
            "LICENSE": "shared",
        })
        configs = self.root / "configs"
        config = configs / "plugins/fixture.toml"
        config.parent.mkdir(parents=True)
        defaults = """
[plugins.fixture]
repo = "../local upstream"

[plugins.fixture.rules]
required = ["dist/claude/SKILL.md", "dist/codex/SKILL.md"]

[plugins.fixture.metadata.claude.manifest]
root = "dist/claude"
"""
        codex_target = """
[plugins.fixture.metadata.codex.manifest]
root = "dist/codex"
"""
        config.write_text(defaults + codex_target, encoding="utf-8")
        command = ["--configs", str(configs)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([*command, "generate"]), 0)
        claude = self.root / "claude/research-marketplace"
        codex = self.root / "codex/research-marketplace"
        claude_plugin = claude / "plugins/fixture"
        codex_plugin = codex / "plugins/fixture"
        self.assertTrue((claude_plugin / "dist/claude/.claude-plugin/plugin.json").is_file())
        self.assertTrue((codex_plugin / "dist/codex/.codex-plugin/plugin.json").is_file())
        self.assertFalse((claude_plugin / "dist/codex").exists())
        self.assertFalse((codex_plugin / "dist/claude").exists())
        self.assertFalse((codex_plugin / "dist/codex/.github/plugin/plugin.json").exists())
        self.assertTrue((claude_plugin / "LICENSE").is_file())

        config.write_text(defaults.replace(', "dist/codex/SKILL.md"', ""), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([*command, "update"]), 0)
        self.assertFalse(codex_plugin.exists())
        index = codex / PLATFORM_PATHS["codex"].marketplace
        self.assertEqual(json.loads(index.read_text(encoding="utf-8"))["plugins"], [])

    def test_concurrent_update_lock(self):
        lock = self.root / ".tmp/marketplace-sync-fixture.lock"
        lock.mkdir(parents=True)
        with self.assertRaisesRegex(SyncError, "Update locked"):
            self.updater.update(self.spec())
        self.assertTrue(lock.is_dir())
        self.commit({"SKILL.md": "fixture"})
        other = replace(self.spec(gitignore="keep"), name="other")
        self.assertTrue(self.updater.update(other).published)
        self.assertTrue(lock.is_dir())

    def test_update_retries_if_scratch_is_removed_before_lock_is_created(self):
        self.commit({"SKILL.md": "fixture"})
        mkdir = Path.mkdir
        removed = False

        def race(path, *args, **kwargs):
            nonlocal removed
            if path.name == "marketplace-sync-fixture.lock" and not removed:
                path.parent.rmdir()
                removed = True
            return mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", race):
            result = self.updater.update(self.spec(gitignore="keep"))

        self.assertTrue(result.published)
        self.assertTrue(removed)
        self.assertFalse((self.root / ".tmp").exists())

    def test_builtin_layouts(self):
        fixture = {
            ".claude-plugin/plugin.json": "{}",
            ".gitignore": "upstream-ignore",
            "README.md": "README",
            "README.en.md": "README",
            "README_CN.md": "README",
            "LICENSE": "license",
            "LICENSE-COMMERCIAL": "license",
            "scripts/test_smoke.py": "discard",
            "scripts/keep.py": "keep",
            "skills/obsolete": "discard",
            "skills/nature-shared/SKILL.md": "shared",
            "skills/nature-academic-search/README.md": "installer instructions",
            "skills/nature-downloader/README.md": "downloader setup",
            "skills/nature-downloader/tests/test_downloader.py": "discard",
            "skills/nature-figure/assets/figures4papers/THIRD_PARTY_NOTICES.md": "keep",
            "skills/academic-research-suite/SKILL.md": "suite",
            "skills/academic-research-suite/ars/scripts/check_data.py": "discard",
            "dist/claude/skills/paper-spine/SKILL.md": "claude",
            "dist/codex/skills/paper-spine/SKILL.md": "codex",
            "skill/zotero-cli/SKILL.md": "---\nname: zotero-cli\n---\n",
        }
        for name in ("academic-paper-reviewer", "academic-paper", "academic-pipeline", "deep-research", "sr-screener"):
            fixture[f"{name}/SKILL.md"] = name
        self.commit(fixture)
        for name, spec in load_specs().items():
            with self.subTest(name=name):
                result = self.updater.update(replace(spec, repo=self.source.name, ref="HEAD"))
                dest = result.destination
                self.assertFalse((dest / ".git").exists())
                self.assertEqual(result.version, spec.metadata.get("version", result.commit[:7]))
                for target in spec.platforms.values():
                    path = target.manifest
                    data = json.loads((dest / path).read_text(encoding="utf-8"))
                    self.assertEqual(data["name"], name)
                    self.assertEqual(data["version"], result.version)
                    if path.endswith(".codex-plugin/plugin.json"):
                        plugin_root = (dest / path).parents[1]
                        self.assertTrue((plugin_root / data["interface"]["logo"]).is_file())
                        self.assertEqual(data["interface"]["composerIcon"], "./assets/icon.png")
                    elif path.endswith(".claude-plugin/plugin.json"):
                        plugin_root = (dest / path).parents[1]
                        self.assertTrue((plugin_root / data["icon"]).is_file())
                    else:
                        self.assertEqual(data["$schema"],
                                         "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
                        self.assertNotIn("skills", data)
                self.assertEqual((dest / ".gitignore").read_bytes(), (DEFAULTS / "gitignore.txt").read_bytes())

        skills = self.root / "plugins/academic-research-skills"
        self.assertTrue((skills / "scripts/keep.py").is_file())
        self.assertFalse((skills / "scripts/test_smoke.py").exists())
        self.assertFalse((skills / "skills/obsolete").exists())
        self.assertEqual(
            (skills / "skills/sr-screener/SKILL.md").read_text(encoding="utf-8"),
            fixture["sr-screener/SKILL.md"],
        )
        suite = self.root / "plugins/academic-research-suite"
        self.assertTrue((suite / "skills/academic-research-suite/ars/scripts/check_data.py").is_file())
        nature = self.root / "plugins/nature-skills"
        self.assertTrue((nature / "skills/nature-downloader/README.md").is_file())
        self.assertTrue((nature / "skills/nature-figure/assets/figures4papers/THIRD_PARTY_NOTICES.md").is_file())
        self.assertFalse((nature / "skills/nature-downloader/tests").exists())
        self.assertFalse((nature / "README.md").exists())
        spine = self.root / "plugins/paper-spine"
        self.assertFalse((spine / ".codex-plugin").exists())
        self.assertFalse((spine / "assets/icon.png").exists())
        self.assertFalse((spine / "dist").exists())
        zotero = self.root / "plugins/zotero-cli/skills/zotero-cli/SKILL.md"
        self.assertIn("name: zotero-cli\n", zotero.read_text(encoding="utf-8"))

    def test_migrated_suite_and_zotero_layouts(self):
        skill = "---\nname: zotero-cli\n---\n"
        self.commit({
            "plugins/ars-codex/skills/academic-research-suite/SKILL.md": "suite",
            "plugins/ars-codex/skills/academic-research-suite/ars/scripts/check_data.py": "discard",
            "skill/zotero-cli/SKILL.md": skill,
            "README.md": "README",
            "README_CN.md": "README",
            "LICENSE": "license",
            "LICENSE-COMMERCIAL": "license",
        })
        specs = load_specs()
        suite = self.updater.update(replace(specs["academic-research-suite"], repo=self.source.name))
        self.assertFalse((suite.destination / "plugins").exists())
        self.assertTrue((suite.destination / "skills/academic-research-suite/ars/scripts/check_data.py").is_file())
        zotero = self.updater.update(replace(specs["zotero-cli"], repo=self.source.name))
        self.assertEqual((zotero.destination / "skills/zotero-cli/SKILL.md").read_text(), skill)

    def test_suite_prefers_root_skill_when_both_upstream_layouts_exist(self):
        self.commit({
            "skills/academic-research-suite/SKILL.md": "root suite",
            "skills/academic-research-suite/ars/scripts/check_data.py": "discard",
            "skills/another-skill/SKILL.md": "keep",
            "plugins/ars-codex/skills/academic-research-suite/SKILL.md": "nested suite",
        })
        spec = replace(load_specs()["academic-research-suite"], repo=self.source.name)
        result = self.updater.update(spec)
        suite = result.destination
        self.assertEqual((suite / "skills/academic-research-suite/SKILL.md").read_text(), "root suite")
        self.assertTrue((suite / "skills/another-skill/SKILL.md").is_file())
        self.assertFalse((suite / "plugins").exists())
        self.assertTrue((suite / "skills/academic-research-suite/ars/scripts/check_data.py").is_file())

    def test_external_config_resolves_icons_relative_to_recipe(self):
        external_project = self.root / "external"
        config = external_project / "configs/plugins/fixture.toml"
        config.parent.mkdir(parents=True)
        icon = external_project / "configs/icons/fixture.png"
        icon.parent.mkdir()
        icon.write_bytes((PROJECT / "configs/icons/zotero-cli.png").read_bytes())
        config.write_text(
            '[plugins.fixture]\nrepo = "local upstream"\n'
            '[plugins.fixture.rules]\nicon = "config:../icons/fixture.png"\ngitignore = "keep"\n'
            '[plugins.fixture.metadata.claude]\n',
            encoding="utf-8",
        )

        spec = load_specs(root=external_project)["fixture"]
        self.assertEqual(spec, load_specs(config)["fixture"])
        self.assertEqual(Path(spec.icon), icon.resolve())
        self.commit({"SKILL.md": "fixture"})
        result = self.updater.update(spec)
        self.assertEqual((result.destination / "assets/icon.png").read_bytes(), icon.read_bytes())

    def test_recipe_directory_rejects_duplicate_plugin_names(self):
        config_dir = self.root / "recipes"
        config_dir.mkdir()
        first = config_dir / "a.toml"
        second = config_dir / "b.toml"
        first.write_text('[plugins.fixture]\nrepo = "first"\n', encoding="utf-8")
        second.write_text('[plugins.fixture]\nrepo = "second"\n', encoding="utf-8")

        self.assertEqual(load_specs(first)["fixture"].repo, "first")
        with self.assertRaisesRegex(SyncError, r"Duplicate plugin 'fixture'.*a\.toml.*b\.toml"):
            load_specs(config_dir)

    def test_copilot_only_plugin_skips_configured_icon(self):
        self.commit({"SKILL.md": "fixture"})
        spec = self.spec(platforms={"copilot": PlatformSpec("copilot")}, gitignore="keep", icon="missing.png")
        result = self.updater.update(spec)
        self.assertFalse((result.destination / "assets/icon.png").exists())
        manifest = json.loads((result.destination / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("icon", manifest)

    def test_icon_from_local_file_and_http(self):
        self.commit({"SKILL.md": "fixture"})
        png = (PROJECT / "configs/icons/zotero-cli.png").read_bytes()

        class IconHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(png if self.path == "/icon" else b"<html>not an icon</html>")

            def log_message(self, _format, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), IconHandler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                local = self.root / "local.png"
                local.write_bytes(png)
                spec = self.spec(
                    platforms={"claude": PlatformSpec("claude")},
                    gitignore="keep",
                    icon=local.name,
                )
                for value in (local.name, str(local), f"{origin}/icon"):
                    result = self.updater.update(replace(spec, icon=value))
                    icon = result.destination / "assets/icon.png"
                    self.assertEqual(icon.read_bytes(), png)
                with self.assertRaisesRegex(SyncError, "Expected a PNG icon"):
                    self.updater.update(replace(spec, icon=f"{origin}/html"))
                self.assertEqual(icon.read_bytes(), png)
            finally:
                server.shutdown()
                worker.join()

    def test_tree_blocks_escape_and_destination_collision(self):
        tree = Tree(self.root)
        with self.assertRaisesRegex(SyncError, "Invalid relative path"):
            tree.write_text("../outside", "blocked")
        tree.write_text("payload", "existing")
        with self.assertRaisesRegex(SyncError, "Destination already exists"):
            tree.copy_from(tree, "payload", "payload")
        tree.mkdir("folder")
        with self.assertRaisesRegex(SyncError, "Cannot copy folder into itself"):
            tree.copy_from(tree, "folder", "folder/nested")
