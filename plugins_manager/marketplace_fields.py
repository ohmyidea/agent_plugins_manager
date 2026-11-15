"""Fields accepted in a generated platform marketplace entry."""

import json

from .utils.tree import SyncError

LISTING_FIELDS = {"description", "author", "homepage", "repository", "license", "keywords"}
CATALOG_FIELDS = {
    "claude": LISTING_FIELDS | {
        "displayName", "category", "tags", "relevance", "strict",
        "dependencies", "defaultEnabled", "metadata",
    },
    "codex": {"category", "policy"},
    "copilot": LISTING_FIELDS | {"category", "tags", "strict"},
}


def validate_entry_fields(kind: str, fields: dict) -> None:
    unknown = fields.keys() - CATALOG_FIELDS[kind]
    if unknown:
        raise SyncError(f"Unsupported {kind} marketplace fields: {', '.join(sorted(unknown))}")
    for key in ("description", "displayName", "category", "homepage", "repository", "license"):
        if key in fields and not (isinstance(fields[key], str) and fields[key].strip()):
            raise SyncError(f"marketplace.{key} must be a nonempty string")
    for key in ("keywords", "tags"):
        if key in fields and not (
            isinstance(fields[key], list) and all(isinstance(value, str) for value in fields[key])
        ):
            raise SyncError(f"marketplace.{key} must be an array of strings")
    for key in ("strict", "defaultEnabled"):
        if key in fields and not isinstance(fields[key], bool):
            raise SyncError(f"marketplace.{key} must be true or false")
    for key in ("author", "metadata", "relevance"):
        if key in fields and not isinstance(fields[key], dict):
            raise SyncError(f"marketplace.{key} must be an object")
    if "author" in fields:
        author = fields["author"]
        if author.keys() - {"name", "email", "url"} or not all(
            isinstance(value, str) and value.strip() for value in author.values()
        ):
            raise SyncError("marketplace.author accepts name, email and url as nonempty strings")
    if "dependencies" in fields and not isinstance(fields["dependencies"], list):
        raise SyncError("marketplace.dependencies must be an array")
    if kind == "copilot" and len(fields.get("description", "")) > 1024:
        raise SyncError("Copilot marketplace.description exceeds 1024 characters")
    if "policy" in fields:
        policy = fields["policy"]
        if not isinstance(policy, dict) or policy.keys() - {"installation", "authentication"}:
            raise SyncError("marketplace.policy accepts installation and authentication")
        if "installation" in policy and policy["installation"] not in (
            "AVAILABLE", "INSTALLED_BY_DEFAULT", "NOT_AVAILABLE",
        ):
            raise SyncError("Invalid Codex marketplace.policy.installation")
        if "authentication" in policy and policy["authentication"] not in ("ON_INSTALL", "ON_USE"):
            raise SyncError("Invalid Codex marketplace.policy.authentication")
    try:
        json.dumps(fields, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise SyncError(f"Marketplace fields must be representable as JSON: {error}") from error
