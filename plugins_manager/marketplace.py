"""Generate platform files for each marketplace type from plugin recipes."""

import json
import re
import shutil
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp

from .config import PluginSpec
from .manifest import DEFAULT_CODEX_CATEGORY, PLATFORM_PATHS
from .marketplace_fields import CATALOG_FIELDS, LISTING_FIELDS, validate_entry_fields
from .utils.json_io import overlay, read_json_object
from .utils.tree import SyncError, Tree, exists

MARKETPLACE_PATHS = {platform: paths.marketplace for platform, paths in PLATFORM_PATHS.items()}


def render_entry(spec: PluginSpec, kind: str, manifest: dict) -> dict:
    target = spec.platforms[kind]
    local = PurePosixPath("plugins", spec.name) / target.root
    path = f"./{local.as_posix()}"
    common = overlay(spec.metadata, target.metadata)
    catalog_defaults = {
        key: value for key, value in common.items() if key in CATALOG_FIELDS[kind]
    }
    if kind == "codex":
        entry = {
            "name": spec.name,
            "source": {"source": "local", "path": path},
        }
        manifest_interface = manifest.get("interface", {})
        own_interface = target.manifest_overrides.get("interface", {})
        if (
            "category" not in catalog_defaults and isinstance(manifest_interface, dict)
            and isinstance(own_interface, dict) and "category" not in own_interface
            and "category" in manifest_interface
        ):
            entry["category"] = manifest_interface["category"]
    else:
        entry = {"name": spec.name, "source": path}
        fields = LISTING_FIELDS | ({"displayName"} if kind == "claude" else set())
        entry.update({
            field: manifest[field] for field in sorted(fields)
            if field in manifest and field not in target.manifest_overrides
        })
        if "version" in manifest:
            entry["version"] = manifest["version"]
    entry = overlay(overlay(entry, catalog_defaults), target.marketplace)
    validate_entry_fields(kind, {key: value for key, value in entry.items() if key in CATALOG_FIELDS[kind]})
    return entry


def plugin_entry(spec: PluginSpec, kind: str, *, tree: Tree) -> dict | None:
    target = spec.platforms.get(kind)
    if target is None or not target.listed:
        return None
    relative = f"plugins/{spec.name}/{target.manifest}"
    if not exists(tree.path(relative)):
        return None
    return render_entry(spec, kind, read_json_object(tree.file(relative)))


def local_plugin_directory(entry: dict) -> str | None:
    source = entry.get("source")
    if isinstance(source, dict):
        source = source.get("path") if source.get("source") == "local" else None
    if not isinstance(source, str):
        return None
    parts = PurePosixPath(source.removeprefix("./")).parts
    return parts[1] if len(parts) >= 2 and parts[0] == "plugins" else None


def managed_entry(entry: dict) -> bool:
    return local_plugin_directory(entry) == entry["name"]


def catalog_has_other_reference(root: Path, kind: str, name: str) -> bool:
    tree = Tree(root)
    relative = MARKETPLACE_PATHS[kind]
    if not exists(tree.path(relative)):
        return False
    entries = read_json_object(tree.file(relative)).get("plugins")
    if not isinstance(entries, list):
        raise SyncError(f"Expected a plugins array in {relative}")
    return any(isinstance(entry, dict) and entry.get("name") != name
               and local_plugin_directory(entry) == name for entry in entries)


