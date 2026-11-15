"""Stage, validate, and publish plugin trees."""

import shutil
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from importlib.resources import as_file
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from .config import PluginSpec
from .icons import write_icons
from .manifest import template_file, write_manifest
from .marketplace import render_entry
from .utils.git import checkout
from .utils.locks import plugin_lock
from .utils.tree import SyncError, Tree, delete, exists, is_link

Transform = Callable[[Tree, Tree], None]


@dataclass(frozen=True)
class UpdateResult:
    destination: Path
    commit: str
    version: str
    published: bool
    platform_versions: dict[str, str | None]


@dataclass(frozen=True)
class PreparedPlugin:
    root: Path
    commit: str


def write_gitignore(root: Path, mode: str, stage: Tree) -> None:
    if mode == "keep":
        return
    template = template_file(root, ".gitignore", "gitignore.txt")
    target = stage.path(".gitignore")
    if mode == "merge" and exists(target):
        current = stage.file(".gitignore").read_text(encoding="utf-8")
        extra = template.read_text(encoding="utf-8")
        stage.write_text(".gitignore", current.rstrip("\n") + "\n\n" + extra)
    else:
        if exists(target):
            if not is_link(target) and not target.is_file():
                raise SyncError(f"Expected a file: {target}")
            delete(target)
        with as_file(template) as source:
            shutil.copy2(source, target)


def ignore_git(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name.lower() == ".git"}


class Updater:
    def __init__(self, root: Path, *, output: Path | None = None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise SyncError(f"Marketplace directory missing: {self.root}")
        self.output_root = self.root if output is None else Path(output).resolve()

    def update(
        self, spec: PluginSpec, *, dry_run: bool = False, transform: Transform | None = None,
        on_publish: Callable[[], None] | None = None,
        prepared: PreparedPlugin | None = None, exclude_roots: Iterable[str] = (),
        marketplace_types: Iterable[str] = (),
        lock_directory: Path | None = None,
    ) -> UpdateResult:
        destination, scratch = self._destination(spec.name)
        if dry_run:
            lock = nullcontext(None)
        elif lock_directory is not None:
            lock = nullcontext(lock_directory)
        else:
            lock = plugin_lock(self.output_root, spec.name)
        with lock as locked, (
            nullcontext(prepared) if prepared is not None else self.prepare(spec, transform=transform)
        ) as payload:
            backup = locked / "previous" if locked is not None else None
            return self._build(
                spec, payload, scratch if backup is not None else None, destination, backup,
                on_publish, tuple(PurePosixPath(root) for root in exclude_roots), set(marketplace_types),
            )

    def _destination(self, name: str) -> tuple[Path, Path]:
        plugins = self.output_root / "plugins"
        scratch = self.output_root / ".tmp"
        for directory in (self.output_root, plugins, scratch):
            if is_link(directory) or (exists(directory) and not directory.is_dir()):
                raise SyncError(f"Expected a real directory: {directory}")
        return plugins / name, scratch

    @contextmanager
    def prepare(self, spec: PluginSpec, *, transform: Transform | None = None) -> Iterator[PreparedPlugin]:
        """Check out and apply shared operations once for one or more builds."""
        with TemporaryDirectory(prefix="marketplace-source-") as temporary:
            workspace = Path(temporary)
            checkout_dir = workspace / "source"
            repo = spec.repo
            if (self.root / repo).exists():
                repo = str(self.root / repo)
            commit = checkout(repo, spec.ref, checkout_dir)
            source = Tree(checkout_dir)
            if spec.source_dir != ".":
                source = Tree(source.path(spec.source_dir))
            if spec.layout == "full" and not spec.operations and transform is None:
                yield PreparedPlugin(source.root, commit)
                return

            output = workspace / "prepared"
            output.mkdir()
            if spec.layout == "full":
                shutil.copytree(source.root, output, symlinks=True, dirs_exist_ok=True, ignore=ignore_git)
            stage = Tree(output)
            for operation in spec.operations:
                operation.apply(source, stage)
            if transform is not None:
                transform(source, stage)
            yield PreparedPlugin(output, commit)

    def remove(self, name: str, *, on_publish: Callable[[], None]) -> None:
        destination, _ = self._destination(name)
        with plugin_lock(self.output_root, name) as lock:
            self._publish(None, destination, lock / "previous", on_publish)

    def _build(
        self, spec: PluginSpec, prepared: PreparedPlugin, scratch: Path | None, destination: Path,
        backup: Path | None, on_publish: Callable[[], None] | None,
        exclude_roots: tuple[PurePosixPath, ...], marketplace_types: set[str],
    ) -> UpdateResult:
        def ignore(directory: str, names: list[str]) -> set[str]:
            relative = Path(directory).relative_to(prepared.root)
            return ignore_git(directory, names) | {
                name for name in names if PurePosixPath(relative.as_posix(), name) in exclude_roots
            }

        # Stage on the marketplace volume so installation can use an atomic rename.
        with TemporaryDirectory(prefix=f"sync-{spec.name}-", dir=scratch) as stage_temp:
            output = Path(stage_temp) / "stage"
            commit = prepared.commit
            version = spec.configured_version or commit[:7]
            shutil.copytree(prepared.root, output, symlinks=True, ignore=ignore)
            stage = Tree(output)
            write_icons(self.root, spec, stage)
            manifest = write_manifest(self.root, spec, stage, version)
            for kind, manifest in manifest.items():
                if kind in marketplace_types and spec.platforms[kind].listed:
                    render_entry(spec, kind, manifest)
            write_gitignore(self.root, spec.gitignore, stage)
            stage.validate_links()
            for path in spec.required:
                if not any(PurePosixPath(path).is_relative_to(root) for root in exclude_roots):
                    stage.require(path)

            if backup is not None:
                self._publish(output, destination, backup, on_publish)
            versions = {kind: manifest.get("version") for kind, manifest in manifest.items()}
            return UpdateResult(destination, commit, version, backup is not None, versions)

    @staticmethod
    def _publish(
        stage: Path | None, destination: Path, backup: Path, on_publish: Callable[[], None] | None
    ) -> None:
        if stage is not None:
            destination.parent.mkdir(exist_ok=True)
        if is_link(destination) or (exists(destination) and not destination.is_dir()):
            raise SyncError(f"Expected a real plugin directory: {destination}")
        installed = False
        try:
            if destination.exists():
                destination.rename(backup)
            if stage is not None:
                stage.rename(destination)
                installed = True
            if on_publish is not None:
                on_publish()
        except BaseException:
            try:
                if installed and stage is not None:
                    destination.rename(stage)
                if backup.exists() and not destination.exists():
                    backup.rename(destination)
            except OSError as error:
                raise SyncError(f"Could not restore {destination}. Check {backup.parent} for recovery files") from error
            raise
        if backup.exists():
            try:
                delete(backup)
            except OSError as error:
                raise SyncError(f"Plugin published. Could not clean backup at {backup}") from error
