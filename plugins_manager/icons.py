"""Read plugin icons from local PNG files or HTTP(S) image URLs."""

from http.client import HTTPException
from pathlib import Path
from urllib.request import Request, urlopen

from .config import PluginSpec
from .manifests import ICON_PATH, manifest_layout
from .utils.tree import SyncError, Tree, is_link

MAX_ICON_BYTES = 5 * 1024 * 1024
PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"


def read_icon(root: Path, source: str) -> bytes:
    """Read a PNG from an absolute path, a marketplace-relative path, or a URL."""
    try:
        if source.lower().startswith(("http://", "https://")):
            request = Request(source, headers={"Accept": "image/png", "User-Agent": "Plugins-Manager"})
            with urlopen(request, timeout=15) as stream:
                data = stream.read(MAX_ICON_BYTES + 1)
        else:
            with (root / source).open("rb") as stream:
                data = stream.read(MAX_ICON_BYTES + 1)
    except (OSError, ValueError, HTTPException) as error:
        raise SyncError(f"Could not read icon {source}: {error}") from error

    if len(data) > MAX_ICON_BYTES:
        raise SyncError(f"Icon exceeds 5 MiB: {source}")
    if not (
        data.startswith(PNG_HEADER) and data.endswith(PNG_END) and len(data) >= 57
        and int.from_bytes(data[16:20], "big") > 0 and int.from_bytes(data[20:24], "big") > 0
    ):
        raise SyncError(f"Expected a PNG icon: {source}")
    return data


def write_icons(root: Path, spec: PluginSpec, stage: Tree) -> None:
    if spec.icon is None:
        return
    paths = set()
    for manifest, mode in spec.manifests.items():
        platform, plugin_root = manifest_layout(manifest)
        if platform in ("codex", "claude") and (mode != "preserve" or not plugin_root.parts):
            paths.add((plugin_root / ICON_PATH).as_posix())
    if not paths:
        return

    data = read_icon(root, spec.icon)
    for relative in sorted(paths):
        path = stage.path(relative)
        if is_link(path):
            raise SyncError(f"Cannot write icon through a link: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