def merge_entries(
    marketplace: dict, entries: list[dict], path: Path, *, excluded: set[str], kind: str,
) -> bool:
    current = marketplace.setdefault("plugins", [])
    if not isinstance(current, list):
        raise SyncError(f"Expected a plugins array in {path}")
    by_name = {}
    for entry in current:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise SyncError(f"Expected a named plugin entry in {path}")
        name = entry["name"]
        if name in excluded and managed_entry(entry):
            continue
        if name in by_name:
            raise SyncError(f"Duplicate plugin {name!r} in {path}")
        by_name[name] = entry
    removed = len(by_name) < len(current)
    for entry in entries:
        existing = by_name.setdefault(entry["name"], {})
        if existing and not managed_entry(existing):
            raise SyncError(f"{path}: generated entry conflicts with existing source for {entry['name']!r}")
        updated = {key: value for key, value in existing.items() if key not in CATALOG_FIELDS[kind] | {"version"}}
        updated.update(entry)
        if kind == "codex":
            updated.setdefault("category", DEFAULT_CODEX_CATEGORY)
            policy = overlay(
                {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                existing.get("policy", {}),
            )
            updated["policy"] = overlay(policy, entry.get("policy", {}))
        by_name[entry["name"]] = updated
    current[:] = by_name.values()
    return removed


def replace_marketplaces(root: Path, pending: dict[Path, str]) -> None:
    if not pending:
        return
    scratch = Path(mkdtemp(prefix="marketplace-", dir=root))
    recovery = []
    try:
        changes = []
        for index, (path, contents) in enumerate(pending.items()):
            previous = None
            if path.is_file():
                if path.read_text(encoding="utf-8") == contents:
                    continue
                previous = scratch / f"{index}.previous"
                shutil.copy2(path, previous)
            draft = scratch / f"{index}.json"
            draft.write_text(contents, encoding="utf-8", newline="\n")
            changes.append((path, draft, previous))

        written = []
        try:
            for path, draft, previous in changes:
                path.parent.mkdir(parents=True, exist_ok=True)
                draft.replace(path)
                written.append((path, previous))
        except BaseException as error:
            for path, previous in reversed(written):
                try:
                    if previous is None:
                        path.unlink()
                    else:
                        previous.replace(path)
                except OSError as restore_error:
                    action = f"restore {path} from {previous}" if previous else f"remove {path}"
                    recovery.append(f"{action}: {restore_error}")
            if recovery:
                raise SyncError(
                    f"Marketplace write failed ({error}); recovery files retained in {scratch}. "
                    + "; ".join(recovery)
                ) from error
            raise
    finally:
        if not recovery:
            shutil.rmtree(scratch, ignore_errors=True)


def catalog_types_for_plugin(root: Path, name: str, selected: Iterable[str]) -> set[str]:
    """Include existing entries whose plugin payload will also be replaced."""
    kinds = set(selected)
    if kinds == MARKETPLACE_PATHS.keys() or not root.is_dir():
        return kinds
    tree = Tree(root)
    for kind in MARKETPLACE_PATHS.keys() - kinds:
        relative = MARKETPLACE_PATHS[kind]
        if not exists(tree.path(relative)):
            continue
        catalog = read_json_object(tree.file(relative))
        entries = catalog.get("plugins")
        if not isinstance(entries, list):
            raise SyncError(f"Expected a plugins array in {relative}")
        if any(isinstance(entry, dict) and entry.get("name") == name and managed_entry(entry)
               for entry in entries):
            kinds.add(kind)
    return kinds


def write_marketplaces(
    root: Path, specs: Iterable[PluginSpec], *, types: Iterable[str] | None = None,
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
            path = tree.path(relative)
            has_marketplace = exists(path)
            marketplace = read_json_object(tree.file(relative)) if has_marketplace else {}
            entries = [
                entry for spec in specs
                if (entry := plugin_entry(spec, kind, tree=tree)) is not None
            ]
            excluded = {
                spec.name for spec in specs
                if kind not in spec.platforms or not spec.platforms[kind].listed
            }
            if not entries and not excluded:
                continue
            if not entries and not has_marketplace:
                continue
            marketplace.setdefault("name", name)
            if kind == "codex":
                marketplace.setdefault("interface", {"displayName": title})
            else:
                marketplace.setdefault("owner", {"name": title})
            removed = merge_entries(marketplace, entries, path, excluded=excluded, kind=kind)
            if not entries and not removed:
                continue
            pending[path] = json.dumps(marketplace, ensure_ascii=False, indent=4, allow_nan=False) + "\n"

        replace_marketplaces(tree.root, pending)
        return list(pending)
    finally:
        lock.rmdir()
