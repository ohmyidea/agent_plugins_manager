"""Load reusable plugin recipes from TOML."""

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from .manifest import COMMON_FIELDS, MANIFEST_PATHS, manifest_fields_for
from .marketplace_fields import CATALOG_FIELDS, LISTING_FIELDS, validate_entry_fields
from .utils.json_io import overlay
from .utils.tree import SyncError, Tree, path_parts

DEFAULT_OUTPUT_DIR = "research-marketplace"

DEFAULT_CONFIGS_DIR = "configs"
PLUGIN_RECIPES_DIR = "plugins"

RULE_FIELDS = {"layout", "source_dir", "gitignore", "required", "operations", "icon"}

OPERATION_KEYS = {
    "copy": ({"from", "to"}, {"origin", "optional"}),
    "move": ({"from", "to"}, {"optional", "skip_existing"}),
    "remove": ({"paths"}, set()),
    "mkdir": ({"path"}, set()),
    "replace": ({"path", "old", "new"}, {"optional", "allow_unchanged"}),
    "replace_regex": ({"paths", "old", "new"}, set()),
}


def table(value: object) -> dict:
    if not isinstance(value, dict):
        raise SyncError("Expected a table")
    return value


def string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise SyncError(f"Expected a string, got {value!r}")
    return value


def choice(value: object, *choices: str) -> str:
    if value not in choices:
        raise SyncError(f"Expected one of {', '.join(choices)}, got {value!r}")
    return value


def array(value: object) -> list:
    if not isinstance(value, list):
        raise SyncError("Expected an array")
    return value


def relative(value: object) -> str:
    value = string(value)
    path_parts(value)
    return value


def check_keys(data: dict, required: set[str], optional: set[str]) -> None:
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing or unknown:
        raise SyncError(
            f"Missing fields {sorted(missing)}, unknown fields {sorted(unknown)}"
        )


@dataclass(frozen=True)
class Operation:
    action: str
    args: dict

    @classmethod
    def parse(cls, raw: object) -> "Operation":
        data = table(raw).copy()
        action = string(data.pop("action", None))
        if action not in OPERATION_KEYS:
            raise SyncError(f"Unknown operation: {action}")
        check_keys(data, *OPERATION_KEYS[action])
        for key in ("path", "from", "to"):
            if key in data:
                relative(data[key])
        if action in ("remove", "replace_regex"):
            for path in array(data["paths"]):
                relative(path)
        if action == "copy":
            choice(data.get("origin", "source"), "source", "stage")
        if action in ("replace", "replace_regex"):
            string(data["old"])
            string(data["new"], allow_empty=True)
        for key in ("optional", "allow_unchanged", "skip_existing"):
            if key in data and not isinstance(data[key], bool):
                raise SyncError(f"{key} must be true or false")
        return cls(action, data)

    def apply(self, source: Tree, stage: Tree) -> None:
        data = self.args
        optional = data.get("optional", False)
        match self.action:
            case "copy":
                origin = source if data.get("origin", "source") == "source" else stage
                stage.copy_from(origin, data["from"], data["to"], optional=optional)
            case "move":
                stage.move(
                    data["from"],
                    data["to"],
                    optional=optional,
                    skip_existing=data.get("skip_existing", False),
                )
            case "remove":
                stage.remove(*data["paths"])
            case "mkdir":
                stage.mkdir(data["path"])
            case "replace":
                stage.replace_text(
                    data["path"],
                    data["old"],
                    data["new"],
                    optional=optional,
                    allow_unchanged=data.get("allow_unchanged", False),
                )
            case "replace_regex":
                stage.replace_regex(data["paths"], data["old"], data["new"])
            case _:
                raise SyncError(f"Unknown operation: {self.action}")


