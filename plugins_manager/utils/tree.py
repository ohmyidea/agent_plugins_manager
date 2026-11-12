"""Filesystem operations confined to a checkout or a staging directory."""

import os
import re
import shutil
import stat
from pathlib import Path


WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", re.IGNORECASE)


class SyncError(Exception):
    """An invalid configuration or an update that could not be completed."""


def path_parts(value: str) -> list[str]:
    parts = value.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or any(character in value for character in ("\\", ":", "\0"))
        or any(
            part.lower() == ".git" or part.endswith((".", " "))
            or WINDOWS_DEVICE.fullmatch(part.split(".", 1)[0])
            for part in parts
        )
    ):
        raise SyncError(f"Invalid relative path: {value!r}")
    return parts


def is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def exists(path: Path) -> bool:
    return path.exists() or is_link(path)


def delete(path: Path) -> None:
    if is_link(path) or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


class Tree:
    """Work with slash-separated paths relative to a directory."""

    def __init__(self, root: Path):
        if is_link(root) or not root.is_dir():
            raise SyncError(f"Expected a directory: {root}")
        self.root = root

    def path(self, relative: str) -> Path:
        parts = path_parts(relative)
        parent = self.root
        for part in parts[:-1]:
            parent = parent / part
            if is_link(parent):
                raise SyncError(f"Path crosses a link: {parent}")
        return parent / parts[-1]

    def file(self, relative: str) -> Path:
        path = self.path(relative)
        if is_link(path) or not path.is_file():
            raise SyncError(f"Expected a regular file: {path}")
        return path

    def require(self, relative: str) -> None:
        if not self.path(relative).exists():
            raise SyncError(f"Expected path missing: {self.root / relative}")

    def mkdir(self, relative: str) -> None:
        path = self.path(relative)
        if is_link(path):
            raise SyncError(f"Cannot create a directory over a link: {path}")
        path.mkdir(parents=True, exist_ok=True)

    def remove(self, *patterns: str) -> None:
        for pattern in patterns:
            path_parts(pattern)
            matches = sorted(
                self.root.glob(pattern), key=lambda item: len(item.parts), reverse=True
            )
            for match in matches:
                if match == self.root:
                    continue
                path = self.path(match.relative_to(self.root).as_posix())
                if exists(path):
                    delete(path)

    def _destination(self, relative: str) -> Path:
        path = self.path(relative)
        if exists(path):
            raise SyncError(f"Destination already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def copy_from(
        self, source: "Tree", original: str, target: str, *, optional: bool = False
    ) -> None:
        path = source.path(original)
        if not exists(path):
            if optional:
                return
            raise SyncError(f"Expected source missing: {path}")
        if path in self.path(target).parents:
            raise SyncError(f"Cannot copy {original} into itself: {target}")
        destination = self._destination(target)
        if path.is_dir() and not is_link(path):
            shutil.copytree(path, destination, symlinks=True)
        else:
            shutil.copy2(path, destination, follow_symlinks=False)

    def move(
        self, original: str, target: str, *, optional: bool = False, skip_existing: bool = False
    ) -> None:
        path = self.path(original)
        if not exists(path):
            if optional:
                return
            raise SyncError(f"Expected source missing: {path}")
        destination = self.path(target)
        if skip_existing and exists(destination):
            return
        if path in destination.parents:
            raise SyncError(f"Cannot move {original} into itself: {target}")
        path.rename(self._destination(target))

    def replace_text(
        self, relative: str, old: str, new: str, *, optional: bool = False,
        allow_unchanged: bool = False,
    ) -> None:
        if optional and not exists(self.path(relative)):
            return
        path = self.file(relative)
        with path.open(encoding="utf-8", newline="") as stream:
            content = stream.read()
        if old not in content:
            if allow_unchanged and new in content:
                return
            raise SyncError(f"Text {old!r} missing from {path}")
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(content.replace(old, new))

    def replace_regex(self, patterns: list[str], old: str, new: str) -> None:
        """Rewrite matching UTF-8 files without changing their line endings."""
        matches = set()
        for pattern in patterns:
            path_parts(pattern)
            matches.update(path for path in self.root.glob(pattern) if path.is_file())
        try:
            expression = re.compile(old)
            for match in sorted(matches):
                path = self.file(match.relative_to(self.root).as_posix())
                with path.open(encoding="utf-8", newline="") as stream:
                    original = stream.read()
                updated = expression.sub(new, original)
                if updated != original:
                    with path.open("w", encoding="utf-8", newline="") as stream:
                        stream.write(updated)
        except re.error as error:
            raise SyncError(f"Invalid regex replacement: {error}") from error

    def write_text(self, relative: str, content: str) -> None:
        path = self.path(relative)
        if is_link(path):
            raise SyncError(f"Cannot write through a link: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def validate_links(self) -> None:
        root = self.root.resolve()
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs.copy() + files:
                path = Path(directory) / name
                if not is_link(path):
                    continue
                if name in dirs:
                    dirs.remove(name)
                target = path.readlink()
                try:
                    inside = path.resolve().is_relative_to(root)
                except (RuntimeError, OSError):
                    inside = False
                if target.anchor or not inside:
                    raise SyncError(f"Link must resolve within the plugin: {path} -> {target}")
