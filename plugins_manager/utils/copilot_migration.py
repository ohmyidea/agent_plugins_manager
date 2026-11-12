"""Migrate staged Copilot components to the Agent Plugins layout."""

import json
from urllib.parse import urlsplit

from .json_io import read_json_object
from .tree import SyncError, Tree, exists, is_link

LEGACY_COPILOT_PATHS = (".github/plugin/plugin.json", ".plugin/plugin.json")


def portable_server(name: str, value: object) -> dict:
    if not isinstance(value, dict):
        raise SyncError(f"MCP server {name!r} must be an object")
    server = dict(value)
    transport = server.get("type")
    if transport is None:
        transport = "stdio" if "command" in server else "streamable-http"
    if transport == "http":
        transport = "streamable-http"
    server["type"] = transport

    error = f"MCP server {name!r} requires an explicit portable mcp.json"
    if transport == "stdio":
        command = server.get("command")
        args = server.get("args", [])
        env = server.get("env", {})
        cwd = server.get("cwd", "./")
        if cwd == ".":
            server["cwd"] = cwd = "./"
        if (
            server.keys() - {"type", "command", "args", "env", "cwd"}
            or not isinstance(command, str)
            or not command.strip()
            or "\\" in command
            or ("/" in command and not command.startswith("./"))
            or ".." in command.split("/")
            or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
            or not isinstance(env, dict)
            or {"PLUGIN_ROOT", "PLUGIN_DATA"} & env.keys()
            or not all(isinstance(item, str) for item in env.values())
            or not isinstance(cwd, str)
            or ".." in cwd.split("/")
            or not (
                cwd.startswith(("./", "${PLUGIN_ROOT}/", "${PLUGIN_DATA}/"))
                or cwd in ("${PLUGIN_ROOT}", "${PLUGIN_DATA}")
            )
        ):
            raise SyncError(error)
    elif transport in ("streamable-http", "sse"):
        endpoint = server.get("url")
        if server.keys() - {"type", "url"} or not isinstance(endpoint, str):
            raise SyncError(error)
        try:
            url = urlsplit(endpoint)
            loopback = url.hostname in ("localhost", "127.0.0.1", "::1")
            if (
                not url.hostname
                or url.username is not None
                or url.fragment
                or not (url.scheme == "https" or (url.scheme == "http" and loopback))
            ):
                raise SyncError(error)
        except ValueError as cause:
            raise SyncError(error) from cause
    else:
        raise SyncError(error)
    return server


def prepare_copilot(plugin: Tree, schema: str) -> None:
    mcp_schema = schema.removesuffix("plugin.schema.json") + "mcp.schema.json"
    if exists(plugin.path("mcp.json")):
        config = read_json_object(plugin.file("mcp.json"))
        if (
            config.keys() != {"$schema", "mcpServers"}
            or config["$schema"] != mcp_schema
            or not isinstance(config["mcpServers"], dict)
        ):
            raise SyncError(
                f"mcp.json must use {mcp_schema} and contain a mcpServers object"
            )
    elif exists(plugin.path(".mcp.json")):
        config = read_json_object(plugin.file(".mcp.json"))
        servers = config.get("mcpServers", config)
        if not isinstance(servers, dict):
            raise SyncError("Expected a mcpServers object in .mcp.json")
        document = {
            "$schema": mcp_schema,
            "mcpServers": {
                name: portable_server(name, value) for name, value in servers.items()
            },
        }
        plugin.write_text(
            "mcp.json",
            json.dumps(document, ensure_ascii=False, indent=4, allow_nan=False),
        )

    hooks_target = "com.github.copilot/hooks/hooks.json"
    if not exists(plugin.path(hooks_target)):
        for relative in ("hooks/hooks.json", "hooks.json"):
            if not plugin.path(relative).is_file():
                continue
            try:
                hooks = read_json_object(plugin.file(relative))
            except SyncError:
                continue
            version = hooks.get("version")
            if (
                type(version) is int
                and version == 1
                and isinstance(hooks.get("hooks"), dict)
            ):
                plugin.copy_from(plugin, relative, hooks_target)
                break
    for relative in LEGACY_COPILOT_PATHS:
        path = plugin.path(relative)
        if path.is_file() or is_link(path):
            path.unlink()
