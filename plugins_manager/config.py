"""Load reusable plugin recipes from TOML."""

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .manifests import MANIFEST_PATHS, manifest_layout
from .utils.tree import SyncError, Tree, path_parts

DEFAULT_OUTPUT_DIR = "research-marketplace"

DEFAULT_CONFIGS_DIR = "configs"
PLUGIN_RECIPES_DIR = "plugins"

STANDARD_MANIFESTS = dict.fromkeys(MANIFEST_PATHS.values(), "template")

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
class PluginSpec:
    name: str
    repo: str
    ref: str = "HEAD"
    layout: str = "full"
    source_dir: str = "."
    version: str | None = None
    gitignore: str = "template"
    manifests: dict[str, str] = field(default_factory=STANDARD_MANIFESTS.copy)
    metadata: dict = field(default_factory=dict)
    required: tuple[str, ...] = ()
    operations: tuple[Operation, ...] = ()
    icon: str | None = None
    manifest_fields: dict[str, dict] = field(default_factory=dict)
    marketplaces: tuple[str, ...] = tuple(MANIFEST_PATHS)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.name):
            raise SyncError(f"Invalid plugin name: {self.name!r}")
        relative(self.name)
        string(self.repo)
        string(self.ref)
        choice(self.layout, "full", "selective")
        if self.source_dir != ".":
            relative(self.source_dir)
        if self.version is not None:
            string(self.version)
        if self.icon is not None:
            string(self.icon)
        choice(self.gitignore, "template", "keep", "merge")
        for marketplace in self.marketplaces:
            choice(marketplace, *MANIFEST_PATHS)
        for path, mode in table(self.manifests).items():
            relative(path)
            choice(mode, "template", "merge", "preserve")
            platform, plugin_root = manifest_layout(path)
            if platform == "copilot":
                target = (plugin_root / MANIFEST_PATHS["copilot"]).as_posix()
                if path != target:
                    raise SyncError(
                        f"Copilot manifest must use {target}, replacing {path}"
                    )
        table(self.metadata)
        try:
            json.dumps(self.metadata, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise SyncError(
                f"Metadata must be representable as JSON: {error}"
            ) from error
        if {"name", "version"} & self.metadata.keys():
            raise SyncError("Set name and version on the plugin, outside metadata")
        for platform, fields in table(self.manifest_fields).items():
            choice(platform, *MANIFEST_PATHS)
            table(fields)
            if {"name", "version"} & fields.keys():
                raise SyncError(
                    "Set name and version on the plugin, outside manifest_fields"
                )
        try:
            json.dumps(self.manifest_fields, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise SyncError(
                f"Manifest fields must be representable as JSON: {error}"
            ) from error
        for path in self.required:
            relative(path)

    @classmethod
    def parse(cls, name: str, raw: object) -> "PluginSpec":
        data = table(raw).copy()
        check_keys(
            data,
            {"repo"},
            {
                "ref",
                "layout",
                "source_dir",
                "version",
                "gitignore",
                "manifests",
                "metadata",
                "required",
                "operations",
                "icon",
                "manifest_fields",
                "marketplaces",
            },
        )
        data["operations"] = tuple(
            Operation.parse(op) for op in array(data.get("operations", []))
        )
        data["required"] = tuple(array(data.get("required", [])))
        if "marketplaces" in data:
            data["marketplaces"] = tuple(array(data["marketplaces"]))
        return cls(name, **data)


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