@dataclass(frozen=True)
class PlatformSpec:
    kind: str
    metadata: dict = field(default_factory=dict)
    root: str = "."
    mode: str = "template"
    schema: str | None = None
    listed: bool = True
    marketplace: dict = field(default_factory=dict)
    manifest_overrides: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        choice(self.kind, *MANIFEST_PATHS)
        if self.root != ".":
            relative(self.root)
        choice(self.mode, "template", "merge", "preserve")
        if self.schema is not None:
            if self.kind != "copilot" or self.mode == "preserve":
                raise SyncError("manifest.schema requires a generated Copilot manifest")
            choice(self.schema, "1.0.0", "1.1.0")
        allowed = manifest_fields_for(self.kind) | {"version"}
        if self.kind == "copilot":
            allowed = allowed - {"$schema"}
        common = allowed & CATALOG_FIELDS[self.kind]
        common |= {"category"} if self.kind == "codex" else {"version"}
        validate_metadata(self.metadata, common)
        validate_entry_fields(self.kind, {
            key: value for key, value in self.metadata.items() if key not in LISTING_FIELDS | {"version"}
        })
        try:
            validate_metadata(self.manifest_overrides, allowed)
        except SyncError as error:
            raise SyncError(f"manifest: {error}") from error
        if self.mode == "preserve" and self.manifest_overrides:
            raise SyncError("manifest.mode = 'preserve' cannot apply manifest field overrides")
        validate_entry_fields(self.kind, table(self.marketplace))
        if not isinstance(self.listed, bool):
            raise SyncError("marketplace.enabled must be true or false")

    @property
    def manifest(self) -> str:
        return (PurePosixPath(self.root) / MANIFEST_PATHS[self.kind]).as_posix()

    @property
    def manifest_metadata(self) -> dict:
        common = self.metadata
        if self.kind == "codex" and "category" in self.metadata:
            common = {"interface": {"category": self.metadata["category"]}}
        return overlay(common, self.manifest_overrides)

    @classmethod
    def parse(cls, kind: str, raw: object) -> "PlatformSpec":
        data = table(raw).copy()
        manifest_overrides = table(data.pop("manifest", {})).copy()
        controls = {
            key: manifest_overrides.pop(key) for key in ("root", "mode", "schema")
            if key in manifest_overrides
        }
        marketplace = table(data.pop("marketplace", {})).copy()
        listed = marketplace.pop("enabled", True)
        return cls(
            kind, data, listed=listed, marketplace=marketplace,
            manifest_overrides=manifest_overrides, **controls,
        )


