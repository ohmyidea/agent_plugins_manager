"""Stage, validate, and publish plugin trees."""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import as_file
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import PluginSpec
from .icons import write_icons
from .manifests import template_file, write_manifests
from .utils.git import checkout, source_version
from .utils.locks import plugin_lock
from .utils.tree import SyncError, Tree, delete, exists, is_link

Transform = Callable[[Tree, Tree], None]


@dataclass(frozen=True)
class UpdateResult:
    destination: Path
    commit: str
    version: str
    published: bool


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
        self, spec: PluginSpec, *, dry_run: bool = False, transform: Transform | None = None
    ) -> UpdateResult:
        plugins = self.output_root / "plugins"
        scratch = self.output_root / ".tmp"
        for directory in (self.output_root, plugins, scratch):
            if is_link(directory) or (exists(directory) and not directory.is_dir()):
                raise SyncError(f"Expected a real directory: {directory}")
        destination = plugins / spec.name
        if dry_run:
            return self._build(spec, None, destination, None, transform)

        with plugin_lock(self.output_root, spec.name) as lock:
            return self._build(spec, scratch, destination, lock / "previous", transform)

    def _build(
        self, spec: PluginSpec, scratch: Path | None, destination: Path, backup: Path | None,
        transform: Transform | None,
    ) -> UpdateResult:
        # Keep Git's checkout in the OS temp directory and stage on the marketplace volume.
        with (
            TemporaryDirectory(prefix="marketplace-source-") as source_temp,
            TemporaryDirectory(prefix=f"sync-{spec.name}-", dir=scratch) as stage_temp,
        ):
            checkout_dir = Path(source_temp) / "source"
            output = Path(stage_temp) / "stage"
            repo = spec.repo
            if (self.root / repo).exists():
                repo = str(self.root / repo)
            commit = checkout(repo, spec.ref, checkout_dir)
            version = spec.version or source_version(repo, commit, checkout_dir)
            source = Tree(checkout_dir)
            if spec.source_dir != ".":
                source = Tree(source.path(spec.source_dir))
            output.mkdir()
            if spec.layout == "full":
                shutil.copytree(
                    source.root, output, symlinks=True, dirs_exist_ok=True, ignore=ignore_git
                )

            stage = Tree(output)
            for operation in spec.operations:
                operation.apply(source, stage)
            if transform is not None:
                transform(source, stage)
            write_icons(self.root, spec, stage)
            write_manifests(self.root, spec, stage, version, commit)
            write_gitignore(self.root, spec.gitignore, stage)
            stage.validate_links()
            for path in spec.required:
                stage.require(path)

            if backup is not None:
                self._publish(output, destination, backup)
            return UpdateResult(destination, commit, version, backup is not None)

    @staticmethod
    def _publish(stage: Path, destination: Path, backup: Path) -> None:
        destination.parent.mkdir(exist_ok=True)
        if is_link(destination) or (exists(destination) and not destination.is_dir()):
            raise SyncError(f"Expected a real plugin directory: {destination}")
        try:
            if destination.exists():
                destination.rename(backup)
            stage.rename(destination)
        except BaseException:
            if backup.exists() and not destination.exists():
                try:
                    backup.rename(destination)
                except OSError as error:
                    raise SyncError(f"Restore failed. Previous plugin is saved at {backup}") from error
            raise
        if backup.exists():
            try:
                delete(backup)
            except OSError as error:
                raise SyncError(f"Plugin published. Could not clean backup at {backup}") from error
