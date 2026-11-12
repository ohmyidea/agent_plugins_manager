"""Resolve manifest locations and render plugin manifests."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit

from .utils.copilot_migration import LEGACY_COPILOT_PATHS, prepare_copilot
from .utils.json_io import overlay, read_json_object
from .utils.tree import SyncError, Tree, exists, is_link

if TYPE_CHECKING:
    from .config import PluginSpec

class PlatformPaths(NamedTuple):
    manifest: str
    marketplace: str


PLATFORM_PATHS = {
    "codex": PlatformPaths(".codex-plugin/plugin.json", ".agents/plugins/marketplace.json"),
    "claude": PlatformPaths(".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"),
    "copilot": PlatformPaths("plugin.json", ".github/plugin/marketplace.json"),
}
MANIFEST_PATHS = {platform: paths.manifest for platform, paths in PLATFORM_PATHS.items()}
_MANIFEST_PLATFORMS = {path: platform for platform, path in MANIFEST_PATHS.items()}
_MANIFEST_PLATFORMS.update(dict.fromkeys(LEGACY_COPILOT_PATHS, "copilot"))

DEFAULT_CODEX_CATEGORY = "Productivity"
ICON_PATH = "assets/icon.png"
CODEX_ICON_FIELDS = {"composerIcon": f"./{ICON_PATH}", "logo": f"./{ICON_PATH}"}

COMMON_FIELDS = {"description", "author", "homepage", "repository", "license", "keywords"}
PLATFORM_FIELDS = {
    "codex": {"id", "interface", "skills", "mcpServers", "apps", "hooks", "extensions"},
    "claude": {
        "displayName", "metadata", "icon", "documentationUrl", "supportUrl", "privacyPolicyUrl",
        "termsOfServiceUrl", "defaultEnabled", "dependencies", "settings", "userConfig", "types",
        "channels", "skills", "commands", "agents", "hooks", "mcpServers", "lspServers",
        "outputStyles", "workflows", "experimental", "themes", "monitors",
    },
}
COPILOT_FIELDS = COMMON_FIELDS | {"$schema", "extensions"}
DEFAULT_AGENT_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PORTABLE_SCHEMAS = {
    f"https://agent-plugins.org/schemas/{version}/plugin.schema.json"
    for version in ("1.0.0", "1.1.0")
}
SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][a-zA-Z0-9-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][a-zA-Z0-9-]*))*)?"
    r"(?:\+[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*)?"
)
PLACEHOLDERS = (
    "<plugin_description>", "<author_name>", "<plugin_homepage>", "<plugin_repository>",
    "<plugin_license>", "<plugin_keywords>",
)


def manifest_layout(relative: str) -> tuple[str | None, PurePosixPath]:
    path = PurePosixPath(relative)
    for index in range(len(path.parts)):
        suffix = "/".join(path.parts[index:])
        platform = _MANIFEST_PLATFORMS.get(suffix)
        if platform is not None:
            return platform, PurePosixPath(*path.parts[:index])
    return None, path.parent


def template_file(root: Path, relative: str, fallback: str) -> Traversable:
    template = root / "template"
    if is_link(template) or exists(template / relative):
        return Tree(template).file(relative)
    return files(__package__).joinpath("defaults", fallback)


def infer_codex(manifest: dict, plugin: Tree, *, configured_icon: bool) -> None:
    interface = manifest.setdefault("interface", {})
    if not isinstance(interface, dict):
        raise SyncError("Codex interface must be an object")
    title = manifest["name"].replace("-", " ").replace("_", " ").title()
    interface.setdefault("displayName", title[:30])
    description = manifest.get("description")
    if isinstance(description, str) and description.strip():
        short = description[:30].rstrip()
        if len(description) > 30 and " " in short:
            short = short.rsplit(" ", 1)[0]
        interface.setdefault("shortDescription", short)
        interface.setdefault("longDescription", description)
    author = manifest.get("author")
    if isinstance(author, dict) and isinstance(author.get("name"), str):
        interface.setdefault("developerName", author["name"])
    homepage = manifest.get("homepage")
    if "websiteURL" not in interface and isinstance(homepage, str):
        homepage = homepage.strip()
        try:
            url = urlsplit(homepage)
        except ValueError:
            pass
        else:
            if url.scheme == "https" and url.hostname and url.username is None:
                interface["websiteURL"] = homepage
    interface.setdefault("category", DEFAULT_CODEX_CATEGORY)
    interface.setdefault("capabilities", [])
    if plugin.path(ICON_PATH).is_file():
        for key, value in CODEX_ICON_FIELDS.items():
            if configured_icon:
                interface[key] = value
            else:
                interface.setdefault(key, value)
    if plugin.path("skills").is_dir():
        manifest.setdefault("skills", "./skills/")
    if plugin.path(".mcp.json").is_file():
        manifest.setdefault("mcpServers", "./.mcp.json")


def validate_copilot_manifest(manifest: dict) -> str:
    schema = manifest.get("$schema")
    if not isinstance(schema, str) or schema not in PORTABLE_SCHEMAS:
        raise SyncError(f"Copilot requires a supported Agent Plugins $schema, got {schema!r}")
    name = manifest["name"]
    if (
        len(name) > 64 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name)
        or "--" in name or ".." in name
    ):
        raise SyncError(f"Invalid Agent Plugins name: {name!r}")
    unknown = manifest.keys() - COPILOT_FIELDS - {"name", "version"}
    if unknown:
        raise SyncError(f"Unsupported copilot manifest fields: {', '.join(sorted(unknown))}")
    extensions = manifest.get("extensions", {})
    if not isinstance(extensions, dict) or not all(isinstance(value, dict) for value in extensions.values()):
        raise SyncError("Agent Plugins extensions must be an object of namespaced objects")
    return schema


def manifest_template(root: Path, platform: str | None, stage: Tree, path: str) -> dict:
    if platform == "copilot" and stage.path(path).is_file():
        existing = read_json_object(stage.file(path))
        schema = existing.get("$schema")
        if isinstance(schema, str) and schema.startswith("https://agent-plugins.org/schemas/"):
            return existing
    shared_path = MANIFEST_PATHS["claude"]
    relative = MANIFEST_PATHS.get(platform, shared_path)
    template = root / "template"
    if exists(template / relative):
        return read_json_object(Tree(template).file(relative))
    if not is_link(template) and not exists(template / shared_path):
        return {}
    shared = read_json_object(Tree(template).file(shared_path))
    if platform in ("codex", "copilot"):
        return {key: value for key, value in shared.items() if key in COMMON_FIELDS}
    return shared


def render_manifest(
    spec: PluginSpec, platform: str | None, plugin: Tree, base: dict, version: str, commit: str
) -> dict:
    custom = spec.manifest_fields.get(platform, {})
    if platform == "copilot":
        allowed = COPILOT_FIELDS
        base = {key: value for key, value in base.items() if key in allowed}
        base.setdefault("$schema", DEFAULT_AGENT_SCHEMA)
    else:
        allowed = COMMON_FIELDS | PLATFORM_FIELDS.get(platform, set())

    unknown = custom.keys() - allowed
    if unknown:
        raise SyncError(f"Unsupported {platform} manifest fields: {', '.join(sorted(unknown))}")
    metadata = spec.metadata if platform is None else {
        key: value for key, value in spec.metadata.items() if key in allowed
    }
    manifest = overlay(overlay(base, metadata), custom)
    manifest["name"] = spec.name
    manifest["version"] = (
        f"0.0.0+git.{commit[:7]}"
        if platform in ("codex", "copilot") and not SEMVER.fullmatch(version)
        else version
    )
    if platform == "codex":
        infer_codex(manifest, plugin, configured_icon=spec.icon is not None)
    elif platform == "claude" and plugin.path(ICON_PATH).is_file():
        manifest.setdefault("icon", f"./{ICON_PATH}")
    return manifest


def write_manifests(root: Path, spec: PluginSpec, stage: Tree, version: str, commit: str) -> None:
    for path, mode in spec.manifests.items():
        try:
            platform, plugin_root = manifest_layout(path)
            base = (
                manifest_template(root, platform, stage, path)
                if mode == "template" else read_json_object(stage.file(path))
            )
            output = stage.path(path)
            output.parent.mkdir(parents=True, exist_ok=True)
            plugin = stage if not plugin_root.parts else Tree(stage.path(plugin_root.as_posix()))
            if mode == "preserve":
                if not isinstance(base.get("name"), str) or not base["name"].strip():
                    raise SyncError("Manifest name missing")
                if platform == "copilot":
                    prepare_copilot(plugin, validate_copilot_manifest(base))
                continue
            manifest = render_manifest(spec, platform, plugin, base, version, commit)
            if platform == "copilot":
                prepare_copilot(plugin, validate_copilot_manifest(manifest))
            content = json.dumps(manifest, ensure_ascii=False, indent=4, allow_nan=False)
            if mode == "template" and any(token in content for token in PLACEHOLDERS):
                raise SyncError("Missing template metadata")
            stage.write_text(path, content)
        except SyncError as error:
            raise SyncError(f"{path}: {error}") from error