def validate_metadata(metadata: dict, allowed: set[str]) -> None:
    check_keys(table(metadata), set(), allowed)
    for key in ("version", "description", "homepage", "repository", "license"):
        if key in metadata:
            string(metadata[key])
    if "author" in metadata:
        author = table(metadata["author"])
        check_keys(author, set(), {"name", "email", "url"})
        for value in author.values():
            string(value)
    if "keywords" in metadata:
        for keyword in array(metadata["keywords"]):
            string(keyword)
    try:
        json.dumps(metadata, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise SyncError(f"Metadata must be representable as JSON: {error}") from error


@dataclass(frozen=True)
class PluginSpec:
    name: str
    repo: str
    ref: str = "HEAD"
    layout: str = "full"
    source_dir: str = "."
    gitignore: str = "template"
    metadata: dict = field(default_factory=dict)
    platforms: dict[str, PlatformSpec] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    operations: tuple[Operation, ...] = ()
    icon: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.name):
            raise SyncError(f"Invalid plugin name: {self.name!r}")
        relative(self.name)
        string(self.repo)
        string(self.ref)
        choice(self.layout, "full", "selective")
        if self.source_dir != ".":
            relative(self.source_dir)
        if self.icon is not None:
            string(self.icon)
        choice(self.gitignore, "template", "keep", "merge")
        validate_metadata(self.metadata, COMMON_FIELDS | {"version"})
        manifest_owners = {}
        for kind, target in table(self.platforms).items():
            if not isinstance(target, PlatformSpec) or kind != target.kind:
                raise SyncError(f"Invalid platform target: {kind}")
            path_key = target.manifest.casefold()
            other = manifest_owners.setdefault(path_key, kind)
            if other != kind:
                raise SyncError(
                    f"{other} and {kind} use the same manifest path {target.manifest}; set manifest.root"
                )
        for path in self.required:
            relative(path)

    @property
    def configured_version(self) -> str | None:
        return self.metadata.get("version")

    @classmethod
    def parse(cls, name: str, raw: object) -> "PluginSpec":
        data = table(raw).copy()
        if "version" in data:
            raise SyncError(f"Set version in [plugins.{name}.metadata]")
        misplaced = data.keys() & RULE_FIELDS
        if misplaced:
            raise SyncError(f"Move {', '.join(sorted(misplaced))} to [plugins.{name}.rules]")
        check_keys(data, {"repo"}, {"ref", "rules", "metadata"})
        try:
            rules = table(data.pop("rules", {})).copy()
            check_keys(rules, set(), RULE_FIELDS)
            if "operations" in rules:
                rules["operations"] = tuple(Operation.parse(op) for op in array(rules["operations"]))
            if "required" in rules:
                rules["required"] = tuple(array(rules["required"]))
        except SyncError as error:
            raise SyncError(f"rules: {error}") from error
        metadata = table(data.get("metadata", {})).copy()
        targets = {}
        for kind in MANIFEST_PATHS:
            if kind in metadata:
                try:
                    targets[kind] = PlatformSpec.parse(kind, metadata.pop(kind))
                except SyncError as error:
                    raise SyncError(f"metadata.{kind}: {error}") from error
        data["metadata"] = metadata
        data["platforms"] = targets
        return cls(name, **data, **rules)


def find_configs_dir(root: Path | None = None) -> Path:
    cwd = Path.cwd()
    locations = [cwd, *cwd.parents, Path(__file__).resolve().parents[1]]
    if root is not None:
        locations.insert(0, root)
    for directory in locations:
        configs = directory / DEFAULT_CONFIGS_DIR
        if (configs / PLUGIN_RECIPES_DIR).is_dir():
            return configs
    raise SyncError(
        f"External recipe directory {DEFAULT_CONFIGS_DIR}/{PLUGIN_RECIPES_DIR} not found."
    )


def load_recipe_file(config: Path) -> dict[str, PluginSpec]:
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        check_keys(data, {"plugins"}, set())
        recipes = table(data["plugins"])
    except (tomllib.TOMLDecodeError, SyncError) as error:
        raise SyncError(f"{config}: {error}") from error
    if not recipes:
        raise SyncError(f"No plugins configured in {config}")

    specs = {}
    for name, raw in recipes.items():
        try:
            spec = PluginSpec.parse(name, raw)
            if spec.icon is not None and spec.icon.startswith("config:"):
                icon = Path(string(spec.icon.removeprefix("config:")))
                if icon.anchor:
                    raise SyncError(
                        "config: icon paths must be relative to the recipe file"
                    )
                spec = replace(spec, icon=str((config.parent / icon).resolve()))
            specs[name] = spec
        except SyncError as error:
            raise SyncError(f"{config} [{name}]: {error}") from error
    return specs


def load_specs(
    path: Path | None = None, *, root: Path | None = None
) -> dict[str, PluginSpec]:
    """Load a single TOML recipe file or all TOML files in a directory."""
    if path is None:
        path = find_configs_dir(root) / PLUGIN_RECIPES_DIR
    location = path.resolve()
    configs = [location]
    if location.is_dir():
        configs = sorted(
            config for config in location.glob("*.toml") if config.is_file()
        )
        if not configs:
            raise SyncError(f"No TOML recipe files in {location}")

    specs = {}
    origins = {}
    for config in configs:
        for name, spec in load_recipe_file(config).items():
            if name in origins:
                raise SyncError(
                    f"Duplicate plugin {name!r} in {origins[name]} and {config}"
                )
            specs[name] = spec
            origins[name] = config
    return specs
