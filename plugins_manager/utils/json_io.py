"""Read JSON objects and recursively overlay their fields."""

import json
from importlib.resources.abc import Traversable

from .tree import SyncError


def read_json_object(path: Traversable) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        raise SyncError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise SyncError(f"Expected a JSON object: {path}")
    return data


def overlay(base: dict, updates: dict) -> dict:
    """Overlay metadata while keeping unmodified keys in nested objects."""
    result = base.copy()
    for key, value in updates.items():
        previous = result.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            value = overlay(previous, value)
        result[key] = value
    return result
