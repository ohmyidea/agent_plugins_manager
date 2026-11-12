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

from plugins_manager import Operation, PluginSpec, SyncError, Tree, Updater, load_specs
from plugins_manager.cli import main


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
            "components/example/.claude-plugin/plugin.json": '{"name":"keep"}\n',
            "components/example/skills/demo/SKILL.md": "# Demo",
            "components/example/.gitignore": "upstream-only\n",
            "outside.txt": "not imported",
        })
        self.git("tag", "-a", "v1.2.0", "-m", "Release")
        spec = self.spec(
            source_dir="components/example",
            ref="refs/tags/v1.2.0",
            manifests={
                ".codex-plugin/plugin.json": "merge",
                ".claude-plugin/plugin.json": "preserve",
            },
            metadata={"author": {"name": "New"}},
            required=("skills/demo/SKILL.md",),
            gitignore="keep",
        )
        result = self.updater.update(spec)
        self.assertEqual((result.commit, result.version), (commit, "1.2.0"))
        dest = result.destination
        self.assertFalse((dest / "outside.txt").exists())
        self.assertFalse((dest / ".git").exists())
        self.assertEqual((dest / ".gitignore").read_text(), "upstream-only\n")
        self.assertEqual((dest / ".claude-plugin/plugin.json").read_text(), '{"name":"keep"}\n')
        codex = json.loads((dest / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(codex["author"], {"name": "New", "url": "https://example.test"})
        self.assertEqual(codex["skills"], "./skills")

    def test_dry_run_and_failures_keep_previous_tree(self):
        self.commit({"payload": "old tagged commit"})
        self.git("tag", "v2.0")
        commit = self.commit({"payload": "new"})
        spec = self.spec(
            layout="selective",
            manifests={},
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
        settings = 'repo = "../local upstream"\ngitignore = "keep"\n'
        recipe = f"[plugins.first]\n{settings}"
        config.write_text(
            f"{recipe}[plugins.second]\n{settings}[plugins.third]\n{settings}",
            encoding="utf-8",
        )
        command = ["--configs", str(configs)]
        output = self.root / "research-marketplace"
        codex = output / ".agents/plugins/marketplace.json"
        copilot = output / ".github/plugin/marketplace.json"
        claude = output / ".claude-plugin/marketplace.json"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main([*command, "generate", "first", "--exclude", "first"]), 0)
            self.assertFalse(output.exists())
            self.assertEqual(
                main([
                    *command, "generate", "first", "second", "--exclude", "second",
                    "--type", "codex", "--type", "copilot",
                ]),
                0,
            )
            self.assertEqual([path.name for path in (output / "plugins").iterdir()], ["first"])
            for marketplace_file in (codex, copilot):
                entries = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
                self.assertEqual([entry["name"] for entry in entries], ["first"])
            self.assertFalse(claude.exists())

            config.write_text(f"{recipe}[plugins.second]\n{settings}", encoding="utf-8")
            with self.assertRaises(SystemExit) as error:
                main([*command, "update", "--type", "claude", "first"])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(main([*command, "update", "--exclude", "second"]), 0)
            self.assertFalse((output / "plugins/second").exists())
            for marketplace_file in (codex, copilot):
                entries = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
                self.assertEqual([entry["name"] for entry in entries], ["first"])
            self.assertEqual(main([*command, "update", "first"]), 0)

        self.assertFalse(claude.exists())
        for marketplace_file in (codex, copilot):
            entries = json.loads(marketplace_file.read_text(encoding="utf-8"))["plugins"]
            self.assertEqual([entry["name"] for entry in entries], ["first", "second"])

    def test_batch_stops_at_first_failed_plugin_and_keeps_existing_marketplace(self):
        self.commit({"SKILL.md": "fixture"})
        configs = self.root / "configs"
        config = configs / "plugins/recipes.toml"
        config.parent.mkdir(parents=True)
        settings = 'repo = "../local upstream"\ngitignore = "keep"\nmanifests = {}\n'
        config.write_text(
            f"[plugins.first]\n{settings}"
            f"[plugins.broken]\n{settings}"
            'required = ["missing.txt"]\n'
            f"[plugins.later]\n{settings}",
            encoding="utf-8",
        )
        output = self.root / "research-marketplace"
        marketplace_file = output / ".agents/plugins/marketplace.json"
        marketplace_file.parent.mkdir(parents=True)
        original = b'{"name":"existing","plugins":[]}\n'
        marketplace_file.write_bytes(original)

        command = ["--configs", str(configs), "update"]
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            status = main(command)

        self.assertEqual(status, 1)
        self.assertIn("Plugin 'broken' failed: Expected path missing", stderr.getvalue())
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
            '[plugins.fixture]\nrepo = "../local upstream"\nicon = "branding/icon.png"\n'
            'manifests = {".codex-plugin/plugin.json" = "merge"}\n',
            encoding="utf-8",
        )
        icon = configs / "branding/icon.png"
        icon.parent.mkdir()
        icon.write_bytes((PROJECT / "configs/icons/zotero-cli.png").read_bytes())
        template = configs / "template/.gitignore"
        template.parent.mkdir()
        template.write_text("local-rule\n", encoding="utf-8")
        output = self.root / "export" / "marketplace"
        command = ["--configs", str(configs)]
        update = [*command, "update", "--output", str(output), "fixture"]

        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(update), 1)
            self.assertFalse(output.exists())
            output.mkdir(parents=True)
            self.assertEqual(main([*update, "--dry-run"]), 1)
            self.assertIn("Run generate first", stderr.getvalue())
            self.assertNotIn("staging", stdout.getvalue())
            self.assertEqual(list(output.iterdir()), [])

            self.assertEqual(main([*command, "generate", "--output", str(output)]), 0)
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

    def test_concurrent_update_lock(self):
        lock = self.root / ".tmp/marketplace-sync-fixture.lock"
        lock.mkdir(parents=True)
        with self.assertRaisesRegex(SyncError, "Update locked"):
            self.updater.update(self.spec())
        self.assertTrue(lock.is_dir())
        self.commit({"SKILL.md": "fixture"})
        other = replace(self.spec(manifests={}, gitignore="keep"), name="other")
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
            result = self.updater.update(self.spec(manifests={}, gitignore="keep"))

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
                result = self.updater.update(replace(spec, repo=self.source.name))
                dest = result.destination
                self.assertFalse((dest / ".git").exists())
                for path in spec.manifests:
                    data = json.loads((dest / path).read_text(encoding="utf-8"))
                    self.assertEqual(data["name"], name)
                    version = (result.version if ".claude-plugin/" in path
                               else f"0.0.0+git.{result.commit[:7]}")
                    self.assertEqual(data["version"], version)
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
        self.assertEqual((skills / "sr-screener/SKILL.md").read_bytes(),
                         (skills / "skills/sr-screener/SKILL.md").read_bytes())
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
            'icon = "config:../icons/fixture.png"\ngitignore = "keep"\n'
            'manifests = {".claude-plugin/plugin.json" = "template"}\n',
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
        spec = self.spec(manifests={"plugin.json": "template"}, gitignore="keep", icon="missing.png")
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
                    manifests={".claude-plugin/plugin.json": "template"},
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
