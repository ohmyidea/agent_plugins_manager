"""Publish generated marketplace files to a Git branch."""

import re
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from .manifests import PLATFORM_PATHS
from .utils.git import git
from .utils.json_io import read_json_object
from .utils.locks import plugin_lock
from .utils.tree import SyncError, Tree, delete, exists, path_parts


DEFAULT_COMMIT_MESSAGE = "chore: publish marketplace"


@dataclass(frozen=True)
class PublishResult:
    repo: str
    branch: str
    commit: str | None
    changed_files: int
    pushed: bool


def repository_url(repo: str) -> str:
    if not repo.strip() or repo.startswith("-"):
        raise SyncError("Expected a repository URL or owner/repository")
    slug = repo.removesuffix(".git")
    if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", slug) and slug.split("/")[1] not in {".", ".."}:
        return f"https://github.com/{slug}.git"
    if repo.startswith(("https://", "http://", "ssh://", "git@")):
        if repo.startswith(("https://", "http://")):
            url = urlsplit(repo)
            if url.username is not None or url.password is not None:
                raise SyncError("Use a Git credential helper for HTTPS authentication")
        return repo
    local = Path(repo)
    if local.is_dir():
        return str(local.resolve())
    raise SyncError("Expected a Git repository URL or owner/repository")


def managed_paths(source: Tree) -> tuple[list[str], dict[str, set[str]]]:
    marketplace_paths = []
    plugins: dict[str, set[str]] = {}
    for platform, paths in PLATFORM_PATHS.items():
        if not exists(source.path(paths.marketplace)):
            continue
        marketplace = read_json_object(source.file(paths.marketplace))
        entries = marketplace.get("plugins")
        if not isinstance(entries, list):
            raise SyncError(f"Expected a plugins array in {paths.marketplace}")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise SyncError(f"Invalid plugin entry in {paths.marketplace}")
            origin = entry.get("source")
            if isinstance(origin, dict):
                if origin.get("source") != "local":
                    continue
                origin = origin.get("path")
            elif isinstance(origin, str) and ("://" in origin or origin.startswith("git@")):
                continue
            if not isinstance(origin, str):
                raise SyncError(f"Missing source for plugin {entry['name']!r}")
            relative = origin.removeprefix("./")
            parts = path_parts(relative)
            if len(parts) < 2 or parts[0] != "plugins":
                raise SyncError(f"Local plugin must be under plugins/: {origin}")
            plugins.setdefault(parts[1], set()).add(f"{relative}/{paths.manifest}")
        marketplace_paths.append(paths.marketplace)
    if not marketplace_paths:
        raise SyncError("No marketplace files found in the output directory")
    return marketplace_paths, plugins


def snapshot_output(output: Path, destination: Path) -> tuple[list[str], list[str]]:
    source = Tree(output.resolve())
    lock = source.path(".marketplace.lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise SyncError(f"Marketplace generation or publishing is active: {lock}") from error
    try:
        with ExitStack() as locks:
            marketplace_paths, plugins = managed_paths(source)
            target = Tree(destination)
            names = sorted(plugins)
            for name in names:
                locks.enter_context(plugin_lock(source.root, name))
            paths = [*marketplace_paths, *(f"plugins/{name}" for name in names)]
            manifests = sorted(manifest for group in plugins.values() for manifest in group)
            for manifest in manifests:
                source.file(manifest)
            for name in names:
                plugin = Tree(source.path(f"plugins/{name}"))
                plugin.validate_links()
                if any(plugin.root.rglob(".git")):
                    raise SyncError(f"Nested Git metadata found in plugin {name!r}")
            for relative in paths:
                target.copy_from(source, relative, relative)
            return paths, [*marketplace_paths, *manifests]
    finally:
        lock.rmdir()


def publish(
    output: Path,
    repo: str,
    branch: str,
    *,
    message: str = DEFAULT_COMMIT_MESSAGE,
    dry_run: bool = False,
) -> PublishResult:
    """Snapshot managed files and push a normal commit to the selected branch."""
    remote = repository_url(repo)
    if not message.strip():
        raise SyncError("Commit message must not be empty")
    if not branch or branch.startswith(("refs/", "-")):
        raise SyncError("Expected a branch name such as main or marketplace")

    with TemporaryDirectory(prefix="marketplace-publish-") as directory:
        workspace = Path(directory)
        repository = workspace / "repository"
        repository.mkdir()
        git("init", "--quiet", cwd=repository)
        if git("check-ref-format", "--branch", branch, cwd=repository) != branch:
            raise SyncError("Use an explicit branch name")
        snapshot = workspace / "snapshot"
        snapshot.mkdir()
        paths, required = snapshot_output(output, snapshot)

        git("remote", "add", "--", "origin", remote, cwd=repository)
        ref = f"refs/heads/{branch}"
        existing = git("ls-remote", "--heads", "origin", ref, cwd=repository)
        default = "" if existing else git("ls-remote", "origin", "HEAD", cwd=repository)
        base = None
        if existing or default:
            git("fetch", "--depth=1", "--no-tags", "origin", ref if existing else "HEAD", cwd=repository)
            base = git("rev-parse", "FETCH_HEAD", cwd=repository)
            git("checkout", "--quiet", "-b", branch, "FETCH_HEAD", cwd=repository)
        else:
            git("checkout", "--quiet", "--orphan", branch, cwd=repository)

        source = Tree(snapshot)
        target = Tree(repository)
        for relative in paths:
            destination = target.path(relative)
            if exists(destination):
                delete(destination)
            target.copy_from(source, relative, relative)
        git("--literal-pathspecs", "add", "--all", "--", *paths, cwd=repository)
        indexed = git("--literal-pathspecs", "ls-files", "-z", "--", *required, cwd=repository)
        missing = set(required).difference(indexed.split("\0"))
        if missing:
            raise SyncError(f"Required files excluded by Git: {', '.join(sorted(missing))}")
        changes = git("diff", "--cached", "--name-only", "-z", cwd=repository)
        count = len([path for path in changes.split("\0") if path])
        if dry_run:
            return PublishResult(remote, branch, base, count, False)
        if count:
            git("commit", "--quiet", "-m", message, cwd=repository)
        if count or not existing:
            git("push", "--", "origin", f"HEAD:{ref}", cwd=repository)
            commit = git("rev-parse", "HEAD", cwd=repository)
            return PublishResult(remote, branch, commit, count, True)
        return PublishResult(remote, branch, base, 0, False)
