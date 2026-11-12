import io
import os
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from plugins_manager import PluginSpec, SyncError, Tree, publish
from plugins_manager.cli import main
from plugins_manager.manifests import PLATFORM_PATHS
from plugins_manager.marketplace import write_marketplaces
from plugins_manager.utils.git import git
from plugins_manager.utils.locks import plugin_lock


class PublisherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="test-publish-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.output = root / "output"
        self.output.mkdir()
        self.remote = root / "remote.git"
        self.remote.mkdir()
        self.seed = root / "seed"
        self.seed.mkdir()

        env = patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Publisher Test",
            "GIT_AUTHOR_EMAIL": "publisher@example.com",
            "GIT_COMMITTER_NAME": "Publisher Test",
            "GIT_COMMITTER_EMAIL": "publisher@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        git("init", "--bare", "--initial-branch=main", cwd=self.remote)

        tree = Tree(self.output)
        for paths in PLATFORM_PATHS.values():
            tree.write_text(f"plugins/example/{paths.manifest}", '{"name":"example"}\n')
        tree.write_text("plugins/example/.gitignore", ".env\n")
        tree.write_text("plugins/example/.env", "ignored local data\n")
        tree.write_text("plugins/example/asset.txt", "runtime asset\n")
        tree.write_text("plugins/unlisted/info.txt", "local only\n")
        tree.write_text("configs/plugins/example.toml", "local recipe\n")
        write_marketplaces(self.output, [PluginSpec("example", "source")])

    def seed_main(self):
        git("init", "--initial-branch=main", cwd=self.seed)
        tree = Tree(self.seed)
        tree.write_text("README.md", "Preserve me\n")
        tree.write_text("plugins/example/stale.txt", "remove me\n")
        tree.write_text("plugins/other/notes.txt", "keep me\n")
        git("add", ".", cwd=self.seed)
        git("commit", "-m", "Seed main", cwd=self.seed)
        git("push", str(self.remote), "main", cwd=self.seed)
        return git("rev-parse", "HEAD", cwd=self.seed)

    def publish_to(self, branch="main", **kwargs):
        return publish(self.output, str(self.remote), branch, **kwargs)

    def remote_git(self, *args):
        return git(*args, cwd=self.remote)

    def test_publish_replaces_managed_paths_and_is_idempotent(self):
        base = self.seed_main()
        result = self.publish_to(message="Publish example")
        self.assertTrue(result.pushed)
        self.assertGreater(result.changed_files, 0)
        self.assertEqual(result.commit, self.remote_git("rev-parse", "main"))
        self.assertEqual(self.remote_git("rev-parse", "main^"), base)
        self.assertEqual(self.remote_git("log", "-1", "--format=%s", "main"), "Publish example")

        files = self.remote_git("ls-tree", "-r", "--name-only", "main").splitlines()
        for paths in PLATFORM_PATHS.values():
            self.assertIn(paths.marketplace, files)
            self.assertIn(f"plugins/example/{paths.manifest}", files)
        self.assertIn("plugins/example/asset.txt", files)
        self.assertNotIn("plugins/example/.env", files)
        self.assertIn("plugins/other/notes.txt", files)
        self.assertIn("README.md", files)
        self.assertNotIn("plugins/example/stale.txt", files)
        self.assertNotIn("plugins/unlisted/info.txt", files)
        self.assertNotIn("configs/plugins/example.toml", files)

        repeated = self.publish_to()
        self.assertEqual(repeated.commit, result.commit)
        self.assertEqual(repeated.changed_files, 0)
        self.assertFalse(repeated.pushed)
        self.assertFalse((self.output / ".marketplace.lock").exists())
        self.assertFalse((self.output / ".tmp").exists())

    def test_creates_named_branch_from_default(self):
        base = self.seed_main()
        result = self.publish_to("releases/marketplace")
        self.assertTrue(result.pushed)
        self.assertEqual(self.remote_git("rev-parse", "releases/marketplace^"), base)
        self.assertEqual(self.remote_git("rev-parse", "main"), base)

    def test_cli_dry_run_and_first_publish_to_empty_remote(self):
        args = [
            "publish", "--output", str(self.output),
            "--repo", str(self.remote), "--branch", "marketplace",
        ]
        console = io.StringIO()
        with redirect_stdout(console):
            self.assertEqual(main([*args, "--dry-run"]), 0)
        self.assertIn("Prepared:", console.getvalue())
        self.assertEqual(self.remote_git("for-each-ref"), "")
        self.assertFalse((self.output / ".tmp").exists())

        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertEqual(self.remote_git("rev-list", "--count", "marketplace"), "1")

    def test_generate_update_and_publish_share_default_output(self):
        root = self.output.parent
        configs = root / "external-resources"
        config = configs / "plugins/example.toml"
        config.parent.mkdir(parents=True)
        recipe = '[plugins.example]\nrepo = "../seed"\n'
        config.write_text(recipe, encoding="utf-8")
        git("init", "--initial-branch=main", cwd=self.seed)
        upstream = Tree(self.seed)
        upstream.write_text("SKILL.md", "# Original\n")
        git("add", ".", cwd=self.seed)
        git("commit", "-m", "Upstream", cwd=self.seed)

        output = root / "research-marketplace"
        local = ["--configs", str(configs)]
        remote = [
            "publish",
            "--repo", str(self.remote), "--branch", "marketplace",
        ]
        with chdir(root), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main([*local, *remote])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(main(remote), 1)
            self.assertEqual(main([*local, "generate", "--dry-run"]), 0)
            self.assertFalse(output.exists())

            self.assertEqual(main(local), 0)
            self.assertIn("usage: plugins-manager", stdout.getvalue())
            self.assertFalse(output.exists())
            self.assertEqual(main([*local, "generate"]), 0)
            self.assertEqual(self.remote_git("for-each-ref"), "")
            payload = output / "plugins/example/SKILL.md"
            self.assertEqual(payload.read_text(encoding="utf-8"), "# Original\n")
            for paths in PLATFORM_PATHS.values():
                self.assertTrue((output / paths.marketplace).is_file())

            upstream.write_text("SKILL.md", "# Updated\n")
            git("commit", "-am", "New upstream", cwd=self.seed)
            config.write_text("invalid config", encoding="utf-8")
            self.assertEqual(main(remote), 0)
            self.assertEqual(self.remote_git("show", "marketplace:plugins/example/SKILL.md"), "# Original")

            config.write_text(recipe, encoding="utf-8")
            self.assertEqual(main([*local, "update"]), 0)
            self.assertEqual(payload.read_text(encoding="utf-8"), "# Updated\n")
            self.assertEqual(self.remote_git("show", "marketplace:plugins/example/SKILL.md"), "# Original")

            self.assertEqual(main([*remote, "--message", "Release"]), 0)
        self.assertEqual(self.remote_git("show", "marketplace:plugins/example/SKILL.md"), "# Updated")
        self.assertEqual(self.remote_git("log", "-1", "--format=%s", "marketplace"), "Release")
        self.assertFalse((output / ".tmp").exists())

    def test_rejects_incomplete_output_without_push(self):
        missing = "plugins/example/.codex-plugin/plugin.json"
        (self.output / missing).unlink()
        with self.assertRaisesRegex(SyncError, r"plugin\.json"):
            self.publish_to()
        self.assertEqual(self.remote_git("for-each-ref"), "")
        self.assertFalse((self.output / ".marketplace.lock").exists())
        self.assertFalse((self.output / ".tmp").exists())

    def test_rejects_a_required_manifest_ignored_by_git(self):
        Tree(self.output).write_text("plugins/example/.gitignore", "/plugin.json\n")
        with self.assertRaisesRegex(SyncError, "Required files excluded by Git: plugins/example/plugin.json"):
            self.publish_to()
        self.assertEqual(self.remote_git("for-each-ref"), "")

    def test_active_plugin_update_prevents_publishing(self):
        with plugin_lock(self.output, "example"):
            with self.assertRaisesRegex(SyncError, "Update locked for example"):
                self.publish_to()
            self.assertEqual(self.remote_git("for-each-ref"), "")
            self.assertFalse((self.output / ".marketplace.lock").exists())
        self.assertFalse((self.output / ".tmp").exists())

    def test_push_rejects_concurrent_remote_commit(self):
        self.seed_main()

        def advance_before_push(*args, cwd):
            if args[0] == "push":
                Tree(self.seed).write_text("README.md", "Concurrent change\n")
                git("commit", "-am", "Concurrent update", cwd=self.seed)
                git("push", str(self.remote), "main", cwd=self.seed)
            return git(*args, cwd=cwd)

        with patch("plugins_manager.publisher.git", side_effect=advance_before_push):
            with self.assertRaisesRegex(SyncError, "rejected"):
                self.publish_to()
        self.assertEqual(self.remote_git("show", "main:README.md"), "Concurrent change")
