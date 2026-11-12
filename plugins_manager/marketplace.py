"""Generate platform files for each marketplace type from plugin recipes."""

import json
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from .config import PluginSpec
from .manifests import DEFAULT_CODEX_CATEGORY, PLATFORM_PATHS, SEMVER, manifest_layout
from .utils.json_io import overlay, read_json_object
from .utils.tree import SyncError, Tree, exists

MARKETPLACE_PATHS = {platform: paths.marketplace for platform, paths in PLATFORM_PATHS.items()}


def plugin_entry(spec: PluginSpec, kind: str) -> dict | None:
    if kind not in spec.marketplaces:
        return None
    roots = set()
    for relative in spec.manifests:
        platform, plugin_root = manifest_layout(relative)
        if platform == kind:
            roots.add(plugin_root)
    if not roots:
        return None
    if len(roots) > 1:
        raise SyncError(f"Plugin {spec.name!r} has multiple manifests for {kind}")

    plugin_root, = roots
    path = "./" + (PurePosixPath("plugins", spec.name) / plugin_root).as_posix()
    metadata = overlay(spec.metadata, spec.manifest_fields.get(kind, {}))
    if kind == "codex":
        entry = {
            "name": spec.name,
            "source": {"source": "local", "path": path},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        }
        interface = metadata.get("interface")
        if isinstance(interface, dict):
            category = interface.get("category")
            if isinstance(category, str) and category.strip():
                entry["category"] = category
        return entry
    entry = {"name": spec.name, "source": path}
    for field in ("description", "author", "homepage"):
        if field in metadata:
            entry[field] = metadata[field]
    if spec.version is not None and (kind != "copilot" or SEMVER.fullmatch(spec.version)):
        entry["version"] = spec.version
    return entry


def merge_entries(
    marketplace: dict, entries: list[dict], path: Path, *, excluded: set[str], codex: bool,
) -> bool:
    current = marketplace.setdefault("plugins", [])
    if not isinstance(current, list):
        raise SyncError(f"Expected a plugins array in {path}")
    by_name = {}
    for entry in current:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise SyncError(f"Expected a named plugin entry in {path}")
        name = entry["name"]
        if name in excluded:
            continue
        if name in by_name:
            raise SyncError(f"Duplicate plugin {name!r} in {path}")
        by_name[name] = entry
    removed = len(by_name) < len(current)
    for entry in entries:
        existing = by_name.setdefault(entry["name"], {})
        for key, value in entry.items():
            if key == "policy" and isinstance(existing.get(key), dict):
                value = overlay(value, existing[key])
            existing[key] = value
        if codex:
            existing.setdefault("category", DEFAULT_CODEX_CATEGORY)
    current[:] = by_name.values()
    return removed


def write_marketplaces(
    root: Path, specs: Iterable[PluginSpec], *, types: Iterable[str] | None = None
) -> list[Path]:
    """Write files for the selected marketplace types, retaining custom entries."""
    selected = set(MARKETPLACE_PATHS if types is None else types)
    unknown = selected - MARKETPLACE_PATHS.keys()
    if unknown:
        raise SyncError(f"Unknown marketplace type(s): {', '.join(sorted(unknown))}")
    tree = Tree(root.resolve())
    specs = tuple(specs)
    name = re.sub(r"[^a-z0-9]+", "-", tree.root.name.lower()).strip("-")
    name = name[:64].rstrip("-") or "plugins-manager"
    title = name.replace("-", " ").title()

    lock = tree.path(".marketplace.lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise SyncError(f"Marketplace generation is locked: {lock}") from error
    try:
        pending = {}
        for kind, relative in MARKETPLACE_PATHS.items():
            if kind not in selected:
                continue
            codex = kind == "codex"
            entries = [
                entry for spec in specs
                if (entry := plugin_entry(spec, kind)) is not None
            ]
            excluded = {spec.name for spec in specs if kind not in spec.marketplaces}
            if not entries and not excluded:
                continue
            path = tree.path(relative)
            has_marketplace = exists(path)
            if not entries and not has_marketplace:
                continue
            marketplace = read_json_object(tree.file(relative)) if has_marketplace else {}
            marketplace.setdefault("name", name)
            if codex:
                marketplace.setdefault("interface", {"displayName": title})
            else:
                marketplace.setdefault("owner", {"name": title})
            removed = merge_entries(marketplace, entries, path, excluded=excluded, codex=codex)
            if not entries and not removed:
                continue
            pending[path] = json.dumps(marketplace, ensure_ascii=False, indent=4, allow_nan=False) + "\n"

        for path, contents in pending.items():
            if path.is_file() and path.read_text(encoding="utf-8") == contents:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="marketplace-", dir=path.parent) as directory:
                draft = Path(directory) / path.name
                draft.write_text(contents, encoding="utf-8", newline="\n")
                draft.replace(path)
        return list(pending)
    finally:
        lock.rmdir()
