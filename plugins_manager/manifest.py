"""Resolve manifest locations and render plugin manifest."""

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
from .utils.tree import SyncError, Tree, delete, exists, is_link

if TYPE_CHECKING:
    from .config import PlatformSpec, PluginSpec

class PlatformPaths(NamedTuple):
    manifest: str
    marketplace: str


PLATFORM_PATHS = {
    "codex": PlatformPaths(".codex-plugin/plugin.json", ".agents/plugins/marketplace.json"),
    "claude": PlatformPaths(".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"),
    "copilot": PlatformPaths("plugin.json", ".github/plugin/marketplace.json"),
}
MANIFEST_PATHS = {platform: paths.manifest for platform, paths in PLATFORM_PATHS.items()}

DEFAULT_CODEX_CATEGORY = "Productivity"
ICON_PATH = "assets/icon.png"
CODEX_ICON_FIELDS = {"composerIcon": f"./{ICON_PATH}", "logo": f"./{ICON_PATH}"}
CODEX_TEXT_FIELDS = {
    "displayName", "shortDescription", "longDescription", "developerName", "category",
    "websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL",
    "brandColor", "brandColorDark",
}
CODEX_IMAGE_FIELDS = {"logo", "logoDark", "composerIcon", "composerIconDark"}

COMMON_FIELDS = {"description", "author", "homepage", "repository", "license", "keywords"}
PLATFORM_FIELDS = {
    "codex": {"id", "interface", "skills", "mcpServers", "apps", "hooks", "extensions"},
    "claude": {
        "$schema", "displayName", "metadata", "icon", "documentationUrl", "supportUrl", "privacyPolicyUrl",
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
PLACEHOLDERS = (
    "<plugin_description>", "<author_name>", "<plugin_homepage>", "<plugin_repository>",
    "<plugin_license>", "<plugin_keywords>",
)


def manifest_fields_for(kind: str) -> set[str]:
    return COPILOT_FIELDS if kind == "copilot" else COMMON_FIELDS | PLATFORM_FIELDS[kind]


def template_file(root: Path, relative: str, fallback: str) -> Traversable:
    template = root / "template"
    if is_link(template) or exists(template / relative):
        return Tree(template).file(relative)
    return files(__package__).joinpath("defaults", fallback)


def infer_codex(manifest: dict, plugin: Tree, *, portable: bool) -> None:
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
            interface.setdefault(key, value)
    if plugin.path("skills").is_dir():
        manifest.setdefault("skills", "./skills/")
    if portable and (plugin.path("mcp.json").is_file() or plugin.path(".mcp.json").is_file()):
        manifest.setdefault("mcpServers", "./mcp.json")
    elif plugin.path(".mcp.json").is_file():
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


def manifest_template(root: Path, platform: str, stage: Tree, path: str) -> dict:
    if platform == "copilot" and stage.path(path).is_file():
        existing = read_json_object(stage.file(path))
        schema = existing.get("$schema")
        if isinstance(schema, str) and schema.startswith("https://agent-plugins.org/schemas/"):
            return existing
    shared_path = MANIFEST_PATHS["claude"]
    relative = MANIFEST_PATHS[platform]
    template = root / "template"
    if exists(template / relative):
        return read_json_object(Tree(template).file(relative))
    if not is_link(template) and not exists(template / shared_path):
        return {}
    shared = read_json_object(Tree(template).file(shared_path))
    if platform in ("codex", "copilot"):
        return {key: value for key, value in shared.items() if key in COMMON_FIELDS}
    return shared


def render_manifest(spec: PluginSpec, target: PlatformSpec, plugin: Tree, base: dict, version: str) -> dict:
    if target.kind == "copilot":
        base = {key: value for key, value in base.items() if key in COPILOT_FIELDS}
    metadata = target.manifest_metadata
    manifest = overlay(overlay(base, spec.metadata), metadata)
    manifest["name"] = spec.name
    manifest["version"] = metadata.get("version", version)
    if target.kind == "copilot":
        if target.schema is not None:
            manifest["$schema"] = f"https://agent-plugins.org/schemas/{target.schema}/plugin.schema.json"
        manifest.setdefault("$schema", DEFAULT_AGENT_SCHEMA)
    elif target.kind == "codex":
        if spec.icon is not None:
            interface = manifest.get("interface", {})
            if isinstance(interface, dict):
                explicit = metadata.get("interface", {})
                if isinstance(explicit, dict):
                    manifest["interface"] = overlay(interface, {
                        key: value for key, value in CODEX_ICON_FIELDS.items() if key not in explicit
                    })
        copilot = spec.platforms.get("copilot")
        infer_codex(manifest, plugin, portable=copilot is not None and copilot.root == target.root)
    else:
        manifest.setdefault("displayName", spec.name.replace("-", " ").replace("_", " ").title())
        if plugin.path(ICON_PATH).is_file() and (spec.icon is not None or "icon" not in manifest):
            manifest["icon"] = metadata.get("icon", f"./{ICON_PATH}")
    return manifest


def validate_display(kind: str, manifest: dict, plugin: Tree) -> None:
    if kind == "codex":
        interface = manifest.get("interface", {})
        if not isinstance(interface, dict):
            raise SyncError("Codex interface must be an object")
        for key in (CODEX_TEXT_FIELDS | CODEX_IMAGE_FIELDS) & interface.keys():
            value = interface[key]
            if not isinstance(value, str) or not value.strip():
                raise SyncError(f"interface.{key} must be a nonempty string")
        for key in ("capabilities", "screenshots", "defaultPrompt"):
            if key not in interface or (key == "defaultPrompt" and isinstance(interface[key], str)):
                continue
            values = interface[key]
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise SyncError(f"interface.{key} must be an array of strings")
        images = [interface[key] for key in CODEX_IMAGE_FIELDS if key in interface]
        images.extend(interface.get("screenshots", []))
    elif kind == "claude":
        for key in ("displayName", "icon", "documentationUrl", "supportUrl", "privacyPolicyUrl", "termsOfServiceUrl"):
            if key in manifest and not (isinstance(manifest[key], str) and manifest[key].strip()):
                raise SyncError(f"{key} must be a nonempty string")
        images = [manifest["icon"]] if "icon" in manifest else []
    else:
        return
    for image in images:
        if kind == "codex" and not image.startswith("./"):
            raise SyncError(f"Codex display assets must use ./ paths inside the plugin: {image!r}")
        if not image.startswith(("https://", "http://")):
            plugin.file(image.removeprefix("./"))


def align_shared_root(spec: PluginSpec, manifest: dict[str, dict]) -> None:
    codex = spec.platforms.get("codex")
    copilot = spec.platforms.get("copilot")
    if codex is None or copilot is None or codex.root != copilot.root:
        return
    native, portable = manifest["codex"], manifest["copilot"]
    for field in sorted(COMMON_FIELDS | {"name", "version"}):
        if native.get(field) != portable.get(field):
            raise SyncError(f"Codex and Copilot share a root with different {field}; set manifest.root")
    for field, expected in (("skills", "skills"), ("mcpServers", "mcp.json")):
        if field in native:
            value = native.get(field)
            if not isinstance(value, str) or value.removeprefix("./").rstrip("/") != expected:
                raise SyncError(f"Codex {field} conflicts with the portable layout; set manifest.root")
    extensions = native.get("extensions", {})
    if not isinstance(extensions, dict) or not isinstance(extensions.get("com.openai", {}), dict):
        raise SyncError("Codex extensions.com.openai must be an object")
    extension = dict(extensions.get("com.openai", {}))
    extension.update({key: native[key] for key in ("id", "interface", "apps", "hooks") if key in native})
    if not extension:
        return
    upstream = portable.get("extensions", {}).get("com.openai")
    if copilot.mode == "preserve":
        if upstream is not None and overlay(upstream, extension) != upstream:
            raise SyncError("Preserved Copilot com.openai overrides the Codex manifest; set manifest.root")
    else:
        portable.setdefault("extensions", {})["com.openai"] = overlay(upstream or {}, extension)


def write_manifest(root: Path, spec: PluginSpec, stage: Tree, version: str) -> dict[str, dict]:
    roots = {".", *(target.root for target in spec.platforms.values())}
    active = {target.manifest for target in spec.platforms.values()}
    copilot = spec.platforms.get("copilot")
    for plugin_root in roots:
        if copilot is None or copilot.root != plugin_root:
            for filename in LEGACY_COPILOT_PATHS:
                relative = (PurePosixPath(plugin_root) / filename).as_posix()
                if relative in active:
                    continue
                legacy = stage.path(relative)
                if legacy.is_file() or is_link(legacy):
                    legacy.unlink()
        for kind, filename in MANIFEST_PATHS.items():
            relative = (PurePosixPath(plugin_root) / filename).as_posix()
            path = stage.path(relative)
            if relative in active or not exists(path):
                continue
            if kind == "copilot":
                try:
                    document = read_json_object(stage.file(relative))
                except SyncError:
                    continue
                schema = document.get("$schema")
                if not isinstance(schema, str) or not schema.startswith("https://agent-plugins.org/schemas/"):
                    continue
            delete(path)
    rendered = {}
    for platform, target in spec.platforms.items():
        path = target.manifest
        try:
            base = (
                manifest_template(root, platform, stage, path)
                if target.mode == "template" else read_json_object(stage.file(path))
            )
            output = stage.path(path)
            output.parent.mkdir(parents=True, exist_ok=True)
            plugin = stage if target.root == "." else Tree(stage.path(target.root))
            if target.mode == "preserve" and base.get("name") != spec.name:
                raise SyncError(f"Preserved manifest name must match {spec.name!r}")
            manifest = base if target.mode == "preserve" else render_manifest(spec, target, plugin, base, version)
            if "version" in manifest and not (
                isinstance(manifest["version"], str) and manifest["version"].strip()
            ):
                raise SyncError("version must be a nonempty string")
            validate_display(platform, manifest, plugin)
            if platform == "copilot":
                prepare_copilot(plugin, validate_copilot_manifest(manifest))
            rendered[platform] = manifest
        except SyncError as error:
            raise SyncError(f"{path}: {error}") from error
    align_shared_root(spec, rendered)
    for platform, target in spec.platforms.items():
        if target.mode == "preserve":
            continue
        content = json.dumps(rendered[platform], ensure_ascii=False, indent=4, allow_nan=False)
        if target.mode == "template" and any(token in content for token in PLACEHOLDERS):
            raise SyncError(f"{target.manifest}: Missing template metadata")
        stage.write_text(target.manifest, content)
    return rendered
